"""Intervention KPIs (dashboard widget) and operational shortcuts (Interventions page banner).

ONE unit of count = ONE intervention = ONE intervention request. Its sub-actions are never counted; the
flat actions of the older requests (no mother, no sub-action) are grouped by request. Everything is
computed from the actions the caller may see (the visibility service of the Action Items list): a unit
that is not authorized is never counted, so no aggregate leaks a hidden intervention.

The same `Unit` objects feed the counters AND the list filters (`kpi=<flag>`), so that a counter and the
interventions displayed after the click can never disagree.

Rules (see docs/KPI-interventions.md): status and progress reuse action_progress (statuses open / in
progress / closed, weighted progress, mother without sub-action 0 / 10 / 100); the customer deadline is
InterventionRequest.expected_end_date; planning dates are the action's start_date / forecast_date.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable

from django.db.models import Q
from django.utils import timezone

from project_management.models import ActionItem, InterventionRequest
from project_management.services.action_progress import (
    CLOSED,
    IN_PROGRESS,
    MOTHER_KEY,
    MOTHER_ONLY_POINTS,
    NOT_STARTED,
    compute_progress,
    status_state,
)
from project_management.models.action_template import normalize_title

# No Hold status exists in the default Action Item configuration (open / in progress / closed). A project
# that configures one of these labels gets the Hold counter; otherwise it is simply 0 (never guessed).
HOLD_STATUSES = frozenset({"hold", "on hold", "on_hold", "suspended"})

FLAGS = (
    "not_started", "in_progress", "done", "other_status", "bpu", "late", "hold", "on_time",
    "unassigned", "to_plan", "to_verify",
)
DEFAULT_WEEKS = 8
MAX_WEEKS = 26


def is_hold(status) -> bool:
    return normalize_title(status) in HOLD_STATUSES


@dataclass
class Unit:
    request_id: int
    top_ids: list[int]
    state: str                      # not_started | in_progress | closed | unknown
    progress: int | None
    bpu: bool
    hold: bool
    received_date: date | None
    expected_end_date: date | None
    closed_at: date | None
    unassigned: bool
    to_plan: bool
    to_verify: bool
    flags: set = field(default_factory=set)


def _top_level_scope(visible: "QuerySet", limited_ids) -> "QuerySet":
    tops = visible.filter(parent_action_item__isnull=True, intervention_request__isnull=False)
    if limited_ids is not None:
        # restricted user: only the interventions whose top action they are directly allowed to see
        # (a mother reachable only through one of their sub-actions is navigation, not an authorization)
        tops = tops.filter(id__in=set(limited_ids))
    return tops


def build_units(visible, *, limited_ids=None, today: date | None = None) -> list[Unit]:
    today = today or timezone.localdate()
    tops = list(_top_level_scope(visible, limited_ids).values("id", "intervention_request_id"))
    request_ids = {row["intervention_request_id"] for row in tops}
    if not request_ids:
        return []
    requests = {r.id: r for r in InterventionRequest.objects.filter(id__in=request_ids)}
    actions = (
        ActionItem.objects.filter(
            Q(intervention_request_id__in=request_ids, parent_action_item__isnull=True)
            | Q(parent_action_item__intervention_request_id__in=request_ids)
        )
        .select_related("progress_setting")
        .prefetch_related("assignees")
    )
    top_by_request: dict[int, list[ActionItem]] = {}
    subs_by_parent: dict[int, list[ActionItem]] = {}
    for action in actions:
        if action.parent_action_item_id:
            subs_by_parent.setdefault(action.parent_action_item_id, []).append(action)
        else:
            top_by_request.setdefault(action.intervention_request_id, []).append(action)
    allowed_top_ids = {row["id"] for row in tops}

    units: list[Unit] = []
    for request_id in sorted(request_ids):
        request = requests.get(request_id)
        group = [a for a in top_by_request.get(request_id, []) if a.id in allowed_top_ids]
        if request is None or not group:
            continue
        mother = next((a for a in group if a.intervention_action_key == MOTHER_KEY), None)
        units.append(_unit(request, group, mother, subs_by_parent, today))
    return units


def _active_subs(subs):
    return [s for s in subs if status_state(s.status) != CLOSED]


def _unit(request, group, mother, subs_by_parent, today) -> Unit:
    if mother is not None:
        subs = subs_by_parent.get(mother.id, [])
        status = mother.status
        state = status_state(status)
        progress = compute_progress(mother, subs=subs)["progress"]
        if progress is None and state == CLOSED:
            progress = 100
        bpu = bool(mother.is_bpu)
        closed_at = mother.closed_at if state == CLOSED else None
        owners_ok = bool(list(mother.assignees.all())) or any(list(s.assignees.all()) for s in _active_subs(subs))
        planned = (
            mother.start_date or mother.forecast_date
            or any(s.start_date or s.forecast_date for s in _active_subs(subs))
        )
        to_verify = bool(subs) and state != CLOSED and all(status_state(s.status) == CLOSED for s in subs)
        hold = is_hold(status)
    else:
        # older request: FLAT actions grouped by request (no mother, no sub-action)
        subs = []
        states = [status_state(a.status) for a in group]
        if all(s == CLOSED for s in states):
            state = CLOSED
        elif all(s == NOT_STARTED for s in states):
            state = NOT_STARTED
        elif any(s in (IN_PROGRESS, CLOSED) for s in states):
            state = IN_PROGRESS
        else:
            state = "unknown"
        points = [MOTHER_ONLY_POINTS.get(s, 0) for s in states]
        progress = round(sum(points) / len(points)) if points else None
        bpu = any(a.is_bpu for a in group)  # BPU counted once per intervention
        closed_dates = [a.closed_at for a in group if a.closed_at]
        closed_at = max(closed_dates) if state == CLOSED and len(closed_dates) == len(group) else None
        open_group = [a for a in group if status_state(a.status) != CLOSED]
        owners_ok = any(list(a.assignees.all()) for a in open_group)
        planned = any(a.start_date or a.forecast_date for a in open_group)
        to_verify = False
        hold = any(is_hold(a.status) for a in group)
    unit = Unit(
        request_id=request.id, top_ids=[a.id for a in group], state=state, progress=progress, bpu=bpu,
        hold=hold, received_date=request.received_date, expected_end_date=request.expected_end_date,
        closed_at=closed_at, unassigned=(state != CLOSED and not owners_ok),
        to_plan=(state != CLOSED and not planned), to_verify=to_verify,
    )
    unit.flags = _flags(unit, today)
    return unit


def _flags(unit: Unit, today: date) -> set:
    flags = set()
    flags.add({NOT_STARTED: "not_started", IN_PROGRESS: "in_progress", CLOSED: "done"}.get(unit.state, "other_status"))
    if unit.bpu:
        flags.add("bpu")
    if unit.hold:
        flags.add("hold")
    # late = NOT finished and past the CUSTOMER deadline; a Hold intervention may also be late (overlap)
    if unit.state != CLOSED and unit.expected_end_date and unit.expected_end_date < today:
        flags.add("late")
    if unit.state == CLOSED and unit.closed_at and unit.expected_end_date and unit.closed_at <= unit.expected_end_date:
        flags.add("on_time")
    if unit.unassigned:
        flags.add("unassigned")
    if unit.to_plan:
        flags.add("to_plan")
    if unit.to_verify:
        flags.add("to_verify")
    return flags


def navigation_only_count(visible, units, *, limited_ids=None) -> int:
    """Restricted user: interventions whose mother is VISIBLE (navigation from one of the user's own
    sub-actions) but not authorized, hence not counted. Lets the UI explain why a visible row is not in the
    counters. 0 for a user with the broad scope."""
    if limited_ids is None:
        return 0
    shown = set(
        visible.filter(parent_action_item__isnull=True, intervention_request__isnull=False)
        .values_list("intervention_request_id", flat=True)
    )
    return max(0, len(shown) - len(units))


def ids_for_flag(units: Iterable[Unit], flag: str) -> set[int]:
    """Top-level action ids (mothers / flat actions) of the interventions carrying `flag`."""
    return {top for unit in units if flag in unit.flags for top in unit.top_ids}


def apply_unit_filter(queryset, units: Iterable[Unit], flag: str):
    """Keep the interventions carrying `flag`, with their sub-actions."""
    ids = ids_for_flag(units, flag)
    return queryset.filter(Q(pk__in=ids) | Q(parent_action_item_id__in=ids))


def _pct(part: int, whole: int):
    return round(100.0 * part / whole, 1) if whole else None


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def build_kpis(
    units: list[Unit], *, today: date | None = None, weeks: int = DEFAULT_WEEKS, navigation_only: int = 0
) -> dict:
    today = today or timezone.localdate()
    weeks = max(1, min(int(weeks or DEFAULT_WEEKS), MAX_WEEKS))
    total = len(units)
    count = lambda flag: sum(1 for u in units if flag in u.flags)
    closed = [u for u in units if u.state == CLOSED]
    with_deadline = [u for u in closed if u.expected_end_date]
    measurable = [u for u in with_deadline if u.closed_at]
    on_time = [u for u in measurable if "on_time" in u.flags]
    progress_values = [u.progress for u in units if u.progress is not None]
    first_week = week_start(today) - timedelta(weeks=weeks - 1)
    received = {first_week + timedelta(weeks=i): 0 for i in range(weeks)}
    closed_per_week = dict.fromkeys(received, 0)
    for u in units:
        if u.received_date and week_start(u.received_date) in received:
            received[week_start(u.received_date)] += 1
        if u.closed_at and week_start(u.closed_at) in closed_per_week:
            closed_per_week[week_start(u.closed_at)] += 1
    return {
        "total": total,
        "portfolio": {"total": total, "bpu": count("bpu"), "bpu_percent": _pct(count("bpu"), total)},
        "progress": {
            "done": count("done"), "in_progress": count("in_progress"), "not_started": count("not_started"),
            # Hold or any other status that is not open / in progress / closed: never assimilated to "in progress"
            "other_status": count("other_status"),
        },
        "deadlines": {
            "late": count("late"),                      # open and past the customer deadline
            "hold": count("hold"),                      # may overlap "late": NOT exclusive parts of one set
            "late_and_hold": sum(1 for u in units if "late" in u.flags and "hold" in u.flags),
            "on_time": len(on_time),
            "on_time_denominator": len(measurable),
            "on_time_percent": _pct(len(on_time), len(measurable)),
            "closed_without_deadline": len(closed) - len(with_deadline),
            "closed_without_closure_date": len(with_deadline) - len(measurable),
        },
        "todo": {
            "unassigned": count("unassigned"), "to_plan": count("to_plan"), "to_verify": count("to_verify"),
        },
        "trends": {
            # snapshot: no progress history is stored, so no historical curve is invented
            "average_progress": round(sum(progress_values) / len(progress_values)) if progress_values else None,
            "average_progress_measured": len(progress_values),
            "average_progress_unmeasured": total - len(progress_values),
            "weeks": [
                {"week_start": start.isoformat(), "received": received[start], "closed": closed_per_week[start]}
                for start in sorted(received)
            ],
            "closed_without_date": sum(1 for u in closed if not u.closed_at),
        },
        # restricted user: visible rows reached only through the user's own sub-actions, not counted above
        "navigation_only": navigation_only,
        "as_of": today.isoformat(),
    }
