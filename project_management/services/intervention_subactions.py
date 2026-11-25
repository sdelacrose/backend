"""Adapting the sub-actions of an intervention AFTER its import (add, rename, re-weight, remove).

One atomic operation replaces the whole list of sub-actions of a mother with the wanted one:

    PUT intervention-actions/<mother id>/sub-actions
    {"sub_actions": [{"id": 12|null, "title", "in_progress_points", "closed_points", "assignee_id"?}]}

Rules (checked here, on the server, whatever the front says):
  * only the MOTHER of an intervention (`intervention_action_key == "mother"`) is concerned; an
    intervention already closed is refused, like the existing rule "no sub-action under a closed action";
  * a line with an `id` keeps its row: identifier, status, owners, dates, links, comments are never
    recreated; only the title and the points change. A line without `id` is a NEW open sub-action.
    A sub-action missing from the list is removed;
  * a CLOSED sub-action is frozen (title, points, removal refused): its closure already counted in the
    progress and in the history;
  * the closed contributions of ALL the sub-actions total exactly 100, each in-progress contribution is
    <= its closed one (same `validate_points` as the import and the manual addition);
  * the company template is never read back nor written: the points live on each sub-action
    (`ActionItemProgressSetting`, copied at import) and `source_template` is kept untouched;
  * everything is applied in ONE transaction: any refusal leaves the intervention exactly as it was;
  * the progress of the mother is not stored, it is recomputed by `compute_progress` from the statuses
    (kept) and the new weights; when the removal leaves only closed sub-actions the existing rule closes
    the mother (`close_parent_if_complete`).
"""
from django.db import transaction

from project_management.models import ActionItem, ActionItemProgressSetting
from project_management.services.action_progress import (
    MOTHER_KEY,
    close_parent_if_complete,
    compute_progress,
    status_state,
    CLOSED,
)
from project_management.services.intervention_plan import MAX_SUB_ACTIONS, MAX_SUB_TITLE


class SubActionError(ValueError):
    """The requested list is refused (`code` is machine readable, `detail` safe to show)."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SubActionError("points_invalid", f"{label}: points must be whole numbers.")
    return value


def clean_lines(raw):
    if not isinstance(raw, list) or not raw:
        raise SubActionError("no_sub_action", "A list of at least one sub-action is required.")
    if len(raw) > MAX_SUB_ACTIONS:
        raise SubActionError("too_many_sub_actions", f"At most {MAX_SUB_ACTIONS} sub-actions are allowed.")
    cleaned, seen = [], set()
    for index, line in enumerate(raw, start=1):
        if not isinstance(line, dict):
            raise SubActionError("plan_invalid", f"Sub-action {index} is not valid.")
        title = " ".join(str(line.get("title") or "").split())
        if not title:
            raise SubActionError("title_required", f"Sub-action {index}: the title is required.")
        if len(title) > MAX_SUB_TITLE:
            raise SubActionError("title_too_long", f"Sub-action {index}: the title is too long.")
        line_id = line.get("id")
        if line_id in ("", None):
            line_id = None
        elif isinstance(line_id, bool) or not isinstance(line_id, int):
            raise SubActionError("plan_invalid", f"Sub-action {index}: invalid identifier.")
        elif line_id in seen:
            raise SubActionError("plan_invalid", f"Sub-action {index}: listed twice.")
        else:
            seen.add(line_id)
        assignee_id = line.get("assignee_id")
        if assignee_id in ("", None):
            assignee_id = None
        elif isinstance(assignee_id, bool) or not isinstance(assignee_id, int):
            raise SubActionError("invalid_assignee", f"Sub-action {index}: invalid owner.")
        cleaned.append(
            {
                "id": line_id,
                "title": title,
                "in_progress_points": _int(line.get("in_progress_points"), f"Sub-action {index}"),
                "closed_points": _int(line.get("closed_points"), f"Sub-action {index}"),
                "assignee_id": assignee_id,
            }
        )
    for index, line in enumerate(cleaned, start=1):  # per line; the total is judged once the frozen lines are known
        if not (0 <= line["in_progress_points"] <= line["closed_points"] <= 100):
            raise SubActionError(
                "points_invalid", f"Sub-action {index}: points must satisfy 0 <= in progress <= closed <= 100."
            )
    return cleaned


def is_request_mother(item):
    return bool(item.intervention_request_id and not item.parent_action_item_id and item.intervention_action_key == MOTHER_KEY)


@transaction.atomic
def replace_sub_actions(mother, raw_lines, *, user, can_delete=True):
    """Apply the wanted list atomically. Returns {"created": [...], "updated": [...], "removed": [...],
    "progress": compute_progress(...)}."""
    lines = clean_lines(raw_lines)
    mother = (
        ActionItem.objects.select_for_update(of=("self",)).select_related("milestone__project").get(pk=mother.pk)
    )
    if not is_request_mother(mother):
        raise SubActionError("not_a_mother", "Sub-actions are adapted on the mother action of an intervention.")
    if status_state(mother.status) == CLOSED:
        raise SubActionError("mother_closed", "Sub-actions cannot be changed under a closed intervention.")

    current = {
        s.pk: s
        for s in mother.sub_action_items.select_related("progress_setting").select_for_update(of=("self",))
    }
    project = mother.milestone.project if mother.milestone_id else None
    members = set()
    if project is not None:
        members = set(project.team_members.values_list("team_member_id", flat=True))

    wanted_ids = {l["id"] for l in lines if l["id"] is not None}
    unknown = wanted_ids - set(current)
    if unknown:
        raise SubActionError("unknown_sub_action", "A listed sub-action does not belong to this intervention.")
    removed = [s for pk, s in current.items() if pk not in wanted_ids]
    if removed and not can_delete:
        raise SubActionError("delete_forbidden", "You are not allowed to remove sub-actions.")
    for sub in removed:
        if status_state(sub.status) == CLOSED:
            raise SubActionError("closed_locked", "A closed sub-action cannot be removed.")

    for line in lines:
        if line["assignee_id"] is not None and line["assignee_id"] not in members:
            raise SubActionError("invalid_assignee", "The owner is not a member of this project.")
        if line["id"] is None:
            continue
        sub = current[line["id"]]
        if status_state(sub.status) == CLOSED:
            setting = getattr(sub, "progress_setting", None)
            same_points = (
                setting is not None
                and setting.in_progress_points == line["in_progress_points"]
                and setting.closed_points == line["closed_points"]
            )
            if sub.name != line["title"] or not same_points:
                raise SubActionError("closed_locked", "A closed sub-action cannot be changed.")

    # Weights: the closed sub-actions keep theirs, the open and new ones share what is left. A total above 100 is an
    # explicit "not enough weight available" refusal (with the available share), a lower one a plain total error.
    total = sum(l["closed_points"] for l in lines)
    locked_total = sum(
        current[l["id"]].progress_setting.closed_points
        for l in lines
        if l["id"] is not None and status_state(current[l["id"]].status) == CLOSED
        and hasattr(current[l["id"]], "progress_setting")
    )
    if total > 100:
        raise SubActionError(
            "weight_insufficient",
            f"Not enough weight available: closed sub-actions keep {locked_total}, so the open and new sub-actions "
            f"can share at most {100 - locked_total}, but {total - locked_total} were requested.",
        )
    if total < 100:
        raise SubActionError("points_invalid", f"The closed contributions must total 100 (currently {total}).")

    default_owners = list(mother.assignees.all())
    owners = {u.pk: u for u in _users(project, {l["assignee_id"] for l in lines if l["assignee_id"]})}
    created, updated = [], []
    removed_ids = [s.pk for s in removed]
    for sub in removed:
        sub._suppress_action_item_notifications = True
        sub.delete()
    for line in lines:
        if line["id"] is None:
            item = ActionItem(
                parent_action_item=mother,
                name=line["title"],
                status="open",
                target_date=mother.target_date,
                created_by=user,
            )
            item._suppress_action_item_notifications = True
            item.save()
            item.assignees.set([owners[line["assignee_id"]]] if line["assignee_id"] else default_owners)
            ActionItemProgressSetting.objects.create(
                action_item=item,
                in_progress_points=line["in_progress_points"],
                closed_points=line["closed_points"],
                is_bpu=False,
            )
            created.append(item.pk)
            continue
        sub = current[line["id"]]
        changed = False
        if sub.name != line["title"]:
            sub.name = line["title"]
            sub._suppress_action_item_notifications = True
            sub.save(update_fields=["name", "updated_at"])
            changed = True
        setting = getattr(sub, "progress_setting", None)
        if setting is None:
            ActionItemProgressSetting.objects.create(
                action_item=sub,
                in_progress_points=line["in_progress_points"],
                closed_points=line["closed_points"],
                is_bpu=False,
            )
            changed = True
        elif (setting.in_progress_points, setting.closed_points) != (
            line["in_progress_points"], line["closed_points"]
        ):
            ActionItemProgressSetting.objects.filter(pk=setting.pk).update(
                in_progress_points=line["in_progress_points"], closed_points=line["closed_points"]
            )
            changed = True
        if line["assignee_id"] is not None and not sub.assignees.filter(pk=line["assignee_id"]).exists():
            sub.assignees.set([owners[line["assignee_id"]]])
            changed = True
        if changed:
            updated.append(sub.pk)

    # the removal may leave only closed sub-actions: the existing rule closes the mother
    closed_left = mother.sub_action_items.filter(status__iexact="closed").first()
    if removed and closed_left is not None:
        close_parent_if_complete(closed_left)
    mother.refresh_from_db()
    return {
        "created": created,
        "updated": updated,
        "removed": removed_ids,
        "mother_status": mother.status,
        "progress": compute_progress(mother),
    }


def _users(project, ids):
    if not ids or project is None:
        return []
    return [m.team_member for m in project.team_members.select_related("team_member").filter(team_member_id__in=ids)]
