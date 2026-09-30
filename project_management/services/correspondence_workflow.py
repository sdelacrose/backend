"""LOT 3D-3D1: internal validation workflow of an OUT Correspondence.

Mirrors DocumentWorkflow's proven service pattern (locked workflow row, immutable event
evidence, actor-assigned transitions, select_for_update against concurrent starts) but is a
small, independent implementation against CorrespondenceValidationWorkflow -- see that model's
own docstring for why this is not a shared/parameterized service.

"Start validation" is ONE action (see the model docstring): it creates the workflow AND records
the author's submission in the same call, landing directly in PENDING_VERIFICATION (2-step) or
PENDING_APPROVAL (1-step) -- never a separately-stored "created but not submitted" row state.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from models.models import User

from project_management.models import (
    Correspondence,
    CorrespondenceValidationEvent,
    CorrespondenceValidationWorkflow,
    ProjectTeamMembers,
)

Status = CorrespondenceValidationWorkflow.Status
WorkflowType = CorrespondenceValidationWorkflow.WorkflowType
Step = CorrespondenceValidationEvent.Step
Action = CorrespondenceValidationEvent.Action

# action -> (status it can be performed in, step recorded, actor attribute)
_DECISIONS = {
    Action.VERIFY: (Status.PENDING_VERIFICATION, Step.VERIFY, "verifier"),
    Action.APPROVE: (Status.PENDING_APPROVAL, Step.APPROVE, "approver"),
}
# status waiting for an action -> actor attribute (also used by reject)
_WAITING_ACTOR = {
    Status.PENDING_VERIFICATION: "verifier",
    Status.PENDING_APPROVAL: "approver",
}
_WAITING_STEP = {
    Status.PENDING_VERIFICATION: Step.VERIFY,
    Status.PENDING_APPROVAL: Step.APPROVE,
}


# --- Reads / locking ---------------------------------------------------------------

def get_latest_workflow(correspondence):
    """The most recent validation workflow of this Correspondence (at most one is active), or
    None if validation was never started."""
    return (
        CorrespondenceValidationWorkflow.objects.filter(correspondence=correspondence)
        .select_related("author", "verifier", "approver", "started_by")
        .prefetch_related("events__actor")
        .first()
    )


def validation_state(correspondence):
    """The ONE derived state an observer cares about (section 5): "DRAFT" when no workflow was
    ever started, else the latest workflow's own status. Never stored on Correspondence itself
    -- always derived, exactly like Document's own get_internal_status."""
    if correspondence.direction != Correspondence.Direction.OUT:
        return None
    workflow = get_latest_workflow(correspondence)
    return workflow.status if workflow else "DRAFT"


def has_active_workflow(correspondence):
    return CorrespondenceValidationWorkflow.objects.filter(
        correspondence=correspondence, status__in=CorrespondenceValidationWorkflow.ACTIVE_STATUSES,
    ).exists()


def is_content_locked(correspondence):
    """Section 8: content is locked from the moment validation starts, and STAYS locked once
    approved (mirrors Document: origin stays immutable after approval too, LOT 3D-3C0's own
    test_origin_stays_immutable_after_the_workflow_is_completed) -- only a REJECTED workflow
    (or none at all) leaves the Correspondence editable."""
    if correspondence.direction != Correspondence.Direction.OUT:
        return False
    return CorrespondenceValidationWorkflow.objects.filter(
        correspondence=correspondence,
        status__in=(Status.PENDING_VERIFICATION, Status.PENDING_APPROVAL, Status.APPROVED),
    ).exists()


def ready_to_send(correspondence):
    """Section 15: the ONE canonical rule the future Send lot must call -- never re-encoded on
    the frontend. TRUE only when direction is OUT, the latest workflow is APPROVED,
    Correspondence.status is still DRAFT (approval never issues it -- section 6), and the
    minimum content a send would need is present."""
    if correspondence.direction != Correspondence.Direction.OUT:
        return False
    if correspondence.status != Correspondence.Status.DRAFT:
        return False
    workflow = get_latest_workflow(correspondence)
    if workflow is None or workflow.status != Status.APPROVED:
        return False
    if not correspondence.subject:
        return False
    if not (correspondence.recipient_company_id or correspondence.recipient_contact_id):
        return False
    return True


# --- Actor scope ---------------------------------------------------------------------

def allowed_actor_queryset(correspondence):
    """Section 7: reuse the existing member-picker scoping -- exactly the frontend's own
    useInlineEditMemberOptions(sources: project ? ["project-team", "company"] : undefined), the
    SAME call the drawer's existing Responsible picker already makes. Project scope is a UNION
    of the project's own team members AND the project's company members (a brand-new project
    has no team yet, section 7's own drawer comment: "falling back to the company's own members
    keeps it the same canonical hook, just a wider candidate pool") -- not project-team alone,
    which would silently reject a perfectly valid company member the picker itself offered.
    Company scope offers the company's own members. No new people directory."""
    if correspondence.project_id:
        member_ids = ProjectTeamMembers.objects.filter(
            project_id=correspondence.project_id
        ).values_list("team_member_id", flat=True)
        company_id = correspondence.project.company_profile_id
        return User.objects.filter(
            Q(pk__in=member_ids) | Q(company_id=company_id),
            is_active=True, is_deleted=False, is_permanently_deleted=False,
        )
    if correspondence.company_profile_id:
        return User.objects.filter(
            company_id=correspondence.company_profile_id,
            is_active=True, is_deleted=False, is_permanently_deleted=False,
        )
    if correspondence.freelancer_id:
        return User.objects.filter(pk=correspondence.freelancer_id)
    return User.objects.none()


def _validate_actors(workflow_type, author, verifier, approver, allowed_users):
    errors = {}
    if workflow_type not in WorkflowType.values:
        errors["workflow_type"] = "Must be ONE_STEP or TWO_STEP."
    if author is None:
        errors["author"] = "An author is required."
    if approver is None:
        errors["approver"] = "An approver is required."
    if workflow_type == WorkflowType.TWO_STEP:
        if verifier is None:
            errors["verifier"] = "A verifier is required for a two-validation workflow."
        elif author is not None and verifier.pk == author.pk:
            errors["verifier"] = "The verifier must be different from the author."
    elif verifier is not None and workflow_type == WorkflowType.ONE_STEP:
        errors["verifier"] = "A one-validation workflow has no verifier."
    if allowed_users is not None:
        allowed = set(allowed_users.values_list("pk", flat=True))
        for field, user in (
            ("author", author), ("verifier", verifier), ("approver", approver),
        ):
            if user is not None and user.pk not in allowed:
                errors.setdefault(field, "This user cannot be selected.")
    if errors:
        raise ValidationError(errors)


# --- Writes ----------------------------------------------------------------------

def start_validation(correspondence, workflow_type, author, verifier, approver, started_by):
    """Section 7: create and immediately submit the workflow (see module docstring for why
    this is one action, not two). OUT only, DRAFT lifecycle only, no other active workflow,
    never on an ISSUED Correspondence (section 14 -- forward-looking: nothing in this lot sets
    ISSUED, but the guard belongs here regardless of what sets it later)."""
    if correspondence.direction != Correspondence.Direction.OUT:
        raise ValidationError("Only an outgoing Correspondence can start an internal validation.")
    if correspondence.status == Correspondence.Status.ISSUED:
        raise ValidationError("An issued Correspondence cannot start a validation workflow.")
    allowed_users = allowed_actor_queryset(correspondence)
    _validate_actors(workflow_type, author, verifier, approver, allowed_users)
    if workflow_type == WorkflowType.ONE_STEP:
        verifier = None
    first_status = (
        Status.PENDING_VERIFICATION if workflow_type == WorkflowType.TWO_STEP
        else Status.PENDING_APPROVAL
    )
    try:
        with transaction.atomic():
            locked = Correspondence.objects.select_for_update().get(pk=correspondence.pk)
            if CorrespondenceValidationWorkflow.objects.filter(
                correspondence=locked, status__in=CorrespondenceValidationWorkflow.ACTIVE_STATUSES,
            ).exists():
                raise ValidationError("This correspondence already has an active validation workflow.")
            workflow = CorrespondenceValidationWorkflow.objects.create(
                correspondence=locked,
                workflow_type=workflow_type,
                status=first_status,
                author=author,
                verifier=verifier,
                approver=approver,
                started_by=started_by,
            )
            CorrespondenceValidationEvent.objects.create(
                workflow=workflow, step=Step.START, actor=author, action=Action.SUBMIT,
            )
    except IntegrityError:
        raise ValidationError("This correspondence already has an active validation workflow.")
    return workflow


def perform_action(workflow, actor, action, comment):
    """Verify / approve / reject the step the workflow is waiting for. Everything is enforced
    here: only the assigned actor for the CURRENT step can act (403 -- true even for an
    otherwise full_access user, section 13), the action must match the current step (400), a
    comment is mandatory only for REJECT (section 9 -- an explicit deviation from
    DocumentWorkflow's own "always required" rule, not a DocumentWorkflow regression since that
    function is untouched)."""
    comment = (comment or "").strip()
    if action not in (*_DECISIONS, Action.REJECT):
        raise ValidationError({"action": "Unknown action."})
    if action == Action.REJECT and not comment:
        raise ValidationError({"comment": "A comment is required to reject."})

    with transaction.atomic():
        locked = CorrespondenceValidationWorkflow.objects.select_for_update().get(pk=workflow.pk)
        if not locked.is_active:
            raise ValidationError(
                f"The validation workflow is already {locked.get_status_display().lower()}."
            )
        if action == Action.REJECT:
            actor_field = _WAITING_ACTOR[locked.status]
            step = _WAITING_STEP[locked.status]
        else:
            expected_status, step, actor_field = _DECISIONS[action]
            if locked.status != expected_status:
                raise ValidationError({"action": f"Action {action} is not allowed at this step."})
        expected_actor = getattr(locked, actor_field)
        if expected_actor is None or expected_actor.pk != actor.pk:
            raise PermissionDenied("You are not the assigned actor for this step.")

        CorrespondenceValidationEvent.objects.create(
            workflow=locked, step=step, actor=actor, action=action, comment=comment,
        )
        if action == Action.REJECT:
            locked.status = Status.REJECTED
            locked.completed_at = timezone.now()
        elif action == Action.VERIFY:
            locked.status = Status.PENDING_APPROVAL
        else:  # APPROVE
            locked.status = Status.APPROVED
            locked.completed_at = timezone.now()
        # Never touches correspondence.status (section 6): approval means internally
        # APPROVED/ready to send, never automatically ISSUED.
        locked.save(update_fields=["status", "completed_at"])
        return locked
