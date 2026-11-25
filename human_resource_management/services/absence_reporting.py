"""Absence reporting (LOT RH 8): detailed Leaves-tab export rows and the
approved-absence recap. Reuses the day-counting machinery already built in
services/leave_balance.py (compute_leave_request_days, resolve_leave_type,
get_organization_working_days) -- no new day-counting formula is introduced
here. Never reads/returns `reason` or attachments: only the same fields
serialize_leave_for_calendar already treats as non-confidential (identity,
type, dates, status), plus the sub-type and regularization flag for the
detailed export.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Q

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.services.leave_balance import (
    compute_leave_request_days,
    get_organization_working_days,
)
from models.models import Staff


def resolve_scoped_employees(company_id, employee_ids=None):
    """All Staff rows of `company_id` ("Tous" = every authorized collaborator
    of the company); `employee_ids` (optional iterable of int) narrows the
    set. Returns (queryset, unknown_ids) -- `unknown_ids` holds any requested
    id that does not resolve to a staff member of THIS company, so the
    caller can refuse the whole request rather than silently drop them."""
    queryset = Staff.objects.filter(user__company_id=company_id, user__is_deleted=False)
    if employee_ids is None:
        return queryset, []
    wanted = set(employee_ids)
    found_ids = set(queryset.filter(id__in=wanted).values_list("id", flat=True))
    return queryset.filter(id__in=found_ids), sorted(wanted - found_ids)


@dataclass
class DetailedLeaveRow:
    employee_id: int
    employee_name: str
    type_label: str
    sub_type_label: str
    start_date: "date"
    end_date: "date"
    is_half_day: bool
    half_day_period: "str | None"
    status: str
    is_regularization: bool
    duration: Decimal
    deductible: Decimal


def get_detailed_leave_rows(queryset, period_start, period_end, *, company_profile_id, solution=None):
    """One row per LeaveRequest in `queryset` whose duration/deductible is
    clipped to [period_start, period_end] -- a request straddling the filter
    bounds only ever contributes the portion inside them (LOT RH 8's own
    requirement), via compute_leave_request_days, never a re-implemented
    day count."""
    working_days = get_organization_working_days(company_profile_id)
    rows = []
    for leave_request in queryset.select_related(
        "employee__user", "leave_sub_type"
    ).order_by("employee__user__first_name", "employee__user__last_name", "start_date"):
        breakdown = compute_leave_request_days(
            leave_request,
            period_start,
            period_end,
            company_profile_id=company_profile_id,
            solution=solution,
            working_days=working_days,
        )
        rows.append(
            DetailedLeaveRow(
                employee_id=leave_request.employee_id,
                employee_name=leave_request.employee.user.full_name,
                type_label=leave_request.type or "",
                sub_type_label=(
                    leave_request.leave_sub_type.name if leave_request.leave_sub_type_id else ""
                ),
                start_date=leave_request.start_date,
                end_date=leave_request.end_date,
                is_half_day=leave_request.is_half_day,
                half_day_period=leave_request.half_day_period,
                status=leave_request.status,
                is_regularization=leave_request.is_regularization,
                duration=breakdown.duration,
                deductible=breakdown.deductible,
            )
        )
    return rows


@dataclass
class EmployeeAbsenceRecap:
    employee_id: int
    employee_name: str
    types: list = field(default_factory=list)  # unique type labels, first-seen order
    total_days: Decimal = Decimal(0)
    days_by_type: dict = field(default_factory=dict)
    global_balance: "dict | None" = None
    # LOT RH 9: per-type, current-cycle balances once the company has
    # activated the new entitlement mechanism -- attached by the view, never
    # computed here (this module stays duration-only, see the module
    # docstring's own duration/deduction distinction).
    entitlement_balances: "list | None" = None


def get_approved_absence_recap(
    staff_queryset, period_start, period_end, *, company_profile_id, solution=None,
    allowed_leave_type_configs=None,
):
    """One row per employee (from `staff_queryset`) with at least one
    APPROVED leave overlapping [period_start, period_end] -- an employee with
    no approved absence on the period is omitted entirely (never a zero
    row). `total_days`/`days_by_type` are always the ABSENCE DURATION (never
    the balance-deduction quantity -- see leave_balance.py's own duration vs
    deductible distinction; Sick/Unpaid-style non-consuming types still count
    here as full absence days). Types are bucketed by the request's own raw
    `type` label, so an unresolved/ambiguous historical type is never merged
    into an unrelated one and never loses its days -- it simply keeps its own
    label, exactly like any other type."""
    working_days = get_organization_working_days(company_profile_id)
    by_employee: "dict[int, EmployeeAbsenceRecap]" = {}
    requests = LeaveRequest.objects.filter(
        employee__in=staff_queryset,
        status=StatusType.APPROVED.value,
        start_date__lte=period_end,
        end_date__gte=period_start,
    )
    # LOT RH13 follow-up (solution isolation): a legacy row with no stable
    # `leave_type` FK can never be attributed to a solution and is always
    # kept -- only a row whose leave_type belongs to a DIFFERENT solution's
    # CompanyConfig is excluded. A queryset/list of allowed CompanyConfig
    # rows (not a single solution string) so a COLLABORATIVE_SOLUTION bundle
    # of several solutions-at-once is handled correctly, same as every
    # other RH screen's own `_company_configs_for_request`.
    if allowed_leave_type_configs is not None:
        requests = requests.filter(
            Q(leave_type__isnull=True) | Q(leave_type__company_config__in=allowed_leave_type_configs)
        )
    requests = requests.select_related("employee__user").order_by("employee_id", "start_date")
    for leave_request in requests:
        breakdown = compute_leave_request_days(
            leave_request,
            period_start,
            period_end,
            company_profile_id=company_profile_id,
            solution=solution,
            working_days=working_days,
        )
        if breakdown.duration <= 0:
            continue
        row = by_employee.get(leave_request.employee_id)
        if row is None:
            row = EmployeeAbsenceRecap(
                employee_id=leave_request.employee_id,
                employee_name=leave_request.employee.user.full_name,
            )
            by_employee[leave_request.employee_id] = row
        label = leave_request.type or ""
        if label not in row.days_by_type:
            row.days_by_type[label] = Decimal(0)
            row.types.append(label)
        row.days_by_type[label] += breakdown.duration
        row.total_days += breakdown.duration
    return list(by_employee.values())
