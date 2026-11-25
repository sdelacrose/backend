"""L00-B3.1: resolution + the one validated write path for
StaffCompensationHistory -- the RH12 (StaffQuotiteHistory) pattern
transposed to compensation. Never rewrites a row; a qualification,
correction or cancellation is always a NEW row."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from rest_framework.exceptions import ValidationError

from human_resource_management.models.staff import StaffCompensationHistory
from human_resource_management.permissions import can_update_staff_compensation


class StaffCompensationError(Exception):
    """A business-rule violation (e.g. no prior situation to revert a
    cancellation to) -- distinct from a permission refusal, mirrors the
    LeaveAccrualQuotiteError precedent (services.leave_accrual)."""


def resolve_current_compensation(staff, as_of=None):
    """The StaffCompensationHistory row genuinely in effect for `staff` as
    of `as_of` (defaults to today) -- the ONE authoritative source for
    "current compensation", resolved live on every read. None if the
    employee has no compensation history at all yet (never a guess).
    Same resolution rule as RH12's resolve_current_quotite_entry: on a
    same-`effective_from` collision, the most RECENTLY CREATED row wins."""
    as_of = as_of or date.today()
    return (
        StaffCompensationHistory.objects.filter(staff=staff, effective_from__lte=as_of)
        .order_by("-effective_from", "-created_at")
        .first()
    )


def resolve_next_scheduled_compensation(staff, as_of=None):
    """The single soonest FUTURE entry (effective_from > as_of), if any --
    exposed separately from the current situation, same "actuel vs
    programme" pattern as StaffSerializer.get_current_regime.

    Tie-break on the soonest effective_from is DESCENDING created_at (most
    recently created wins) -- the same "latest word wins" rule as
    resolve_current_compensation above. This matters once a scheduled entry
    has been cancelled/replaced: the cancellation shares that SAME
    effective_from (see create_compensation_event, event_type=cancellation)
    specifically so it outranks the entry it cancels here too, rather than
    the cancelled entry still being shown as "scheduled"."""
    as_of = as_of or date.today()
    return (
        StaffCompensationHistory.objects.filter(staff=staff, effective_from__gt=as_of)
        .order_by("effective_from", "-created_at")
        .first()
    )


def _to_decimal(value, field_name):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError({field_name: "Must be a valid decimal number."})


def create_compensation_event(
    staff, *, actor, event_type, effective_from=None,
    gross_monthly_salary=None, hourly_rate=None, currency=None,
    basis=None, supersedes=None, note="",
):
    """The ONE write path for StaffCompensationHistory -- used by
    StaffCompensationHistoryViewSet.create for every event_type. DEC-04 (no
    self-mutation) and can_update_staff_compensation are enforced here,
    UNCONDITIONALLY, regardless of event_type: qualifying, correcting or
    cancelling one's OWN compensation is exactly as refused as changing it
    outright -- same wording/status (400 ValidationError) as
    StaffSerializer.validate() for the raw salary/hourly_rate path, kept
    consistent on purpose."""
    if actor is not None and actor == staff.user:
        raise ValidationError({
            "non_field_errors": [
                "You cannot change your own compensation -- this requires "
                "validation by another authorized person (not yet available)."
            ]
        })
    if actor is None or not can_update_staff_compensation(actor, staff):
        raise ValidationError({
            "non_field_errors": ["You do not have permission to change this employee's compensation."]
        })

    gross_monthly_salary = _to_decimal(gross_monthly_salary, "gross_monthly_salary")
    hourly_rate = _to_decimal(hourly_rate, "hourly_rate")

    if event_type == StaffCompensationHistory.EVENT_QUALIFICATION:
        if supersedes is None or supersedes.staff_id != staff.id:
            raise ValidationError({"supersedes": "A qualification must reference an existing entry for this same employee."})
        if supersedes.basis != StaffCompensationHistory.BASIS_LEGACY_UNQUALIFIED:
            raise ValidationError({"supersedes": "Only a legacy, unqualified entry can be qualified."})
        effective_from = supersedes.effective_from
        basis = StaffCompensationHistory.BASIS_CONFIRMED_GROSS
        if gross_monthly_salary is None:
            gross_monthly_salary = supersedes.gross_monthly_salary
        if hourly_rate is None:
            hourly_rate = supersedes.hourly_rate
        if not currency:
            currency = supersedes.currency

    elif event_type == StaffCompensationHistory.EVENT_CANCELLATION:
        if supersedes is None or supersedes.staff_id != staff.id:
            raise ValidationError({"supersedes": "A cancellation must reference an existing entry for this same employee."})
        if supersedes.effective_from <= date.today():
            raise ValidationError({"supersedes": "Only a still-FUTURE scheduled entry can be cancelled -- it cannot already be in effect."})
        prior = resolve_current_compensation(staff, as_of=supersedes.effective_from - timedelta(days=1))
        if prior is None:
            raise StaffCompensationError(
                "No prior situation exists before this scheduled change -- there is nothing to revert to. "
                "Record a correction instead if this entry was a mistake."
            )
        effective_from = supersedes.effective_from
        gross_monthly_salary = prior.gross_monthly_salary
        hourly_rate = prior.hourly_rate
        currency = prior.currency
        basis = prior.basis

    elif event_type in (StaffCompensationHistory.EVENT_NEW_SITUATION, StaffCompensationHistory.EVENT_CORRECTION):
        if effective_from is None:
            raise ValidationError({"effective_from": "This field is required."})
        if gross_monthly_salary is None and hourly_rate is None:
            raise ValidationError({"non_field_errors": ["At least one of gross_monthly_salary / hourly_rate must be provided."]})
        if not currency:
            raise ValidationError({"currency": "This field is required."})
        if not basis:
            basis = StaffCompensationHistory.BASIS_CONFIRMED_GROSS
        if supersedes is not None and supersedes.staff_id != staff.id:
            raise ValidationError({"supersedes": "Must reference an entry for this same employee."})

    else:
        raise ValidationError({"event_type": "Invalid or missing event_type."})

    entry = StaffCompensationHistory.objects.create(
        staff=staff, gross_monthly_salary=gross_monthly_salary, hourly_rate=hourly_rate,
        currency=currency, basis=basis, event_type=event_type, supersedes=supersedes,
        effective_from=effective_from, note=note or "", created_by=actor,
    )
    return entry
