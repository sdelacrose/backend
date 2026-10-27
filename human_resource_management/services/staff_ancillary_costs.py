"""L00-B3.2: resolution + the one validated write path for
CompanyChargeRateHistory and StaffAncillaryCostHistory -- same append-only,
deterministic-resolution discipline as StaffCompensationHistory (L00-B3.1).
"""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from human_resource_management.models.staff import (
    CompanyChargeRateHistory, StaffAncillaryCostHistory,
)
from human_resource_management.permissions import (
    can_update_company_compensation_config, can_update_staff_compensation,
)


class StaffAncillaryCostError(Exception):
    """A business-rule violation (e.g. no prior assignment to revert a
    cancellation to) -- distinct from a permission refusal."""


def _to_decimal(value, field_name):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Must be a valid decimal number."})


# === Company-wide charge rates ===============================================

def resolve_current_charge_rates(company_profile, as_of=None):
    """The CompanyChargeRateHistory row in effect for `company_profile` as
    of `as_of` -- None if no rate has ever been recorded (genuinely
    UNKNOWN, never defaulted to 0%). Same (-effective_from, -created_at)
    resolution rule as compensation."""
    as_of = as_of or date.today()
    return (
        CompanyChargeRateHistory.objects.filter(company_profile=company_profile, effective_from__lte=as_of)
        .order_by("-effective_from", "-created_at")
        .first()
    )


def resolve_next_scheduled_charge_rates(company_profile, as_of=None):
    as_of = as_of or date.today()
    return (
        CompanyChargeRateHistory.objects.filter(company_profile=company_profile, effective_from__gt=as_of)
        .order_by("effective_from", "-created_at")
        .first()
    )


def create_charge_rate_event(
    company_profile, *, actor, event_type, effective_from=None,
    employee_rate_percent=None, employer_rate_percent=None, supersedes=None, note="",
):
    """No DEC-04 concept here -- company-wide, see
    can_update_company_compensation_config's docstring."""
    if not can_update_company_compensation_config(actor, company_profile):
        raise ValidationError({
            "non_field_errors": ["You do not have permission to change this company's charge rates."]
        })

    employee_rate_percent = _to_decimal(employee_rate_percent, "employee_rate_percent")
    employer_rate_percent = _to_decimal(employer_rate_percent, "employer_rate_percent")

    if event_type == CompanyChargeRateHistory.EVENT_CANCELLATION:
        if supersedes is None or supersedes.company_profile_id != company_profile.id:
            raise ValidationError({"supersedes": "A cancellation must reference an existing entry for this same company."})
        if supersedes.effective_from <= date.today():
            raise ValidationError({"supersedes": "Only a still-FUTURE scheduled entry can be cancelled."})
        prior = resolve_current_charge_rates(company_profile, as_of=supersedes.effective_from - timedelta(days=1))
        if prior is None:
            raise StaffAncillaryCostError(
                "No prior charge rates exist before this scheduled change -- nothing to revert to."
            )
        effective_from = supersedes.effective_from
        employee_rate_percent = prior.employee_rate_percent
        employer_rate_percent = prior.employer_rate_percent

    elif event_type in (CompanyChargeRateHistory.EVENT_NEW_SITUATION, CompanyChargeRateHistory.EVENT_CORRECTION):
        if effective_from is None:
            raise ValidationError({"effective_from": "This field is required."})
        if employee_rate_percent is None or employer_rate_percent is None:
            raise ValidationError({"non_field_errors": ["Both employee_rate_percent and employer_rate_percent are required."]})
        # L00-B3.2: meaningful bound -- a negative charge rate makes no
        # sense. Deliberately NO upper bound (never an arbitrary employer
        # ceiling imposed, per explicit instruction).
        if employee_rate_percent < 0 or employer_rate_percent < 0:
            raise ValidationError({"non_field_errors": ["Charge rate percentages cannot be negative."]})
    else:
        raise ValidationError({"event_type": "Invalid or missing event_type."})

    return CompanyChargeRateHistory.objects.create(
        company_profile=company_profile, employee_rate_percent=employee_rate_percent,
        employer_rate_percent=employer_rate_percent, effective_from=effective_from,
        event_type=event_type, supersedes=supersedes, note=note or "", created_by=actor,
    )


# === Per-employee ancillary cost assignments =================================

def resolve_active_ancillary_costs(staff, as_of=None):
    """One entry per DISTINCT cost_item_type the employee has ANY history
    for, resolved independently -- the latest row at/before `as_of` for
    each type. A type whose latest resolved row has amount=None (a
    termination, or a cancellation reverting to "nothing before") is
    EXCLUDED -- it is not currently active, never shown as a real 0 cost."""
    as_of = as_of or date.today()
    # NOTE: `.order_by("cost_item_type_id")` here is NOT cosmetic -- without
    # it, the model's own Meta.ordering (which also sorts by effective_from/
    # created_at) gets folded into the SQL, which silently defeats
    # `.distinct()` on the single `cost_item_type_id` column (a well-known
    # Django pitfall). `set(...)` below is an extra, cheap safety net on top.
    type_ids = set(
        StaffAncillaryCostHistory.objects.filter(staff=staff, effective_from__lte=as_of)
        .order_by("cost_item_type_id").values_list("cost_item_type_id", flat=True).distinct()
    )
    active = []
    for type_id in type_ids:
        latest = (
            StaffAncillaryCostHistory.objects.filter(
                staff=staff, cost_item_type_id=type_id, effective_from__lte=as_of,
            )
            .select_related("cost_item_type")
            .order_by("-effective_from", "-created_at")
            .first()
        )
        if latest is not None and latest.amount is not None:
            active.append(latest)
    return active


def resolve_scheduled_ancillary_cost_changes(staff, as_of=None):
    """Future-dated rows, one per cost_item_type with an upcoming change --
    same (effective_from, -created_at) tie-break as compensation."""
    as_of = as_of or date.today()
    type_ids = set(
        StaffAncillaryCostHistory.objects.filter(staff=staff, effective_from__gt=as_of)
        .order_by("cost_item_type_id").values_list("cost_item_type_id", flat=True).distinct()
    )
    scheduled = []
    for type_id in type_ids:
        next_entry = (
            StaffAncillaryCostHistory.objects.filter(
                staff=staff, cost_item_type_id=type_id, effective_from__gt=as_of,
            )
            .order_by("effective_from", "-created_at")
            .first()
        )
        if next_entry is not None:
            scheduled.append(next_entry)
    return scheduled


def create_ancillary_cost_event(
    staff, *, actor, cost_item_type, event_type, effective_from=None,
    amount=None, currency=None, frequency=None, included_in_consolidated_cost=None,
    nature=StaffAncillaryCostHistory.NATURE_ESTIMATED, supersedes=None, note="",
):
    """DEC-04 applies here exactly as it does to compensation -- an
    employee's own ancillary-cost benefits/assignments are part of their
    overall compensation package, and self-assignment would be an
    equally-real self-benefit mechanism."""
    if actor is not None and actor == staff.user:
        raise ValidationError({
            "non_field_errors": [
                "You cannot change your own ancillary cost assignments -- this requires "
                "validation by another authorized person (not yet available)."
            ]
        })
    if actor is None or not can_update_staff_compensation(actor, staff):
        raise ValidationError({
            "non_field_errors": ["You do not have permission to change this employee's ancillary costs."]
        })

    amount = _to_decimal(amount, "amount")

    if event_type == StaffAncillaryCostHistory.EVENT_CANCELLATION:
        if supersedes is None or supersedes.staff_id != staff.id:
            raise ValidationError({"supersedes": "A cancellation must reference an existing entry for this same employee."})
        if supersedes.effective_from <= date.today():
            raise ValidationError({"supersedes": "Only a still-FUTURE scheduled entry can be cancelled."})
        prior_rows = [
            r for r in (
                StaffAncillaryCostHistory.objects.filter(
                    staff=staff, cost_item_type_id=supersedes.cost_item_type_id,
                    effective_from__lte=supersedes.effective_from - timedelta(days=1),
                )
                .order_by("-effective_from", "-created_at")
            )
        ]
        prior = prior_rows[0] if prior_rows else None
        effective_from = supersedes.effective_from
        cost_item_type = supersedes.cost_item_type
        if prior is None:
            # Nothing was assigned before this scheduled entry -- cancelling
            # it means "never assigned", represented as a termination-shaped
            # row (amount=None), never a guessed value.
            amount, currency, frequency, included_in_consolidated_cost = None, None, None, None
        else:
            amount = prior.amount
            currency = prior.currency
            frequency = prior.frequency
            included_in_consolidated_cost = prior.included_in_consolidated_cost
            nature = prior.nature

    elif event_type == StaffAncillaryCostHistory.EVENT_TERMINATION:
        if effective_from is None:
            raise ValidationError({"effective_from": "This field is required."})
        amount, currency, frequency, included_in_consolidated_cost = None, None, None, None

    elif event_type in (StaffAncillaryCostHistory.EVENT_NEW_ASSIGNMENT, StaffAncillaryCostHistory.EVENT_CORRECTION):
        if effective_from is None:
            raise ValidationError({"effective_from": "This field is required."})
        if amount is None:
            raise ValidationError({"amount": "This field is required for a new assignment or correction."})
        if not currency:
            raise ValidationError({"currency": "This field is required."})
        if frequency not in dict(StaffAncillaryCostHistory.FREQUENCY_CHOICES):
            raise ValidationError({"frequency": "Invalid or missing frequency."})
        if included_in_consolidated_cost is None:
            raise ValidationError({"included_in_consolidated_cost": "This field is required (explicit, no default)."})
    else:
        raise ValidationError({"event_type": "Invalid or missing event_type."})

    return StaffAncillaryCostHistory.objects.create(
        staff=staff, cost_item_type=cost_item_type, amount=amount, currency=currency,
        frequency=frequency, included_in_consolidated_cost=included_in_consolidated_cost,
        nature=nature, event_type=event_type, supersedes=supersedes,
        effective_from=effective_from, note=note or "", created_by=actor,
    )
