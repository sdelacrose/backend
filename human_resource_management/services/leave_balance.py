"""Centralized leave-day/balance calculation (LOT RH 2).

Single source of truth for "how many days does this leave request represent",
replacing the three previously-duplicated implementations in
serializers/leave.py, serializers/staff.py and views/staff.py (all of which
called human_resource_management.utils.calculate_leave_days directly and
re-implemented the same year-filtering and Sick/Unpaid exclusion).

Two concepts are kept distinct throughout, per the lot's own requirement:
  - "duration"    -- the absence's length, useful for reporting, independent of
                     whether it consumes any balance.
  - "deductible"  -- the portion of that duration that counts against the
                     employee's balance (zero when the resolved LeaveType has
                     consumes_balance=False).

Entitlements (Staff.annual_leaves_allowed / wfh_leaves_allowed) are read as-is;
nothing here creates, accrues, renews, or carries them over. LeaveType.cycle_*
fields are deliberately never consulted for period boundaries -- the annual
calendar-year period used today is preserved (see get_employee_leave_summary).
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from ecpmpapp.constants import LeaveType as LegacyLeaveType
from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from models.models import GlobalConfig, LeaveType

# Legacy hardcoded exclusion, preserved verbatim for any request whose type
# string cannot be resolved to a configured LeaveType row (see resolve_leave_type) --
# this is exactly the condition the three duplicated implementations already used.
_LEGACY_NON_CONSUMING_TYPES = {LegacyLeaveType.SICK.value, LegacyLeaveType.UNPAID.value}

# Fallback when no GlobalConfig row exists yet for the company (mirrors
# models.default_working_days in admin-server-models).
_DEFAULT_WORKING_DAYS = frozenset({1, 2, 3, 4, 5})

RESOLVED = "resolved"
UNRESOLVED_NO_MATCH = "unresolved_no_match"
UNRESOLVED_AMBIGUOUS = "unresolved_ambiguous"


def get_organization_working_days(company_profile_id):
    """ISO weekday numbers (1=Monday..7=Sunday) configured for this company's
    organization (GlobalConfig.working_days), or the Mon-Fri default when no
    GlobalConfig row exists yet."""
    if not company_profile_id:
        return _DEFAULT_WORKING_DAYS
    global_config = GlobalConfig.objects.filter(
        company_profile_id=company_profile_id
    ).only("working_days").first()
    if global_config is None or not global_config.working_days:
        return _DEFAULT_WORKING_DAYS
    return frozenset(global_config.working_days)


def resolve_leave_type(type_name, company_profile_id, solution=None):
    """Resolve a LeaveRequest.type string to its company-scope LeaveType row,
    within the company/organization scope applicable to this employee.

    Returns (leave_type_or_None, resolution) where resolution is one of
    RESOLVED / UNRESOLVED_NO_MATCH / UNRESOLVED_AMBIGUOUS. Never guesses
    between several same-named types -- more than one match is reported as
    ambiguous, not silently picked.
    """
    if not type_name or not company_profile_id:
        return None, UNRESOLVED_NO_MATCH
    queryset = LeaveType.objects.filter(
        company_config__company_profile_id=company_profile_id,
        name=type_name,
    )
    if solution:
        queryset = queryset.filter(company_config__solution=solution)
    matches = list(queryset[:2])
    if len(matches) == 1:
        return matches[0], RESOLVED
    if len(matches) > 1:
        return None, UNRESOLVED_AMBIGUOUS
    return None, UNRESOLVED_NO_MATCH


def _count_days_in_range(lo, hi, deduction_mode, working_days):
    if lo > hi:
        return Decimal(0)
    if deduction_mode == LeaveType.DEDUCTION_CALENDAR_DAYS:
        return Decimal((hi - lo).days + 1)
    # Working-days mode (also the legacy/unresolved default).
    count = 0
    current = lo
    one_day = timedelta(days=1)
    while current <= hi:
        if current.isoweekday() in working_days:
            count += 1
        current += one_day
    return Decimal(count)


def _half_day_duration(day, deduction_mode, working_days):
    """A half-day names one specific date already chosen by the user, so
    calendar-days mode always counts it as 0.5 (LOT RH 3A requirement).
    Working-days mode still has to respect the org's configured working
    days, same as a full-day range would -- a half-day on a date outside
    them is 0, not 0.5 (this was the one gap LOT RH 3A found: the duration
    was previously hardcoded to 0.5 regardless of mode or weekday)."""
    if deduction_mode == LeaveType.DEDUCTION_WORKING_DAYS and day.isoweekday() not in working_days:
        return Decimal(0)
    return Decimal("0.5")


@dataclass
class LeaveDayBreakdown:
    duration: Decimal
    deductible: Decimal
    resolution: str
    leave_type_id: "int | None" = None


def compute_leave_request_days(
    leave_request,
    period_start,
    period_end,
    *,
    company_profile_id,
    solution=None,
    working_days=None,
):
    """Duration/deductible days of `leave_request`, clipped to
    [period_start, period_end] (inclusive both ends). Pass period_start/end
    equal to the request's own start/end to get its full, unclipped days.

    Date bounds are always inclusive: a single-day request (start == end)
    counts as one day when deductible, matching existing submission rules.

    If `leave_request.leave_type_id` is set (LOT RH 3 -- a request linked to
    its LeaveType by stable id), that FK is used directly and name-based
    `resolve_leave_type` is never consulted -- the whole point of the link is
    that it can never become ambiguous or drift if the type is later renamed.
    Historical rows without the FK keep resolving by name as before.

    A half-day request (`is_half_day=True`) counts as 0.5 in calendar-days
    mode unconditionally; in working-days mode it's 0.5 only if its one date
    falls on a configured working day, else 0 (see `_half_day_duration`) --
    the same org-config respect a full-day range already gets.
    """
    lo = max(leave_request.start_date, period_start)
    hi = min(leave_request.end_date, period_end)
    if lo > hi:
        return LeaveDayBreakdown(Decimal(0), Decimal(0), UNRESOLVED_NO_MATCH, None)

    if working_days is None:
        working_days = get_organization_working_days(company_profile_id)

    if getattr(leave_request, "leave_type_id", None):
        leave_type, resolution = leave_request.leave_type, RESOLVED
    else:
        leave_type, resolution = resolve_leave_type(
            leave_request.type, company_profile_id, solution
        )

    is_half_day = bool(getattr(leave_request, "is_half_day", False))

    if resolution == RESOLVED:
        consumes_balance = bool(leave_type.effective_value("consumes_balance"))
        deduction_mode = leave_type.effective_value("deduction_mode")
        if is_half_day:
            duration = _half_day_duration(lo, deduction_mode, working_days)
        else:
            duration = _count_days_in_range(lo, hi, deduction_mode, working_days)
        deductible = duration if consumes_balance else Decimal(0)
        return LeaveDayBreakdown(duration, deductible, resolution, leave_type.id)

    # Unresolved (no match, or ambiguous): preserve the exact pre-existing
    # behavior -- working-days count, deductible unless the legacy Sick/Unpaid
    # exclusion applies -- rather than inventing a new default for data this
    # lot was told not to reinterpret.
    if is_half_day:
        duration = _half_day_duration(lo, LeaveType.DEDUCTION_WORKING_DAYS, working_days)
    else:
        duration = _count_days_in_range(lo, hi, LeaveType.DEDUCTION_WORKING_DAYS, working_days)
    deductible = Decimal(0) if leave_request.type in _LEGACY_NON_CONSUMING_TYPES else duration
    return LeaveDayBreakdown(duration, deductible, resolution, None)


@dataclass
class LeaveSummary:
    period_start: date
    period_end: date
    leave_taken: Decimal = Decimal(0)
    leave_balance: "Decimal | None" = None
    wfh_taken: Decimal = Decimal(0)
    wfh_balance: "Decimal | None" = None
    unresolved_request_ids: list = field(default_factory=list)
    ambiguous_request_ids: list = field(default_factory=list)


def get_employee_leave_summary(
    employee, period_start, period_end, *, solution=None, exclude_request_id=None
):
    """Approved-only leave taken/balance for `employee` over
    [period_start, period_end] (inclusive). Requests are selected by date
    INTERSECTION with the period (not an exact-year match), so a request
    spanning two periods is included in both -- each occurrence counting only
    its own portion of that period, via compute_leave_request_days. Two
    disjoint periods (e.g. two different calendar years) can therefore never
    double-count the same days between them.

    `exclude_request_id`: omit this request from the aggregation -- used when
    checking the balance *for* a request being created/approved, so its own
    (not-yet-APPROVED) row can never be counted against itself even if the
    caller's own status-filter timing were to change later (LOT RH 2A).

    WFH and regular leave are kept on their own separate budgets
    (wfh_leaves_allowed vs annual_leaves_allowed), matching the existing,
    untouched Staff fields -- this function does not alter or merge them.
    """
    company_profile_id = employee.user.company_id
    working_days = get_organization_working_days(company_profile_id)

    requests = employee.leave_requests.filter(
        status=StatusType.APPROVED.value,
        start_date__lte=period_end,
        end_date__gte=period_start,
    )
    if exclude_request_id is not None:
        requests = requests.exclude(pk=exclude_request_id)

    summary = LeaveSummary(period_start=period_start, period_end=period_end)

    for leave_request in requests:
        breakdown = compute_leave_request_days(
            leave_request,
            period_start,
            period_end,
            company_profile_id=company_profile_id,
            solution=solution,
            working_days=working_days,
        )
        if breakdown.resolution == UNRESOLVED_AMBIGUOUS:
            summary.ambiguous_request_ids.append(leave_request.id)
        elif breakdown.resolution == UNRESOLVED_NO_MATCH:
            summary.unresolved_request_ids.append(leave_request.id)

        if leave_request.is_working_from_home:
            summary.wfh_taken += breakdown.deductible
        else:
            summary.leave_taken += breakdown.deductible

    if employee.annual_leaves_allowed is not None:
        summary.leave_balance = Decimal(employee.annual_leaves_allowed) - summary.leave_taken
    if employee.wfh_leaves_allowed is not None:
        summary.wfh_balance = Decimal(employee.wfh_leaves_allowed) - summary.wfh_taken

    return summary


def current_year_bounds(today=None):
    today = today or date.today()
    return date(today.year, 1, 1), date(today.year, 12, 31)
