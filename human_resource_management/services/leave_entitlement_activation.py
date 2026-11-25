"""LOT RH 9, section 5: explicit, per-company activation of the new
per-type/per-cycle entitlement mechanism. Never a silent switch -- an
existing company keeps running on the legacy global
`Staff.annual_leaves_allowed` balance (services/leave_balance.py) until its
RH reviews a preview and explicitly confirms.

`build_activation_preview` is read-only and side-effect free; `activate`
re-validates the exact same blocking conditions before making any change, so
a stale preview can never be used to force through an activation that would
otherwise be blocked.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave_entitlement import LeaveEntitlement
from human_resource_management.services.leave_accrual import (
    LeaveAccrualError,
    LeaveAccrualTimezoneError,
    QUANT,
    _iter_months,
    _month_bounds,
    compute_presence_window,
    last_complete_local_month,
    resolve_company_timezone,
    resolve_historical_rate,
)
from human_resource_management.services.leave_balance import (
    RESOLVED,
    compute_leave_request_days,
    resolve_leave_type,
)
from human_resource_management.services.leave_entitlement import (
    LeaveEntitlementError,
    resolve_cycle_period,
    resolve_cycle_periods_for_range,
)
from models.models import CompanyConfig, LeaveType, Staff


class ActivationBlockedError(Exception):
    """Raised by `activate` when re-validation finds the preview is stale or
    the company still has a blocking condition -- never silently proceeds."""


def _project_acquired_through(staff, leave_type, last_year, last_month):
    """Read-only projection (no DB writes, no LeaveEntitlement/
    LeaveAccrualCredit rows touched) of what `credit_month` WOULD total if
    run now, from the type's accrual_effective_date through
    (last_year, last_month) inclusive -- for the activation preview only,
    never authoritative (the real run is `services.leave_accrual.
    run_monthly_accrual`/`credit_month`). Does not split by cycle boundary
    (an acceptable simplification for an informational total). Propagates
    `LeaveAccrualError` for a genuinely unresolvable month (no rate history,
    no accrual_effective_date) -- the caller treats that as "not projectable
    yet", never a guess."""
    effective_date = leave_type.effective_accrual_effective_date()
    if effective_date is None:
        raise LeaveAccrualError(f"Leave type '{leave_type.name}' has no accrual_effective_date configured.")
    if (effective_date.year, effective_date.month) > (last_year, last_month):
        return Decimal("0")
    total = Decimal("0")
    for year, month in _iter_months(effective_date.year, effective_date.month, last_year, last_month):
        month_start, month_end = _month_bounds(year, month)
        window = compute_presence_window(staff, leave_type, month_start, month_end)
        if window is None:
            continue
        start, end = window
        days_in_month = (month_end - month_start).days + 1
        window_days = (end - start).days + 1
        rate = resolve_historical_rate(leave_type, month_end)
        total += (Decimal(rate) * Decimal(window_days) / Decimal(days_in_month)).quantize(
            QUANT, rounding=ROUND_HALF_UP
        )
    return total


@dataclass
class ActivationPreview:
    company_profile_id: int
    transition_leave_type_id: "int | None"
    types_and_cycles: list = field(default_factory=list)  # [{leave_type_id, name, cycle_period}]
    pending_count: int = 0
    approved_count: int = 0
    ambiguous_request_ids: list = field(default_factory=list)
    missing_counter_items: list = field(default_factory=list)  # [{employee_id, leave_type_id, cycle_start, cycle_end}]
    negative_available_items: list = field(default_factory=list)  # [{employee_id, leave_type_id, cycle_start, cycle_end, available}]
    proposed_transition_quotas: dict = field(default_factory=dict)  # {employee_id: Decimal}

    # LOT RH10 follow-up: "adapter son aperçu au mode mensuel" -- activation
    # stays the ONE, shared, explicit switch (CompanyConfig.
    # leave_entitlement_mode_active) for both quota and monthly-accrual
    # types; this section previews the monthly side of that same decision,
    # never adding a separate switch and never summing a projected monthly
    # total with a legacy/quota figure.
    monthly_accrual_types: list = field(default_factory=list)  # [{leave_type_id, name, rate, effective_date}]
    monthly_missing_config_items: list = field(default_factory=list)  # [{leave_type_id, name, missing: [...]}]
    monthly_missing_contract_items: list = field(default_factory=list)  # [{employee_id, leave_type_id}]
    monthly_timezone_blocked: "str | None" = None  # the blocking message, or None if resolvable
    monthly_projected: dict = field(default_factory=dict)  # {(employee_id, leave_type_id): {"acquired_through": float, "consumed": float, "reserved": float}}

    @property
    def can_activate(self):
        return (
            self.transition_leave_type_id is not None
            and not self.ambiguous_request_ids
            and not self.missing_counter_items
            and not self.negative_available_items
            and not self.monthly_missing_config_items
            and not self.monthly_missing_contract_items
        )


def _consuming_requests(company_profile_id, *, solution=None):
    """LOT RH13 follow-up (solution isolation): when `solution` is given,
    excludes requests whose leave_type belongs to a DIFFERENT solution's
    CompanyConfig -- a company configured for more than one solution must
    never have activation consider another solution's requests. A legacy
    row with no stable `leave_type` FK (pre-dates that field) is kept either
    way -- it can't be attributed to a solution, and already never resolves
    to a real cycle below (blocks activation, same as before this fix)."""
    qs = LeaveRequest.objects.filter(
        employee__user__company_id=company_profile_id,
        status__in=[StatusType.PENDING.value, StatusType.APPROVED.value],
        is_working_from_home=False,
    )
    if solution is not None:
        qs = qs.filter(Q(leave_type__isnull=True) | Q(leave_type__company_config__solution=solution))
    return qs.select_related("employee__user", "leave_type").order_by("id")


def build_activation_preview(company_profile_id, *, transition_leave_type_id=None, solution=None):
    """`solution`: LOT RH13 follow-up (solution isolation) -- when given,
    every leave type/request considered is scoped to THIS solution's own
    CompanyConfig, never the whole company. `None` preserves the exact
    prior (company-wide) behavior for any caller not yet passing it."""
    preview = ActivationPreview(
        company_profile_id=company_profile_id,
        transition_leave_type_id=transition_leave_type_id,
    )
    transition_leave_type = None
    if transition_leave_type_id is not None:
        transition_type_filters = {"pk": transition_leave_type_id, "company_config__company_profile_id": company_profile_id}
        if solution is not None:
            transition_type_filters["company_config__solution"] = solution
        transition_leave_type = LeaveType.objects.filter(**transition_type_filters).first()
        if transition_leave_type is None:
            raise ActivationBlockedError("The chosen transition leave type does not belong to this company.")
        if transition_leave_type.effective_accrual_rate() is not None:
            # LOT RH10 follow-up: "ne pas additionner quota historique et
            # acquisition mensuelle" -- the quota transition attaches the
            # legacy annual_leaves_allowed balance as a QUOTA-mode
            # LeaveEntitlement on the chosen type's current cycle for every
            # employee; a type ALSO configured for monthly accrual would then
            # have a pre-existing quota row blocking credit_month from ever
            # crediting it (it refuses to touch a quota-mode row -- see
            # LeaveAccrualError there), mixing the two mechanisms for the
            # same type. Refused outright, never silently allowed.
            raise ActivationBlockedError(
                f"The chosen transition leave type '{transition_leave_type.name}' is configured "
                f"for monthly accrual -- it cannot also be used as the quota-mode transition "
                f"target. Choose a different, quota-only leave type for the legacy balance "
                f"transition."
            )

    today = date.today()
    covered = set()  # (employee_id, leave_type_id, cycle_period.id) already backed by an existing entitlement
    projected = {}  # (employee_id, leave_type_id, cycle_period) -> {"consumed": Decimal, "reserved": Decimal}
    seen_type_cycle = set()
    monthly_projected_raw = {}  # (employee_id, leave_type_id) -> {"consumed": Decimal, "reserved": Decimal}
    monthly_types_seen = {}  # leave_type_id -> LeaveType

    for leave_request in _consuming_requests(company_profile_id, solution=solution):
        employee = leave_request.employee
        if leave_request.status == StatusType.PENDING.value:
            preview.pending_count += 1
        else:
            preview.approved_count += 1

        leave_type = leave_request.leave_type
        if leave_type is None:
            # Historical row with no stable FK -- resolve by name. Ambiguous/
            # unresolved rows can never be checked against a real cycle, so
            # they always block activation.
            leave_type, resolution = (
                resolve_leave_type(leave_request.type, company_profile_id)
                if leave_request.type else (None, "unresolved_no_match")
            )
            if resolution != RESOLVED:
                preview.ambiguous_request_ids.append(leave_request.id)
                continue

        if not leave_type.effective_value("consumes_balance"):
            continue  # non-consuming type, no counter needed at all

        if leave_type.effective_accrual_rate() is not None:
            # LOT RH10 follow-up: a monthly-accrual type never needs a
            # pre-existing LeaveEntitlement.quota row (its entitlement is
            # lazily created at credit time) -- previewed separately below
            # ("droits acquis et engagements projetés"), never mixed into
            # the quota-counter checks that follow for quota-mode types.
            monthly_types_seen[leave_type.id] = leave_type
            key = (employee.id, leave_type.id)
            entry = monthly_projected_raw.setdefault(key, {"consumed": Decimal(0), "reserved": Decimal(0)})
            breakdown = compute_leave_request_days(
                leave_request, leave_request.start_date, leave_request.end_date,
                company_profile_id=company_profile_id,
            )
            if leave_request.status == StatusType.APPROVED.value:
                entry["consumed"] += breakdown.deductible
            else:
                entry["reserved"] += breakdown.deductible
            continue

        try:
            periods = resolve_cycle_periods_for_range(
                leave_type, leave_request.start_date, leave_request.end_date
            )
        except LeaveEntitlementError:
            preview.ambiguous_request_ids.append(leave_request.id)
            continue

        for period in periods:
            key = (employee.id, leave_type.id, period.id)
            seen_type_cycle.add((leave_type.id, period))
            entry = projected.setdefault(key, {"consumed": Decimal(0), "reserved": Decimal(0)})
            breakdown = compute_leave_request_days(
                leave_request, period.period_start, period.period_end,
                company_profile_id=company_profile_id,
            )
            if leave_request.status == StatusType.APPROVED.value:
                entry["consumed"] += breakdown.deductible
            else:
                entry["reserved"] += breakdown.deductible

    for leave_type_id, period in seen_type_cycle:
        preview.types_and_cycles.append({
            "leave_type_id": leave_type_id,
            "cycle_start": period.period_start.isoformat(),
            "cycle_end": period.period_end.isoformat(),
        })

    # LOT RH10 follow-up: EVERY monthly-accrual-configured type in this
    # company is previewed, not only ones with a live pending/approved
    # request -- a misconfigured type about to be activated should block
    # even with zero requests against it yet.
    monthly_type_filters = {"company_config__company_profile_id": company_profile_id}
    if solution is not None:
        monthly_type_filters["company_config__solution"] = solution
    for leave_type in LeaveType.objects.filter(**monthly_type_filters):
        if leave_type.effective_accrual_rate() is not None:
            monthly_types_seen[leave_type.id] = leave_type

    for leave_type in monthly_types_seen.values():
        rate = leave_type.effective_accrual_rate()
        effective_date = leave_type.effective_accrual_effective_date()
        preview.monthly_accrual_types.append({
            "leave_type_id": leave_type.id,
            "name": leave_type.name,
            "rate": float(rate) if rate is not None else None,
            "effective_date": effective_date.isoformat() if effective_date else None,
        })
        missing = []
        if rate is None:
            missing.append("rate")  # unreachable given the filter above, kept for clarity
        if effective_date is None:
            missing.append("accrual_effective_date")
        if missing:
            preview.monthly_missing_config_items.append({
                "leave_type_id": leave_type.id, "name": leave_type.name, "missing": missing,
            })

    if monthly_types_seen:
        company_config_for_tz = (
            CompanyConfig.objects.filter(company_profile_id=company_profile_id)
            .exclude(accrual_timezone__isnull=True).exclude(accrual_timezone="")
            .first()
            or CompanyConfig.objects.filter(company_profile_id=company_profile_id).first()
        )
        company_timezone = None
        if company_config_for_tz is None:
            preview.monthly_timezone_blocked = (
                "No CompanyConfig row exists for this company yet -- cannot resolve a "
                "processing timezone."
            )
        else:
            try:
                company_timezone = resolve_company_timezone(company_config_for_tz)
            except LeaveAccrualTimezoneError as exc:
                preview.monthly_timezone_blocked = str(exc)

        for (employee_id, leave_type_id), amounts in monthly_projected_raw.items():
            leave_type = monthly_types_seen[leave_type_id]
            if leave_type_id in {item["leave_type_id"] for item in preview.monthly_missing_config_items}:
                continue  # already reported as a type-level blocker above
            staff = Staff.objects.filter(pk=employee_id).first()
            if staff is None or staff.contract_start_date is None:
                preview.monthly_missing_contract_items.append({
                    "employee_id": employee_id, "leave_type_id": leave_type_id,
                })
                continue
            entry = {
                "consumed": float(amounts["consumed"]),
                "reserved": float(amounts["reserved"]),
                "acquired_through": None,
            }
            if company_timezone is not None:
                try:
                    last_year, last_month = last_complete_local_month(company_timezone)
                    entry["acquired_through"] = float(
                        _project_acquired_through(staff, leave_type, last_year, last_month)
                    )
                except LeaveAccrualError:
                    pass  # genuinely unresolvable (e.g. no rate history yet) -- left as None, not guessed
            preview.monthly_projected[f"{employee_id}:{leave_type_id}"] = entry

    existing_entitlements = {
        (e.employee_id, e.leave_type_id, e.cycle_period_id): e.quota
        for e in LeaveEntitlement.objects.filter(
            employee__user__company_id=company_profile_id
        )
    }

    # Proposed transition quotas: the employee's current annual_leaves_allowed,
    # attached ONLY to the chosen transition type's CURRENT cycle -- never
    # duplicated onto any other type, never treated as a full entitlement on
    # a different-length cycle.
    if transition_leave_type is not None:
        current_cycle = resolve_cycle_period(transition_leave_type, today)
        for staff in Staff.objects.filter(user__company_id=company_profile_id, user__is_deleted=False):
            if staff.annual_leaves_allowed is None:
                continue
            key = (staff.id, transition_leave_type.id, current_cycle.id)
            if key not in existing_entitlements:
                preview.proposed_transition_quotas[staff.id] = staff.annual_leaves_allowed
                existing_entitlements[key] = staff.annual_leaves_allowed

    for (employee_id, leave_type_id, cycle_period_id), amounts in projected.items():
        key = (employee_id, leave_type_id, cycle_period_id)
        quota = existing_entitlements.get(key)
        if quota is None:
            preview.missing_counter_items.append({
                "employee_id": employee_id, "leave_type_id": leave_type_id,
            })
            continue
        available = quota - amounts["consumed"] - amounts["reserved"]
        if available < 0:
            preview.negative_available_items.append({
                "employee_id": employee_id, "leave_type_id": leave_type_id,
                "available": float(available),
            })

    return preview


def activate(company_config, *, transition_leave_type_id, transition_quotas=None, activated_by=None):
    """Re-validates the exact same blocking conditions before committing --
    `transition_quotas` (optional {employee_id: Decimal}) lets RH confirm/
    override the proposed per-employee transition quota; any employee not
    present there falls back to their current `annual_leaves_allowed`."""
    preview = build_activation_preview(
        company_config.company_profile_id, transition_leave_type_id=transition_leave_type_id,
        solution=company_config.solution,
    )
    if not preview.can_activate:
        raise ActivationBlockedError(
            "Activation is blocked: ambiguous requests, missing counters, or a "
            "negative projected available balance remain unresolved."
        )

    transition_leave_type = LeaveType.objects.get(pk=transition_leave_type_id)
    transition_quotas = transition_quotas or {}
    today = date.today()

    with transaction.atomic():
        current_cycle = resolve_cycle_period(transition_leave_type, today)
        for staff in Staff.objects.filter(
            user__company_id=company_config.company_profile_id, user__is_deleted=False
        ):
            quota = transition_quotas.get(staff.id, staff.annual_leaves_allowed)
            if quota is None:
                continue
            # Never overwrite an entitlement RH may already have set up by
            # hand for this employee/type/cycle before activation.
            LeaveEntitlement.objects.get_or_create(
                employee=staff, leave_type=transition_leave_type, cycle_period=current_cycle,
                defaults={
                    "quota": quota,
                    "last_adjustment_reason": "Transition from the legacy global annual leave balance.",
                    "last_adjusted_by": activated_by,
                },
            )

        company_config.leave_entitlement_mode_active = True
        company_config.leave_entitlement_activated_at = timezone.now()
        company_config.leave_entitlement_transition_leave_type = transition_leave_type
        company_config.save(update_fields=[
            "leave_entitlement_mode_active",
            "leave_entitlement_activated_at",
            "leave_entitlement_transition_leave_type",
        ])

    return preview
