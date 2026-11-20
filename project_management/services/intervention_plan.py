"""PROSEVIEW MOD-02: editable plan of an imported request (mother action + sub-actions).

A plan is request-specific data stored on the preview row (`InterventionImportRow.plan`):

    {"template_id": int|None, "template_title": str,
     "sub_actions": [{"title", "in_progress_points", "closed_points", "assignee_id": None}],  # owners: after the import
     "is_bpu": bool,            # BPU of the MOTHER action, inherited by every sub-action (MOD-02b)
     "no_sub_actions": bool,    # explicit confirmation of a request without sub-action (MOD-02b)
     "save_as_template": bool}

* the template is found from the imported title inside the COMPANY of the authorized project
  (case/extra spaces ignored, templates flagged `needs_review` excluded, never fuzzy);
* editing a plan never touches the company template; BPU is never stored on a template and is chosen
  once per request (the mother); a per-line BPU is refused;
* an empty list of sub-actions is accepted ONLY with the explicit flag `no_sub_actions`, and then no
  100-point total applies (the mother follows its own status: 0 / 10 / 100 %);
* every rule is checked here, i.e. on the server, whatever the front says.
"""
from django.db import IntegrityError, transaction

from project_management.models import ActionTemplate, ActionTemplateSubAction
from project_management.models.action_template import normalize_title
from project_management.services.action_progress import find_template, validate_points

MAX_SUB_ACTIONS = 50
MAX_SUB_TITLE = 255


class PlanError(ValueError):
    """The plan is not acceptable (`code` is machine readable, `detail` safe to show)."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def can_manage_templates(user):
    """Company configuration right needed to create a reusable template (same as MOD-01)."""
    from companyprofileapis.permissions import CompanyConfigurationsPermission

    if getattr(user, "is_freelancer", False) or not getattr(user, "company_id", None):
        return False
    perm = CompanyConfigurationsPermission(action="create", scope="company")
    return bool(perm._check_create_permission(user) or perm._check_update_permission(user, None))


def plan_lines_from_template(template):
    return [
        {
            "title": sub.title,
            "in_progress_points": sub.in_progress_points,
            "closed_points": sub.closed_points,
            "assignee_id": None,
        }
        for sub in template.sub_actions.all()
    ]


def default_plan(project, title):
    """Plan proposed at preview: the matching company template's lines, or {} when none matches."""
    template = find_template(project, title)
    if template is None:
        return {}
    return {
        "template_id": template.pk,
        "template_title": template.title,
        "sub_actions": plan_lines_from_template(template),
        "is_bpu": False,  # per request, false by default
        "no_sub_actions": False,
        "save_as_template": False,
    }


def template_plan(project, template):
    """Plan prefilled from a template CHOSEN BY HAND (any title). The template must belong to the
    company of the authorized project and not be flagged for review; it is never modified."""
    from project_management.models import ActionTemplate

    found = ActionTemplate.objects.filter(
        pk=template, company_id=project.company_profile_id, needs_review=False
    ).first()
    if found is None:
        raise PlanError("template_invalid", "This template does not exist for the company of the project.")
    return {
        "template_id": found.pk,
        "template_title": found.title,
        "sub_actions": plan_lines_from_template(found),
        "is_bpu": False,
        "no_sub_actions": False,
        "save_as_template": False,
    }


def _int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanError("points_invalid", f"{label}: points must be whole numbers.")
    return value


def clean_plan(project, raw, *, user=None):
    """Validate a plan received from the client and return the cleaned version (or raise PlanError)."""
    if not isinstance(raw, dict):
        raise PlanError("plan_invalid", "The plan must be an object.")
    lines = raw.get("sub_actions")
    no_sub_actions = raw.get("no_sub_actions", False)
    if not isinstance(no_sub_actions, bool):
        raise PlanError("plan_invalid", "no_sub_actions must be true or false.")
    mother_bpu = raw.get("is_bpu", False)
    if not isinstance(mother_bpu, bool):
        raise PlanError("bpu_invalid", "BPU must be true or false.")
    if not isinstance(lines, list):
        raise PlanError("no_sub_action", "At least one sub-action is required.")
    if no_sub_actions:
        if lines:
            raise PlanError("plan_invalid", "A request without sub-action cannot list sub-actions.")
        if raw.get("save_as_template", False):
            raise PlanError("template_needs_sub_action", "A template needs at least one sub-action.")
        return {
            "template_id": None,
            "template_title": "",
            "sub_actions": [],
            "is_bpu": mother_bpu,
            "no_sub_actions": True,
            "save_as_template": False,
        }
    if not lines:
        raise PlanError("no_sub_action", "At least one sub-action is required, or confirm explicitly without any.")
    if len(lines) > MAX_SUB_ACTIONS:
        raise PlanError("too_many_sub_actions", f"At most {MAX_SUB_ACTIONS} sub-actions are allowed.")

    cleaned = []
    for index, line in enumerate(lines, start=1):
        if not isinstance(line, dict):
            raise PlanError("plan_invalid", f"Sub-action {index} is not valid.")
        title = " ".join(str(line.get("title") or "").split())
        if not title:
            raise PlanError("title_required", f"Sub-action {index}: the title is required.")
        if len(title) > MAX_SUB_TITLE:
            raise PlanError("title_too_long", f"Sub-action {index}: the title exceeds {MAX_SUB_TITLE} characters.")
        bpu = line.get("is_bpu", False)
        if not isinstance(bpu, bool):
            raise PlanError("bpu_invalid", f"Sub-action {index}: BPU must be true or false.")
        if bpu:
            raise PlanError(
                "bpu_inherited",
                f"Sub-action {index}: BPU is set on the request (mother action) and inherited by its sub-actions.",
            )
        # The import creates interventions and sub-actions WITHOUT owner: each one is assigned afterwards, from
        # its own sheet (existing permission and eligibility rules of the action API).
        assignee_id = line.get("assignee_id")
        if assignee_id not in ("", None):
            raise PlanError(
                "assignee_not_at_import",
                f"Sub-action {index}: owners are assigned after the import, from the sheet of each action.",
            )
        assignee_id = None
        cleaned.append(
            {
                "title": title,
                "in_progress_points": _int(line.get("in_progress_points"), f"Sub-action {index}"),
                "closed_points": _int(line.get("closed_points"), f"Sub-action {index}"),
                "assignee_id": assignee_id,
            }
        )
    try:
        validate_points(cleaned)
    except ValueError as error:
        raise PlanError("points_invalid", str(error))

    template_id = raw.get("template_id")
    template_title = ""
    if template_id not in (None, ""):
        # the company comes from the authorized project, never from the client
        template = ActionTemplate.objects.filter(
            pk=template_id if isinstance(template_id, int) and not isinstance(template_id, bool) else None,
            company_id=project.company_profile_id,
            needs_review=False,
        ).first()
        if template is None:
            raise PlanError("template_invalid", "This template does not exist for the company of the project.")
        template_id, template_title = template.pk, template.title
    else:
        template_id = None

    save_as_template = raw.get("save_as_template", False)
    if not isinstance(save_as_template, bool):
        raise PlanError("plan_invalid", "save_as_template must be true or false.")
    if save_as_template:
        if template_id is not None:
            raise PlanError("template_exists", "A template already exists for this title: it cannot be created again.")
        if user is not None and not can_manage_templates(user):
            raise PlanError("template_right_required", "Creating a reusable template needs the company configuration right.")
    return {
        "template_id": template_id,
        "template_title": template_title,
        "sub_actions": cleaned,
        "is_bpu": mother_bpu,
        "no_sub_actions": False,
        "save_as_template": save_as_template,
    }


def save_template_from_plan(project, user, title, plan):
    """Create the reusable company template under the imported title, only on explicit confirmation.

    Returns ("created", template) or ("exists", None) when a template with the same normalized
    title exists (also if another user created it since the preview, or an earlier row of the same
    file did). BPU and owners are never stored."""
    company_id = project.company_profile_id
    key = normalize_title(title)
    if company_id is None or not key:
        return "exists", None
    if ActionTemplate.objects.filter(company_id=company_id, normalized_title=key).exists():
        return "exists", None
    try:
        with transaction.atomic():
            template = ActionTemplate.objects.create(company_id=company_id, title=title, created_by=user)
    except IntegrityError:  # concurrent twin: the DB constraint is the backstop
        return "exists", None
    ActionTemplateSubAction.objects.bulk_create(
        ActionTemplateSubAction(
            template=template,
            position=index,
            title=line["title"],
            in_progress_points=line["in_progress_points"],
            closed_points=line["closed_points"],
        )
        for index, line in enumerate(plan["sub_actions"], start=1)
    )
    return "created", template
