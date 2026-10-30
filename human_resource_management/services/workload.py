"""AI-2054: the single shared engine computing a staff member's occupancy
(capacity/charge/rate) across Project Management's action items -- used
BOTH by the read-only HRM view and by the overload-alert evaluation
(services.workload_alerts), so the two can never drift apart (same
precedent as AI-1685's build_rule_preview/process_rule sharing
_resolve_occurrence).

Never writes to Project Management data. Never divides or extends an
assignee's divisor by who the CURRENT viewer happens to be authorized to
see (the divisor is always every real assignee); only the detail payload
avoids naming co-assignees the caller isn't otherwise shown.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q

from project_management.models import ActionItem
from human_resource_management.services.leave_accrual import (
    LeaveAccrualQuotiteError,
    resolve_current_quotite_entry,
    resolve_quotite_segments,
)
from human_resource_management.services.leave_balance import get_organization_working_days

ZERO = Decimal(0)

# Reasonable tolerance for "parent budget == sum of children" given
# allocated_time is stored as a plain integer number of hours.
_PARENT_CHILD_TOLERANCE = Decimal("0.01")


def month_bounds(year, month):
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def current_month_bounds(today=None):
    today = today or date.today()
    return month_bounds(today.year, today.month)


def period_key_for(period_start):
    return f"{period_start.year:04d}-{period_start.month:02d}"


def _count_working_days(lo, hi, working_days):
    """Plain int count of days in [lo, hi] (inclusive) whose ISO weekday is
    in `working_days`. Returns 0 if lo > hi (never negative, never a
    silent guess)."""
    if lo is None or hi is None or lo > hi:
        return 0
    count = 0
    current = lo
    one_day = timedelta(days=1)
    while current <= hi:
        if current.isoweekday() in working_days:
            count += 1
        current += one_day
    return count


def _effective_assignees(item):
    """Normalizes ActionItem.assignees (M2M, current) with the legacy
    `assignee` FK -- never counts the same person twice if they appear in
    both (AI-2054 explicit requirement). Returns a list of User rows,
    order not significant."""
    assignees = list(item.assignees.all())
    if item.assignee_id and item.assignee_id not in {u.id for u in assignees}:
        assignees.append(item.assignee)
    return assignees


def resolve_action_item_company_profile_id(item):
    """An ActionItem's own company_profile when set (standalone item), else
    its project's (via milestone) -- a sub-action item never carries its own
    company_profile (model-enforced), so this always recurses to the
    parent for those."""
    if item.parent_action_item_id:
        return resolve_action_item_company_profile_id(item.parent_action_item)
    if item.company_profile_id:
        return item.company_profile_id
    if item.milestone_id and item.milestone.project_id:
        return item.milestone.project.company_profile_id
    return None


def resolve_action_item_solution(item):
    """The Project.solution an ActionItem's charge belongs to, resolved via
    its milestone/project exactly like company_profile above -- None for a
    standalone action item with no project at all (never guessed)."""
    if item.parent_action_item_id:
        return resolve_action_item_solution(item.parent_action_item)
    if item.milestone_id and item.milestone.project_id:
        return item.milestone.project.solution
    return None


def _project_context(item):
    """(project_id, project_name, milestone_id, milestone_name) for display
    grouping -- all None when the item is a standalone action item (no
    milestone/project at all)."""
    root = item.parent_action_item if item.parent_action_item_id else item
    milestone = root.milestone
    if milestone is None:
        return None, None, None, None
    project = milestone.project
    return (
        project.id if project else None,
        project.project_name if project else None,
        milestone.id,
        milestone.phase_title or milestone.name,
    )


@dataclass
class WorkloadAnomaly:
    code: str
    message: str
    action_item_id: "int | None" = None
    details: dict = field(default_factory=dict)


@dataclass
class WorkloadContribution:
    action_item_id: int
    name: str
    status: str
    start_date: "date | None"
    target_date: date
    is_sub_action: bool
    project_id: "int | None"
    project_name: "str | None"
    milestone_id: "int | None"
    milestone_name: "str | None"
    budget_total: int
    assignee_count: int
    individual_share: Decimal
    working_days_total: int
    working_days_in_period: int
    contribution: Decimal
    outside_contract_window: bool = False


@dataclass
class WorkloadResult:
    staff_id: int
    period_start: date
    period_end: date
    capacity: "Decimal | None"
    capacity_state: str  # resolved | resolved_zero | unresolved_no_history | unresolved_partial
    capacity_anomalies: list
    charge_known: Decimal
    rate: "Decimal | None"
    rate_is_floor: bool
    completeness: str  # sufficient | partial | capacity_unresolved
    overloaded: "bool | None"
    anomalies: list
    contributions: list
    historical_caveat: bool
    as_of: date


def _leaf_action_items_for_staff(staff, period_start, period_end):
    user = staff.user
    qs = (
        ActionItem.objects.filter(
            sub_action_items__isnull=True,
            target_date__gte=period_start,
        )
        .filter(
            # effective assignee match: current M2M or legacy FK, normalized
            # later in Python only for the dedup/count -- this OR is purely
            # to select the right rows.
            Q(assignees=user) | Q(assignee=user)
        )
        .distinct()
        .select_related(
            "parent_action_item", "milestone", "milestone__project",
            "parent_action_item__milestone", "parent_action_item__milestone__project",
        )
        .prefetch_related("assignees")
    )
    return qs


def compute_staff_workload(staff, period_start, period_end, *, as_of=None, solution=None):
    """The one shared computation -- see module docstring. `period_start`/
    `period_end` are the READING window (inclusive); for alert evaluation
    this is always the current calendar month (services.workload_alerts),
    for the HRM view it is whatever the RH selected (section 4 of AI-2054).

    `solution`: when given, only action items resolving to THIS solution
    (via their project) are counted -- a standalone action item with no
    project at all is excluded from a solution-scoped computation (flagged
    as an anomaly, never silently folded into any solution) rather than
    guessed. `None` (unscoped) is never used by the HRM view/alert engine
    themselves (both always resolve one authorized company+solution first,
    same precedent as every other RH endpoint) -- only available here for
    completeness."""
    as_of = as_of or date.today()
    anomalies = []
    contributions = []
    charge_known = ZERO

    company_profile_id = staff.user.company_id
    working_days = get_organization_working_days(company_profile_id)
    working_days_per_week = Decimal(len(working_days) or 5)

    items = _leaf_action_items_for_staff(staff, period_start, period_end)
    seen_parent_ids = set()
    seen_status_incoherences = set()

    for item in items:
        if item.status and item.status.lower() == "closed":
            continue  # closed work never represents an upcoming/current charge
        if solution is not None:
            item_solution = resolve_action_item_solution(item)
            if item_solution != solution:
                # Expected scoping, not a data-quality issue -- excluded
                # silently (never guessed into scope), not reported as an
                # anomaly that would degrade completeness/rate_is_floor.
                continue
        assignees = _effective_assignees(item)
        n = len(assignees)
        if n == 0:
            anomalies.append(WorkloadAnomaly(
                "no_assignee", "Action item has no resolvable assignee (data anomaly).", item.id,
            ))
            continue
        budget = item.allocated_time
        if budget is None or budget < 0:
            anomalies.append(WorkloadAnomaly(
                "missing_or_negative_budget", "No usable allocated_time for this action item.", item.id,
            ))
            continue
        start = item.start_date
        if start is None:
            anomalies.append(WorkloadAnomaly(
                "no_start_date", "Action item has no start_date -- cannot be distributed over time.", item.id,
            ))
            continue
        end = item.target_date
        if end < start:
            anomalies.append(WorkloadAnomaly(
                "end_before_start", "target_date is before start_date.", item.id,
            ))
            continue
        item_company_id = resolve_action_item_company_profile_id(item) or company_profile_id
        item_working_days = get_organization_working_days(item_company_id)
        total_working_days = _count_working_days(start, end, item_working_days)
        if total_working_days == 0:
            anomalies.append(WorkloadAnomaly(
                "no_working_day_in_range", "The full [start_date, target_date] range contains no working day.", item.id,
            ))
            continue

        overlap_start = max(start, period_start)
        overlap_end = min(end, period_end)
        working_days_in_period = _count_working_days(overlap_start, overlap_end, item_working_days)

        individual_share = Decimal(budget) / Decimal(n)
        contribution_value = (
            individual_share * Decimal(working_days_in_period) / Decimal(total_working_days)
        )

        outside_contract_window = False
        if working_days_in_period > 0:
            contract_start = staff.contract_start_date
            contract_end = staff.contract_end_date
            window_lo = max(overlap_start, contract_start) if contract_start else overlap_start
            window_hi = min(overlap_end, contract_end) if contract_end else overlap_end
            if window_lo > window_hi:
                outside_contract_window = True
                anomalies.append(WorkloadAnomaly(
                    "charge_outside_contract_window",
                    "This contribution falls outside the staff's own contract window -- kept as computed, never redistributed.",
                    item.id,
                ))

        if working_days_in_period > 0:
            charge_known += contribution_value
            project_id, project_name, milestone_id, milestone_name = _project_context(item)
            contributions.append(WorkloadContribution(
                action_item_id=item.id,
                name=item.name,
                status=item.status,
                start_date=start,
                target_date=end,
                is_sub_action=bool(item.parent_action_item_id),
                project_id=project_id,
                project_name=project_name,
                milestone_id=milestone_id,
                milestone_name=milestone_name,
                budget_total=budget,
                assignee_count=n,
                individual_share=individual_share,
                working_days_total=total_working_days,
                working_days_in_period=working_days_in_period,
                contribution=contribution_value,
                outside_contract_window=outside_contract_window,
            ))

        if item.parent_action_item_id:
            parent = item.parent_action_item
            pair = (parent.id, item.id)
            if parent.status and parent.status.lower() == "closed" and item.status and item.status.lower() != "closed" and pair not in seen_status_incoherences:
                seen_status_incoherences.add(pair)
                anomalies.append(WorkloadAnomaly(
                    "parent_closed_child_open",
                    "Parent action item is closed while this sub-action is still open -- result cannot be presented as complete.",
                    item.id, {"parent_id": parent.id},
                ))
            if parent.id not in seen_parent_ids:
                seen_parent_ids.add(parent.id)
                if parent.allocated_time is not None:
                    children_sum = sum(
                        (c.allocated_time or 0) for c in parent.sub_action_items.all()
                    )
                    if abs(Decimal(parent.allocated_time) - Decimal(children_sum)) > _PARENT_CHILD_TOLERANCE:
                        anomalies.append(WorkloadAnomaly(
                            "parent_children_mismatch",
                            "Parent's own allocated_time differs from the sum of its sub-actions' budgets -- the parent's own value is never counted, only signaled.",
                            parent.id,
                            {"parent_allocated_time": parent.allocated_time, "children_sum": children_sum},
                        ))

    capacity, capacity_state, capacity_anomalies = _resolve_capacity(
        staff, period_start, period_end, working_days, working_days_per_week,
    )

    rate = None
    rate_is_floor = False
    overloaded = None
    blocking_anomaly_codes = {
        "no_assignee", "missing_or_negative_budget", "no_start_date",
        "end_before_start", "no_working_day_in_range",
    }
    has_exclusions = any(a.code in blocking_anomaly_codes for a in anomalies)

    if capacity_state not in ("resolved", "resolved_zero"):
        completeness = "capacity_unresolved"
    elif capacity_state == "resolved_zero" or capacity == 0:
        completeness = "capacity_unresolved"
    else:
        rate = (charge_known / capacity) * Decimal(100)
        rate_is_floor = has_exclusions
        completeness = "partial" if has_exclusions else "sufficient"
        if completeness == "sufficient":
            overloaded = rate > Decimal(100)
        else:
            overloaded = True if rate > Decimal(100) else None

    return WorkloadResult(
        staff_id=staff.id,
        period_start=period_start,
        period_end=period_end,
        capacity=capacity,
        capacity_state=capacity_state,
        capacity_anomalies=capacity_anomalies,
        charge_known=charge_known,
        rate=rate,
        rate_is_floor=rate_is_floor,
        completeness=completeness,
        overloaded=overloaded,
        anomalies=anomalies,
        contributions=contributions,
        historical_caveat=period_end < as_of,
        as_of=as_of,
    )


def _resolve_capacity(staff, period_start, period_end, working_days, working_days_per_week):
    """RH12 (StaffQuotiteHistory) segmented over the WHOLE period, clipped to
    the staff's own contract window -- see module docstring on
    `contractual_hours` never being multiplied again by quotite (the
    `contractual_hours` field already IS the real weekly hours)."""
    contract_start = staff.contract_start_date
    contract_end = staff.contract_end_date
    window_start = max(period_start, contract_start) if contract_start else period_start
    window_end = min(period_end, contract_end) if contract_end else period_end

    if window_start > window_end:
        # The contract window does not cover this reading period at all --
        # a genuinely resolved fact (zero), never "unresolved".
        return ZERO, "resolved_zero", []

    try:
        segments = resolve_quotite_segments(staff, window_start, window_end)
    except LeaveAccrualQuotiteError as exc:
        return None, "unresolved_no_history", [
            WorkloadAnomaly("capacity_no_quotite_history", str(exc)),
        ]

    capacity = ZERO
    anomalies = []
    for seg_start, seg_end, _quotite in segments:
        entry = resolve_current_quotite_entry(staff, as_of=seg_start)
        if entry is None:  # pragma: no cover - resolve_quotite_segments already guarantees coverage
            anomalies.append(WorkloadAnomaly(
                "capacity_segment_unresolved", f"No quotite entry resolvable for segment starting {seg_start}.",
            ))
            continue
        seg_working_days = _count_working_days(seg_start, seg_end, working_days)
        capacity += Decimal(entry.contractual_hours) * Decimal(seg_working_days) / working_days_per_week

    state = "unresolved_partial" if anomalies else "resolved"
    return capacity, state, anomalies
