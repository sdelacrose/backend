"""LOT RH 9: per-employee/per-leave-type/per-cycle entitlements ("droits"),
the mechanism a company/solution can explicitly opt into
(`CompanyConfig.leave_entitlement_mode_active`) to replace the historical
global `Staff.annual_leaves_allowed` balance for its regular (non-WFH) leave
types. Work-from-home is explicitly never touched here -- it stays on the
existing `services.leave_balance` global mechanism regardless of activation.

Reuses `compute_leave_request_days`/`get_organization_working_days` from
`services/leave_balance.py` for every day-counting need -- no new duration/
deduction formula is introduced in this module.

LOT RH 10 adds `entitlement_acquired`/`entitlement_available`, the single
pair of mode-aware helpers every "available" computation in this module (and
in `views.leave_entitlement.adjust`) now goes through, instead of each call
site inlining `quota - consumed - reserved`.
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Sum

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave_entitlement import (
    LeaveCyclePeriod,
    LeaveEntitlement,
)
from human_resource_management.services.leave_balance import (
    compute_leave_request_days,
    get_organization_working_days,
)
from models.models import CompanyConfig


def entitlement_acquired(entitlement):
    """Monthly-accrual mode only: sum of this entitlement's already-POSTED
    `LeaveAccrualCredit` rows -- never a projection of future credits (LOT
    RH 10: "aucune anticipation"). None for quota-mode rows, where "acquired"
    is not a meaningful concept."""
    if entitlement.mode != LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
        return None
    total = entitlement.accrual_credits.aggregate(total=Sum("quantity"))["total"]
    return total if total is not None else Decimal("0")


def entitlement_last_processed_at(entitlement):
    """Monthly-accrual mode only: the period_end of the most recently posted
    credit, or None if none has been posted yet."""
    if entitlement.mode != LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
        return None
    latest = entitlement.accrual_credits.order_by("-period_end").first()
    return latest.period_end if latest else None


def entitlement_available(entitlement, consumed, reserved):
    """available = quota - consumed - reserved (quota mode), or
    acquired + adjustment - consumed - reserved (monthly-accrual mode, where
    `quota` is repurposed as the net manual adjustment -- see
    `LeaveEntitlement`'s docstring)."""
    if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
        return entitlement_acquired(entitlement) + entitlement.quota - consumed - reserved
    return entitlement.quota - consumed - reserved


class LeaveEntitlementError(Exception):
    """Raised when a cycle/entitlement cannot be resolved or is insufficient.
    Callers in serializers/views convert this to the usual
    `rest_framework.serializers.ValidationError`/`Response` as appropriate --
    kept as a plain exception here so this module has no DRF dependency."""


def is_entitlement_mode_active_for_employee(employee):
    """True iff ANY CompanyConfig row for this employee's company has
    activated the new mechanism. A company's leave data isn't tagged by
    solution anywhere (LeaveRequest/Staff carry no solution field, and the
    existing global balance check never filters by it either), so this
    deliberately checks across every CompanyConfig row for the company
    rather than resolving "the" active solution via a license lookup."""
    company_profile_id = getattr(employee.user, "company_id", None)
    if not company_profile_id:
        return False
    return CompanyConfig.objects.filter(
        company_profile_id=company_profile_id,
        leave_entitlement_mode_active=True,
    ).exists()


def _safe_date(year, month, day):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def resolve_cycle_period(leave_type, reference_date):
    """The concrete [period_start, period_end] cycle instance of `leave_type`
    covering `reference_date`, resolved from its configured recurring
    month/day pattern (`effective_value("cycle_start_date"/"cycle_end_date")`)
    -- never an implicit January-December assumption, and never moving an
    already-created period if the type's configuration changes later (a
    changed pattern simply resolves to, and creates, a DIFFERENT period for
    future lookups; see `LeaveCyclePeriod`'s own docstring)."""
    cycle_start = leave_type.effective_value("cycle_start_date")
    cycle_end = leave_type.effective_value("cycle_end_date")
    if cycle_start is None or cycle_end is None:
        raise LeaveEntitlementError(
            f"Leave type '{leave_type.name}' has no configured cycle start/end date."
        )
    if cycle_end < cycle_start:
        raise LeaveEntitlementError(
            f"Leave type '{leave_type.name}' has an invalid cycle: end date is before start date."
        )
    # The recurring pattern is the (month, day) of each bound, not a fixed
    # day-count duration -- a fixed timedelta would drift by a day whenever
    # the configured year and a resolved year disagree on being a leap year
    # (e.g. a Jan1-Dec31 cycle configured in a leap year must still resolve
    # to exactly Jan1-Dec31 of a non-leap year, not Jan1-Jan1). If the
    # configured end's (month, day) falls before the start's within one
    # calendar year, the cycle is understood to cross into the next year
    # (e.g. June 1 - May 31); otherwise it stays within the same year
    # (e.g. Jan 1 - Dec 31).
    end_crosses_year = (cycle_end.month, cycle_end.day) < (cycle_start.month, cycle_start.day)

    for candidate_year in (reference_date.year - 1, reference_date.year, reference_date.year + 1):
        candidate_start = _safe_date(candidate_year, cycle_start.month, cycle_start.day)
        end_year = candidate_year + 1 if end_crosses_year else candidate_year
        candidate_end = _safe_date(end_year, cycle_end.month, cycle_end.day)
        if candidate_start <= reference_date <= candidate_end:
            existing = LeaveCyclePeriod.objects.filter(
                leave_type=leave_type, period_start=candidate_start, period_end=candidate_end,
            ).first()
            if existing is not None:
                return existing
            # LOT RH 9 follow-up: a changed cycle configuration must never
            # create a period overlapping one that already exists for this
            # type -- that would silently double-attach a request/entitlement
            # resolved under the old pattern to a second, conflicting period
            # under the new one. Refuse clearly instead; the already-created
            # period is never moved or deleted (see LeaveCyclePeriod's own
            # docstring) -- resolving the incompatibility is left to RH
            # (e.g. reverting the config change, or waiting until the
            # existing period has fully elapsed).
            conflicting = LeaveCyclePeriod.objects.filter(
                leave_type=leave_type,
                period_start__lte=candidate_end,
                period_end__gte=candidate_start,
            ).first()
            if conflicting is not None:
                raise LeaveEntitlementError(
                    f"Leave type '{leave_type.name}' cycle configuration now resolves "
                    f"{candidate_start}-{candidate_end}, which overlaps an existing cycle "
                    f"period ({conflicting.period_start}-{conflicting.period_end}). The "
                    f"existing period is preserved as-is; this configuration change cannot "
                    f"be applied for dates in this range until the conflict is resolved."
                )
            period = LeaveCyclePeriod.objects.create(
                leave_type=leave_type, period_start=candidate_start, period_end=candidate_end,
            )
            return period
    raise LeaveEntitlementError(
        f"Could not resolve a leave cycle for '{leave_type.name}' covering {reference_date}."
    )


def resolve_cycle_periods_for_range(leave_type, start_date, end_date):
    """Ordered list of every cycle period a [start_date, end_date] window
    overlaps -- a request straddling two (or, rarely, more) cycles is
    represented by several entries here, one per cycle it touches, each to
    be checked/clipped independently (LOT RH 9: "demande répartie entre les
    cycles concernés")."""
    periods = []
    cursor = start_date
    guard = 0
    while cursor <= end_date:
        period = resolve_cycle_period(leave_type, cursor)
        periods.append(period)
        cursor = period.period_end + timedelta(days=1)
        guard += 1
        if guard > 10:
            raise LeaveEntitlementError(
                f"Leave type '{leave_type.name}': too many cycle periods resolved for one "
                f"request -- check its cycle configuration."
            )
    return periods


def sum_existing_requests(
    employee, leave_type, period, *, status, exclude_request_id, company_profile_id, working_days
):
    queryset = LeaveRequest.objects.filter(
        employee=employee, leave_type=leave_type, status=status,
        start_date__lte=period.period_end, end_date__gte=period.period_start,
    )
    if exclude_request_id is not None:
        queryset = queryset.exclude(pk=exclude_request_id)
    total = Decimal(0)
    for leave_request in queryset:
        breakdown = compute_leave_request_days(
            leave_request, period.period_start, period.period_end,
            company_profile_id=company_profile_id, working_days=working_days,
        )
        total += breakdown.deductible
    return total


@dataclass
class CyclePortionBreakdown:
    cycle_period: LeaveCyclePeriod
    quota: "Decimal | None"
    consumed: Decimal
    reserved: Decimal
    available: "Decimal | None"
    requested: Decimal
    mode: "str | None" = None
    acquired: "Decimal | None" = None
    adjustment: "Decimal | None" = None
    last_processed_at: "date | None" = None
    # LOT RH11: own vs. carried-over droits, distinct -- own_available is
    # None when no entitlement row exists (same "absent counter" semantics
    # as `quota`); carry_over_available/carry_over_expires_on are only
    # populated when at least one active (non-expired) carry-over exists.
    # `closed` reflects a persisted `LeaveCycleClosure` -- read-only, never
    # gates consultation itself.
    own_available: "Decimal | None" = None
    carry_over_available: "Decimal | None" = None
    carry_over_expires_on: "date | None" = None
    closed: bool = False


def get_entitlement_breakdown(
    employee, leave_type, start_date, end_date, *, exclude_request_id=None
):
    """Per-cycle breakdown of `leave_type` for `employee` over
    [start_date, end_date]: quota (None if no entitlement row exists for
    that cycle -- an absent counter, distinct from an explicit zero quota),
    already-consumed (approved) and reserved (pending) days, and available.
    For a monthly-accrual entitlement, `quota` instead carries the field's
    repurposed meaning (net manual adjustment -- also exposed as
    `adjustment`), `acquired` carries the posted-credits total, and
    `available = acquired + adjustment - consumed - reserved`; `mode` and
    `last_processed_at` are populated for both modes (None for quota mode).
    `requested` is the portion of [start_date, end_date] this specific
    window itself represents in that cycle (0 if the probe used is
    read-only/reporting-only, i.e. not an actual pending/approved request).
    Returns `[]` if the type does not consume balance (no reservation/
    deduction concept applies to it)."""
    if not leave_type.effective_value("consumes_balance"):
        return []
    company_profile_id = employee.user.company_id
    working_days = get_organization_working_days(company_profile_id)
    periods = resolve_cycle_periods_for_range(leave_type, start_date, end_date)
    probe = LeaveRequest(
        employee=employee, leave_type=leave_type, type=leave_type.name,
        start_date=start_date, end_date=end_date,
    )
    results = []
    for period in periods:
        breakdown = compute_leave_request_days(
            probe, period.period_start, period.period_end,
            company_profile_id=company_profile_id, working_days=working_days,
        )
        entitlement = LeaveEntitlement.objects.filter(
            employee=employee, leave_type=leave_type, cycle_period=period
        ).first()
        consumed = sum_existing_requests(
            employee, leave_type, period, status=StatusType.APPROVED.value,
            exclude_request_id=exclude_request_id, company_profile_id=company_profile_id,
            working_days=working_days,
        )
        reserved = sum_existing_requests(
            employee, leave_type, period, status=StatusType.PENDING.value,
            exclude_request_id=exclude_request_id, company_profile_id=company_profile_id,
            working_days=working_days,
        )
        own_available = carry_over_available = carry_over_expires_on = None
        closed = False
        if entitlement is None:
            quota = acquired = adjustment = available = last_processed_at = None
            mode = None
        else:
            # LOT RH11: local import, see _check_type_cumulative_or_raise.
            from human_resource_management.services.leave_carryover import (
                entitlement_available_with_carryover, get_active_carry_overs,
                has_any_carry_over_history,
            )
            closed = hasattr(entitlement, "closure") or LeaveEntitlement.objects.filter(
                pk=entitlement.pk, closure__isnull=False
            ).exists()
            active_carry_overs = get_active_carry_overs(entitlement, date.today())
            if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
                quota = entitlement.quota
                acquired = entitlement_acquired(entitlement)
                adjustment = entitlement.quota
                last_processed_at = entitlement_last_processed_at(entitlement)
            else:
                quota = entitlement.quota
                acquired = adjustment = last_processed_at = None
            mode = entitlement.mode
            # Gating uses the PERMANENT question (has this entitlement ever
            # had a carry-over) -- not just "still active today" -- so an
            # existing request's frozen allocation keeps being excluded
            # from "own" long after its own carry-over is fully resolved
            # (see has_any_carry_over_history's docstring).
            if has_any_carry_over_history(entitlement):
                carry_breakdown = entitlement_available_with_carryover(
                    entitlement, period, exclude_request_id=exclude_request_id,
                )
                own_available = carry_breakdown.own_available
                carry_over_available = carry_breakdown.carry_over_available
                carry_over_expires_on = active_carry_overs[0].expires_on if active_carry_overs else None
                available = carry_breakdown.available
            else:
                available = entitlement_available(entitlement, consumed, reserved)
                own_available = available
        results.append(CyclePortionBreakdown(
            cycle_period=period, quota=quota, consumed=consumed, reserved=reserved,
            available=available, requested=breakdown.deductible, mode=mode,
            acquired=acquired, adjustment=adjustment, last_processed_at=last_processed_at,
            own_available=own_available, carry_over_available=carry_over_available,
            carry_over_expires_on=carry_over_expires_on, closed=closed,
        ))
    return results


def check_entitlement_or_raise_for_items(employee, items, *, exclude_request_id=None):
    """Enforces availability for a batch of unsaved LeaveRequest-like probes
    (`items`), ALL belonging to non-WFH, consumption-checked leave types --
    callers (serializers.leave._check_balance_or_raise) are responsible for
    routing WFH items and non-consuming-type items elsewhere before calling
    this. Items of the SAME leave type are summed together per cycle before
    comparing against that cycle's entitlement (LOT RH 9: "le batch vérifie
    le cumul"); items of different types are checked independently against
    their own type's entitlement -- never pooled."""
    by_type = {}
    for item in items:
        leave_type = getattr(item, "leave_type", None)
        if leave_type is None:
            raise LeaveEntitlementError(
                "No leave entitlement counter configured for this request. Please contact HR."
            )
        by_type.setdefault(leave_type.id, {"leave_type": leave_type, "items": []})["items"].append(item)

    for entry in by_type.values():
        _check_type_cumulative_or_raise(
            employee, entry["leave_type"], entry["items"], exclude_request_id=exclude_request_id
        )


def _check_type_cumulative_or_raise(employee, leave_type, items, *, exclude_request_id):
    company_profile_id = employee.user.company_id
    working_days = get_organization_working_days(company_profile_id)

    periods_by_id = {}
    for item in items:
        for period in resolve_cycle_periods_for_range(leave_type, item.start_date, item.end_date):
            periods_by_id[period.id] = period

    for period in periods_by_id.values():
        requested = Decimal(0)
        for item in items:
            breakdown = compute_leave_request_days(
                item, period.period_start, period.period_end,
                company_profile_id=company_profile_id, working_days=working_days,
            )
            requested += breakdown.deductible
        if requested <= 0:
            continue

        entitlement = LeaveEntitlement.objects.filter(
            employee=employee, leave_type=leave_type, cycle_period=period
        ).first()
        if entitlement is None:
            raise LeaveEntitlementError(
                f"No leave entitlement configured for '{leave_type.name}' covering "
                f"{period.period_start}-{period.period_end}. Please contact HR."
            )

        # LOT RH11: local imports -- leave_carryover.py imports FROM this
        # module at its own top level, so importing it back here at
        # module scope would be circular.
        from human_resource_management.services.leave_carryover import (
            entitlement_available_with_carryover,
            finalize_due_carryovers,
            has_any_carry_over_history,
            raise_if_cycle_closed,
        )
        # LOT RH11: finalize any overdue carry-over BEFORE checking/writing
        # anything against this entitlement -- same per-employee lock the
        # caller (create()/update()/adjust()) already holds at this point.
        finalize_due_carryovers(entitlement)
        raise_if_cycle_closed(entitlement)

        if has_any_carry_over_history(entitlement):
            period_items = [
                item for item in items
                if compute_leave_request_days(
                    item, period.period_start, period.period_end,
                    company_profile_id=company_profile_id, working_days=working_days,
                ).deductible > 0
            ]
            breakdown = entitlement_available_with_carryover(
                entitlement, period, exclude_request_id=exclude_request_id,
                probes=period_items,
            )
            available = breakdown.available
        else:
            consumed = sum_existing_requests(
                employee, leave_type, period, status=StatusType.APPROVED.value,
                exclude_request_id=exclude_request_id, company_profile_id=company_profile_id,
                working_days=working_days,
            )
            reserved = sum_existing_requests(
                employee, leave_type, period, status=StatusType.PENDING.value,
                exclude_request_id=exclude_request_id, company_profile_id=company_profile_id,
                working_days=working_days,
            )
            available = entitlement_available(entitlement, consumed, reserved)
        if requested > available:
            raise LeaveEntitlementError(
                f"Insufficient leave entitlement for '{leave_type.name}' covering "
                f"{period.period_start}-{period.period_end}: available {available}, "
                f"requested {requested}."
            )
