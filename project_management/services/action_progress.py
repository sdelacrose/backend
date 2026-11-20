"""PROSEVIEW MOD-01: template application, progress by status, automatic closing of the parent.

Status mapping (the statuses really used by LINKS Action Items, see ECPMP_STATUS_OPTIONS):
    "open"         -> not started   (0 point)
    "in progress"  -> in progress   (in_progress_points)
    "closed"       -> closed        (closed_points)
MOD-02b: a request mother WITHOUT sub-action follows its own status with the same mapping:
open 0 %, in progress 10 %, closed 100 %. As soon as it has a sub-action the calculation is ONLY the
contributions of its sub-actions (no second calculation is kept).
Any other status is UNKNOWN: it is never assimilated to "in progress"; it contributes 0 point and
is reported in `unmapped_statuses` so that the project owner can arbitrate.

Parent progress = sum of the contributions of its sub-actions that carry copied points. When every
sub-action is closed the progress is 100 whatever the points (a 0-point or manually added
sub-action must be closed too) and the parent is closed automatically.
"""
from datetime import date

from django.db import transaction

from project_management.models import (
    ActionItem,
    ActionItemProgressSetting,
    ActionTemplate,
)
from project_management.models.action_template import normalize_title
from project_management.project_access import accessible_projects

NOT_STARTED = "not_started"
IN_PROGRESS = "in_progress"
CLOSED = "closed"
UNKNOWN = "unknown"

_STATUS_MAP = {
    "open": NOT_STARTED,
    "in progress": IN_PROGRESS,
    "in_progress": IN_PROGRESS,
    "closed": CLOSED,
}


class TemplateApplicationError(Exception):
    """The template cannot be applied to this action (message is safe to show)."""


MOTHER_ONLY_POINTS = {NOT_STARTED: 0, IN_PROGRESS: 10, CLOSED: 100}
MOTHER_KEY = "mother"  # ActionItem.intervention_action_key of a mother created by the import


def effective_bpu(item):
    """BPU of an action. Priority rule (MOD-02b): a mother carries BPU itself; its sub-actions
    INHERIT it (mother true => every sub-action is BPU). Sub-action flags stored by MOD-02 before the
    inheritance (ActionItemProgressSetting.is_bpu) are NOT rewritten: while the mother is not BPU they
    keep applying, so previously recorded data stay visible. Returns (is_bpu, source)."""
    parent = item.parent_action_item if item.parent_action_item_id else None
    if parent is None:
        return bool(item.is_bpu), ("mother" if item.is_bpu else None)
    if parent.is_bpu:
        return True, "mother"
    setting = getattr(item, "progress_setting", None)
    if setting is not None and setting.is_bpu:
        return True, "legacy_line"
    return False, None


def is_intervention_action(item):
    """True for an action born from an intervention request (the mother, or a sub-action of such a mother).
    BPU and intervention templates exist only in that perimeter: classic actions never show them."""
    if item.intervention_request_id:
        return True
    parent = item.parent_action_item if item.parent_action_item_id else None
    return bool(parent is not None and parent.intervention_request_id)


def displayed_bpu(item):
    """What the classic action screens may show: the BPU of an intervention action, nothing otherwise.
    Stored BPU data is never deleted or rewritten; it is only not displayed outside interventions."""
    return effective_bpu(item) if is_intervention_action(item) else (False, None)


def status_state(status):
    return _STATUS_MAP.get(normalize_title(status), UNKNOWN)


def company_id_of_project(project):
    return getattr(project, "company_profile_id", None)


def find_template(project, title):
    """Exact match on the normalized title inside the COMPANY that owns `project` (company read
    from the project server-side); None otherwise. No fuzzy match; a template flagged
    `needs_review` (ambiguous title) is never returned."""
    key = normalize_title(title)
    company_id = company_id_of_project(project)
    if not key or company_id is None:
        return None
    return ActionTemplate.objects.filter(
        company_id=company_id, normalized_title=key, needs_review=False
    ).first()


def refresh_review_flags(company_id, normalized_titles):
    """Clear `needs_review` on templates whose title no longer collides inside the company.
    Never merges or deletes anything; only titles in `normalized_titles` are re-evaluated."""
    for key in {k for k in normalized_titles if k}:
        group = list(ActionTemplate.objects.filter(company_id=company_id, normalized_title=key))
        if len(group) == 1 and group[0].needs_review:
            ActionTemplate.objects.filter(pk=group[0].pk).update(needs_review=False)


def validate_points(sub_actions):
    """Raise ValueError with a readable message when the contributions are inconsistent."""
    if not sub_actions:
        raise ValueError("A template needs at least one sub-action.")
    total = 0
    for index, sub in enumerate(sub_actions, start=1):
        in_progress, closed = sub["in_progress_points"], sub["closed_points"]
        if not (0 <= in_progress <= closed <= 100):
            raise ValueError(
                f"Sub-action {index}: points must satisfy 0 <= in progress <= closed <= 100."
            )
        total += closed
    if total != 100:
        raise ValueError(f"The closed contributions must total 100 (currently {total}).")


def uses_points_mechanism(parent):
    """True for a mother whose progress is points based: it has sub-actions with points, or it is a
    request mother (created by the import) that has no sub-action yet (its first addition must then
    carry 100 points)."""
    if is_weighted(parent):
        return True
    return parent.intervention_action_key == MOTHER_KEY and not parent.sub_action_items.exists()


def compute_progress(parent, subs=None):
    """Weighted progress of a parent action. `mode` is "unweighted" when no sub-action carries points.

    `subs` (optional): the already loaded sub-actions (prefetched by the request list, with their
    progress_setting) so that a page of requests costs no query per row."""
    if subs is None:
        subs = list(parent.sub_action_items.select_related("progress_setting").order_by("id"))
    else:
        subs = sorted(subs, key=lambda sub: sub.id)
    weighted = any(hasattr(sub, "progress_setting") for sub in subs)
    unmapped = sorted({sub.status for sub in subs if status_state(sub.status) == UNKNOWN})
    details, total = [], 0
    for sub in subs:
        state = status_state(sub.status)
        setting = getattr(sub, "progress_setting", None)
        points = 0
        if setting is not None:
            if state == IN_PROGRESS:
                points = setting.in_progress_points
            elif state == CLOSED:
                points = setting.closed_points
        total += points
        details.append({
            "id": sub.id, "name": sub.name, "status": sub.status, "state": state, "points": points,
            # MOD-02: the copied settings, so a manual addition can adjust open siblings explicitly
            "in_progress_points": setting.in_progress_points if setting else None,
            "closed_points": setting.closed_points if setting else None,
            "is_bpu": effective_bpu(sub)[0],
            "sub_reference": sub.sub_reference,
        })
    all_closed = bool(subs) and all(d["state"] == CLOSED for d in details)
    if not subs and parent.intervention_action_key == MOTHER_KEY:
        state = status_state(parent.status)
        return {
            "mode": "mother_only",
            "progress": MOTHER_ONLY_POINTS.get(state, 0),
            "all_closed": state == CLOSED,
            "unmapped_statuses": [parent.status] if state == UNKNOWN else [],
            "sub_actions": [],
            "mother_bpu": bool(parent.is_bpu),
        }
    if not weighted:
        return {"mode": "unweighted", "progress": None, "all_closed": all_closed,
                "unmapped_statuses": unmapped, "sub_actions": details, "mother_bpu": bool(parent.is_bpu)}
    progress = 100 if all_closed else min(total, 100)
    return {"mode": "weighted", "progress": progress, "all_closed": all_closed,
            "unmapped_statuses": unmapped, "sub_actions": details, "mother_bpu": bool(parent.is_bpu)}


def build_sub_actions(
    parent, lines, *, user, default_assignees, target_date=None, template=None, intervention_request=None
):
    """Create ordinary sub-action items under `parent` (parent_action_item) and COPY their points.

    Shared by apply_template and by the import (MOD-02). Each line:
    {title, in_progress_points, closed_points, is_bpu?, assignee?}. A line owner overrides
    `default_assignees`. With `intervention_request` the sub-actions are linked to the request
    (keys s1, s2, ... unique per request). Notifications are suppressed (bulk generation)."""
    created = []
    for index, line in enumerate(lines, start=1):
        item = ActionItem(
            parent_action_item=parent,
            name=line["title"],
            status="open",
            target_date=target_date or parent.target_date,
            created_by=user,
            intervention_request=intervention_request,
            intervention_action_key=f"s{index}" if intervention_request is not None else "",
        )
        item._suppress_action_item_notifications = True
        item.save()
        owners = [line["assignee"]] if line.get("assignee") is not None else default_assignees
        if owners:
            item.assignees.set(owners)
        ActionItemProgressSetting.objects.create(
            action_item=item,
            in_progress_points=line["in_progress_points"],
            closed_points=line["closed_points"],
            is_bpu=False,  # MOD-02b: BPU is inherited from the mother, never chosen per line
            source_template=template,
            source_template_title=template.title if template is not None else "",
        )
        created.append(item)
    return created


@transaction.atomic
def apply_template(parent, template, *, user=None, target_date=None, assignees=None):
    """Create the template's sub-actions under `parent` and COPY their points.

    Not wired to the import or any button in MOD-01: it is the single entry point the next lots
    will call. Existing actions are never touched.

    The company is derived from the parent action's PROJECT on the server: a template of another
    company, an ambiguous template (`needs_review`) and (when `user` is given) a project the user
    cannot reach are refused."""
    parent = ActionItem.objects.select_for_update().get(pk=parent.pk)
    project = parent.milestone.project if parent.milestone_id else None
    if project is None:
        raise TemplateApplicationError("The action does not belong to a project.")
    if user is not None and not accessible_projects(user).filter(pk=project.pk).exists():
        raise TemplateApplicationError("The project is not accessible.")
    if template.company_id is None or template.company_id != company_id_of_project(project):
        raise TemplateApplicationError("This template belongs to another company.")
    if template.needs_review:
        raise TemplateApplicationError("This template has an ambiguous title and must be reviewed first.")
    if parent.parent_action_item_id:
        raise TemplateApplicationError("A template can only be applied to a top-level action.")
    if normalize_title(parent.status) == CLOSED:
        raise TemplateApplicationError("A template cannot be applied to a closed action.")
    if ActionItemProgressSetting.objects.filter(action_item__parent_action_item=parent).exists():
        raise TemplateApplicationError("A template was already applied to this action.")
    lines = [
        {"title": sub.title, "in_progress_points": sub.in_progress_points, "closed_points": sub.closed_points}
        for sub in template.sub_actions.all()
    ]
    return build_sub_actions(
        parent, lines, user=user, target_date=target_date,
        default_assignees=assignees if assignees is not None else list(parent.assignees.all()),
        template=template,
    )


def close_parent_if_complete(sub):
    """Close the parent once every sub-action is closed. Only parents that carry template points
    are concerned: parents without them keep their existing manual closing rule."""
    if not sub.parent_action_item_id or status_state(sub.status) != CLOSED:
        return False
    with transaction.atomic():
        parent = ActionItem.objects.select_for_update().get(pk=sub.parent_action_item_id)
        if status_state(parent.status) == CLOSED:
            return False
        siblings = list(parent.sub_action_items.all())
        if not any(
            ActionItemProgressSetting.objects.filter(action_item=s).exists() for s in siblings
        ):
            return False
        if any(status_state(s.status) != CLOSED for s in siblings):
            return False
        parent.status = "closed"
        parent.closed_at = sub.closed_at or date.today()
        parent.closed_by = sub.closed_by
        parent._suppress_action_item_notifications = True
        parent.save(update_fields=["status", "closed_at", "closed_by", "updated_at"])
    return True


def start_parent_if_child_started(sub):
    """A mother still "open" follows its sub-actions: as soon as one of them is started ("in progress")
    or closed while not all are closed, the mother becomes "in progress". Only the open -> in progress
    step is automatic (never a downgrade, never a closed or unknown-status mother: those keep their
    existing rules); an unknown sub-action status is never read as "started". Mothers without
    sub-action are not concerned. Existing data is not rewritten: a mother is evaluated when one of
    its sub-actions is saved."""
    if not sub.parent_action_item_id or status_state(sub.status) not in (IN_PROGRESS, CLOSED):
        return False
    with transaction.atomic():
        parent = ActionItem.objects.select_for_update().get(pk=sub.parent_action_item_id)
        if status_state(parent.status) != NOT_STARTED:
            return False
        states = [status_state(s.status) for s in parent.sub_action_items.all()]
        if not any(state in (IN_PROGRESS, CLOSED) for state in states) or all(
            state == CLOSED for state in states
        ):
            return False  # nothing started, or all closed (closing keeps its own rule)
        parent.status = "in progress"
        parent._suppress_action_item_notifications = True
        parent.save(update_fields=["status", "updated_at"])
    return True


# --- MOD-02: sub-actions added by hand under a weighted mother ---------------------------------

class WeightedAdditionError(ValueError):
    """The points of a manual sub-action addition are inconsistent (message is safe to show)."""


def is_weighted(parent):
    """A mother is concerned by the points mechanism when one of its sub-actions carries points."""
    return ActionItemProgressSetting.objects.filter(action_item__parent_action_item=parent).exists()


def _points(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise WeightedAdditionError(f"{label}: points must be whole numbers.")
    return value


def validate_weighted_addition(parent, setting, siblings):
    """Validate a manual sub-action added under a weighted mother.

    `setting`: {in_progress_points, closed_points, is_bpu?} of the NEW sub-action (explicit: the
    points are never guessed). `siblings`: explicit adjustments [{id, in_progress_points,
    closed_points}] of OPEN sibling sub-actions. Nothing is recalculated or redistributed: the
    closed contributions of all sub-actions, after the adjustments, must total exactly 100.
    Returns the cleaned (setting, siblings)."""
    if not isinstance(setting, dict):
        raise WeightedAdditionError("The points of the new sub-action are required (progress_setting).")
    is_bpu = setting.get("is_bpu", False)
    if not isinstance(is_bpu, bool):
        raise WeightedAdditionError("BPU must be true or false.")
    if is_bpu:
        raise WeightedAdditionError("BPU is set on the mother action and inherited by its sub-actions.")
    clean = {
        "in_progress_points": _points(setting.get("in_progress_points"), "New sub-action"),
        "closed_points": _points(setting.get("closed_points"), "New sub-action"),
        "is_bpu": False,
    }
    existing = {
        sub.pk: sub
        for sub in parent.sub_action_items.select_related("progress_setting")
        if hasattr(sub, "progress_setting")
    }
    adjustments = {}
    for entry in siblings or []:
        if not isinstance(entry, dict) or entry.get("id") not in existing:
            raise WeightedAdditionError("An adjusted sibling is not a sub-action of this action with points.")
        if entry["id"] in adjustments:
            raise WeightedAdditionError("A sibling is adjusted twice.")
        if status_state(existing[entry["id"]].status) == CLOSED:
            raise WeightedAdditionError("The points of a closed sub-action cannot be changed.")
        adjustments[entry["id"]] = {
            "in_progress_points": _points(entry.get("in_progress_points"), "Adjusted sub-action"),
            "closed_points": _points(entry.get("closed_points"), "Adjusted sub-action"),
        }
    lines = [clean] + [
        adjustments.get(pk, {
            "in_progress_points": sub.progress_setting.in_progress_points,
            "closed_points": sub.progress_setting.closed_points,
        })
        for pk, sub in existing.items()
    ]
    try:
        validate_points(lines)
    except ValueError as error:  # includes "must total 100": a 400 for the client, never a 500
        raise WeightedAdditionError(str(error))
    return clean, [{"id": pk, **values} for pk, values in adjustments.items()]


def apply_weighted_addition(new_item, setting, siblings):
    """Store the new sub-action's points/BPU and the explicit sibling adjustments (same transaction)."""
    ActionItemProgressSetting.objects.create(action_item=new_item, **setting)
    for entry in siblings:
        ActionItemProgressSetting.objects.filter(action_item_id=entry["id"]).update(
            in_progress_points=entry["in_progress_points"], closed_points=entry["closed_points"]
        )
