from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, OuterRef, Q, Subquery, When
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from project_management.models import (
    Document,
    DocumentOrigin,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
)

Status = DocumentWorkflow.Status
WorkflowType = DocumentWorkflow.WorkflowType
Step = DocumentWorkflowEvent.Step
Action = DocumentWorkflowEvent.Action

# Derived INTERNAL status of a document (validation lifecycle of its CURRENT version).
# It is never stored and never written to Document.status (the configurable business status).
INTERNAL_TO_CREATE = "TO_CREATE"  # register entry without any main controlled file/version
INTERNAL_DRAFT = "DRAFT"
INTERNAL_IN_VALIDATION = "IN_VALIDATION"
INTERNAL_APPROVED = "APPROVED"

# action -> (status it can be performed in, step recorded, actor attribute)
_DECISIONS = {
    Action.SIGN: (Status.DRAFT, Step.AUTHOR_SIGN, "author"),
    Action.VALIDATE: (Status.VERIFY, Step.VERIFY, "verifier"),
    Action.APPROVE: (Status.APPROVE, Step.APPROVE, "approver"),
}
# status waiting for an action -> (step, actor attribute) (also used by reject)
_WAITING = {
    Status.DRAFT: (Step.AUTHOR_SIGN, "author"),
    Status.VERIFY: (Step.VERIFY, "verifier"),
    Status.APPROVE: (Step.APPROVE, "approver"),
}


def _next_status(workflow, action):
    if action == Action.SIGN:
        return (
            Status.VERIFY
            if workflow.workflow_type == WorkflowType.THREE_STEP
            else Status.APPROVE
        )
    if action == Action.VALIDATE:
        return Status.APPROVE
    return Status.APPROVED


# --- Reads / locking -------------------------------------------------------------

def get_workflow_for_revision(revision):
    """Latest workflow of a revision (at most one is active), or None."""
    return (
        DocumentWorkflow.objects.filter(revision=revision)
        .select_related("author", "verifier", "approver", "created_by")
        .prefetch_related("events__actor")
        .first()
    )


def annotate_current_workflow(queryset):
    """Annotate Documents with the workflow of their CURRENT (latest) revision only.

    wf_status: status of that revision's latest workflow (None: no revision/workflow)
    wf_expected_actor_id: user expected to act now (author at DRAFT, verifier at VERIFY,
    approver at APPROVE; None once the workflow is APPROVED/REJECTED).
    Correlated subqueries: no per-row queries, safe with pagination and counts.
    """
    if "wf_status" in queryset.query.annotations:
        return queryset
    current_revision = DocumentRevision.objects.filter(
        document=OuterRef("pk")
    ).order_by("-created_at", "-id")
    latest_workflow = DocumentWorkflow.objects.filter(
        revision=OuterRef("current_revision_id")
    ).order_by("-created_at", "-id")
    expected = Case(
        When(status=Status.DRAFT, then=F("author_id")),
        When(status=Status.VERIFY, then=F("verifier_id")),
        When(status=Status.APPROVE, then=F("approver_id")),
        default=None,
        output_field=IntegerField(),
    )
    return queryset.annotate(
        current_revision_id=Subquery(current_revision.values("pk")[:1])
    ).annotate(
        # Official Revision of the CURRENT version (null until it is approved).
        current_revision_label=Subquery(current_revision.values("revision_label")[:1]),
        wf_status=Subquery(latest_workflow.values("status")[:1]),
        wf_expected_actor_id=Subquery(
            latest_workflow.annotate(expected=expected).values("expected")[:1]
        ),
    )


def filter_awaiting_user(queryset, user):
    """Documents whose current revision waits for an action from `user`."""
    return annotate_current_workflow(queryset).filter(wf_expected_actor_id=user.pk)


def revision_has_active_workflow(revision):
    return DocumentWorkflow.objects.filter(
        revision=revision, status__in=DocumentWorkflow.ACTIVE_STATUSES
    ).exists()


def get_current_revision(document):
    return (
        DocumentRevision.objects.filter(document=document)
        .order_by("-created_at", "-id")
        .first()
    )


def internal_status_from_workflow_status(workflow_status):
    """Canonical derivation: workflow status of the CURRENT version -> internal status.

    No workflow (or a REJECTED one) -> DRAFT; DRAFT/VERIFY/APPROVE -> IN_VALIDATION;
    APPROVED -> APPROVED. Older versions never matter.
    """
    if workflow_status in DocumentWorkflow.ACTIVE_STATUSES:
        return INTERNAL_IN_VALIDATION
    if workflow_status == Status.APPROVED:
        return INTERNAL_APPROVED
    return INTERNAL_DRAFT


def get_internal_status(document):
    """Internal status of a document. Uses the list annotation when present (no extra query).

    TO_CREATE: no stored version AND no Document.file (supporting attachments never count).
    A legacy document (file, no stored version) is a synthetic Version 1: DRAFT or workflow-driven.
    """
    if hasattr(document, "wf_status"):
        if getattr(document, "current_revision_id", None) is None and not document.file:
            return INTERNAL_TO_CREATE
        return internal_status_from_workflow_status(document.wf_status)
    current = get_current_revision(document)
    if current is None and not document.file:
        return INTERNAL_TO_CREATE
    latest = (
        DocumentWorkflow.objects.filter(revision=current)
        .order_by("-created_at", "-id")
        .values_list("status", flat=True)
        .first()
        if current
        else None
    )
    return internal_status_from_workflow_status(latest)


def assert_document_file_unlocked(document):
    """The current revision/file of a document cannot change during an active workflow."""
    current = get_current_revision(document)
    if current and revision_has_active_workflow(current):
        raise ValidationError(
            f"Revision {current.label} is locked by an active workflow."
        )




def document_has_workflow(document):
    """Any internal validation workflow (active or historical) on any version of the document."""
    return DocumentWorkflow.objects.filter(revision__document=document).exists()


DOCUMENT_EVIDENCE_MESSAGE = (
    "This document has validation workflow history or an approved revision "
    "and cannot be deleted."
)


def document_has_evidence(document):
    """Documentary evidence exists: any workflow, workflow event or official revision label.

    A plain draft (versions without any workflow) is not evidence and stays deletable.
    """
    if not document.pk:
        return False
    return (
        DocumentWorkflow.objects.filter(revision__document=document).exists()
        or DocumentWorkflowEvent.objects.filter(
            workflow__revision__document=document
        ).exists()
        or DocumentRevision.objects.filter(
            document=document, revision_label__isnull=False
        ).exists()
    )


FOLDER_EVIDENCE_MESSAGE = (
    "This folder contains documents with validation workflow history or an approved "
    "revision and cannot be deleted."
)
PROJECT_EVIDENCE_MESSAGE = (
    "This project contains documents with validation workflow history or an approved "
    "revision and cannot be deleted."
)


def _documents_with_evidence(documents):
    return documents.filter(
        Q(revisions__workflows__isnull=False) | Q(revisions__revision_label__isnull=False)
    )


def folder_has_evidence(folder):
    """Preflight for a folder deletion: evidence in the folder or any subfolder."""
    from project_management.models import Folder

    ids = [folder.pk]
    frontier = [folder.pk]
    while frontier:
        frontier = list(
            Folder.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        )
        ids.extend(frontier)
    return _documents_with_evidence(Document.objects.filter(folder_id__in=ids)).exists()


def project_has_evidence(project):
    """Preflight for a project deletion: evidence in any document of the project."""
    return _documents_with_evidence(
        Document.objects.filter(folder__project=project)
    ).exists()


def assert_internal_origin(document):
    """Only INTERNAL documents can use the internal 2/3-signature workflow (server-side rule)."""
    if document.document_origin != DocumentOrigin.INTERNAL:
        raise ValidationError(
            "The internal validation workflow is available for internal documents only."
        )


# --- Writes ----------------------------------------------------------------------

def _validate_actors(workflow_type, author, verifier, approver, allowed_users):
    errors = {}
    if workflow_type not in WorkflowType.values:
        errors["workflow_type"] = "Must be TWO_STEP or THREE_STEP."
    if author is None:
        errors["author"] = "An author is required."
    if approver is None:
        errors["approver"] = "An approver is required."
    if workflow_type == WorkflowType.THREE_STEP:
        if verifier is None:
            errors["verifier"] = "A verifier is required for a three-step workflow."
        elif author is not None and verifier.pk == author.pk:
            errors["verifier"] = "The verifier must be different from the author."
    elif verifier is not None and workflow_type == WorkflowType.TWO_STEP:
        errors["verifier"] = "A two-step workflow has no verifier."
    if allowed_users is not None:
        allowed = set(allowed_users.values_list("pk", flat=True))
        for field, user in (
            ("author", author),
            ("verifier", verifier),
            ("approver", approver),
        ):
            if user is not None and user.pk not in allowed:
                errors.setdefault(field, "This user cannot be selected.")
    if errors:
        raise ValidationError(errors)


def resolve_workflow_type(document, workflow_type):
    """An explicit workflow type is kept as is; when omitted, the project's workflow.default."""
    if workflow_type:
        return workflow_type
    from project_management.services.document_control_settings import (
        get_document_control_settings,
    )

    return get_document_control_settings(document)["workflow"]["default"]


def start_workflow(
    revision, workflow_type, author, verifier, approver, created_by, allowed_users=None
):
    """Create and start the workflow of a revision (status DRAFT, waiting for the author).

    A revision has one workflow: resubmission after a rejection is not implemented.
    """
    assert_internal_origin(revision.document)
    workflow_type = resolve_workflow_type(revision.document, workflow_type)
    _validate_actors(workflow_type, author, verifier, approver, allowed_users)
    if workflow_type == WorkflowType.TWO_STEP:
        verifier = None
    try:
        with transaction.atomic():
            # Serialize concurrent starts for the same revision.
            locked = DocumentRevision.objects.select_for_update().get(pk=revision.pk)
            if DocumentWorkflow.objects.filter(revision=locked).exists():
                raise ValidationError("This revision already has a workflow.")
            now = timezone.now()
            workflow = DocumentWorkflow.objects.create(
                revision=locked,
                workflow_type=workflow_type,
                status=Status.DRAFT,
                author=author,
                verifier=verifier,
                approver=approver,
                created_by=created_by,
                started_at=now,
            )
            DocumentWorkflowEvent.objects.create(
                workflow=workflow,
                step=Step.START,
                actor=created_by,
                action=Action.START,
            )
    except IntegrityError:
        raise ValidationError("This revision already has an active workflow.")
    return workflow


def start_workflow_for_document(
    document, workflow_type, author, verifier, approver, created_by, allowed_users=None
):
    """Start the workflow of the document's CURRENT revision.

    A legacy document (no stored revision yet) gets its first real revision
    materialized from its current file, in the same transaction: nothing is created
    when the workflow cannot start.
    """
    from project_management.services.document_revision import (
        ensure_current_revision,
    )

    assert_internal_origin(document)
    workflow_type = resolve_workflow_type(document, workflow_type)
    _validate_actors(workflow_type, author, verifier, approver, allowed_users)
    with transaction.atomic():
        revision = ensure_current_revision(document)
        if revision is None:
            raise ValidationError("This document has no file to review.")
        return start_workflow(
            revision,
            workflow_type,
            author,
            verifier,
            approver,
            created_by,
            allowed_users=allowed_users,
        )


def perform_action(workflow, actor, action, comment):
    """Sign / validate / approve / reject the step the workflow is waiting for.

    Everything is enforced here: only the person assigned to the current step can act
    (403), the action must match the current step (400), a comment is mandatory.
    """
    comment = (comment or "").strip()
    if action not in (*_DECISIONS, Action.REJECT):
        raise ValidationError({"action": "Unknown action."})
    if not comment:
        raise ValidationError({"comment": "A comment is required."})

    with transaction.atomic():
        locked = DocumentWorkflow.objects.select_for_update().get(pk=workflow.pk)
        if not locked.is_active:
            raise ValidationError(
                f"The workflow is already {locked.get_status_display().lower()}."
            )

        step, actor_field = _WAITING[locked.status]
        if action != Action.REJECT:
            expected_status, step, actor_field = _DECISIONS[action]
            if locked.status != expected_status:
                raise ValidationError(
                    {"action": f"Action {action} is not allowed at this step."}
                )
        expected_actor = getattr(locked, actor_field)
        if expected_actor is None or expected_actor.pk != actor.pk:
            raise PermissionDenied("You are not the assigned actor for this step.")

        DocumentWorkflowEvent.objects.create(
            workflow=locked, step=step, actor=actor, action=action, comment=comment
        )
        if action == Action.REJECT:
            locked.status = Status.REJECTED
            locked.completed_at = timezone.now()
        else:
            locked.status = _next_status(locked, action)
            if locked.status == Status.APPROVED:
                locked.completed_at = timezone.now()
        locked.save(update_fields=["status", "completed_at"])
        if locked.status == Status.APPROVED:
            # Same transaction as the approval.
            from project_management.services.document_revision import (
                assign_revision_label,
                record_approval_evidence,
            )

            assign_revision_label(locked.revision)
            record_approval_evidence(locked.revision, locked)
        return locked


# --- Actor replacement -------------------------------------------------------------

_ROLE_STEP = {"verifier": Step.VERIFY, "approver": Step.APPROVE}
# Statuses in which the role's own step is not done yet (so the actor can still change).
_REPLACEABLE_IN = {
    "verifier": (Status.DRAFT, Status.VERIFY),
    "approver": (Status.DRAFT, Status.VERIFY, Status.APPROVE),
}


def change_actor(workflow, changed_by, role, new_user, comment, allowed_users=None):
    """The workflow author replaces the verifier or approver before that step is done.

    Never for a terminal workflow or an already completed step; the reason is
    mandatory and an immutable CHANGE_ACTOR event records old actor, new actor, who
    changed it and when. This is not a delegation system.
    """
    comment = (comment or "").strip()
    if role not in _ROLE_STEP:
        raise ValidationError({"role": "Only the verifier or the approver can be replaced."})
    if new_user is None:
        raise ValidationError({"new_actor": "A new actor is required."})
    if not comment:
        raise ValidationError({"comment": "A comment is required."})

    with transaction.atomic():
        locked = DocumentWorkflow.objects.select_for_update().get(pk=workflow.pk)
        if locked.author_id != changed_by.pk:
            raise PermissionDenied("Only the workflow author can replace an actor.")
        if not locked.is_active:
            raise ValidationError(
                f"The workflow is already {locked.get_status_display().lower()}."
            )
        if role == "verifier" and locked.workflow_type != WorkflowType.THREE_STEP:
            raise ValidationError({"role": "This workflow has no verifier."})
        if locked.status not in _REPLACEABLE_IN[role]:
            raise ValidationError({"role": f"The {role} step is already completed."})

        previous = getattr(locked, role)
        if previous.pk == new_user.pk:
            raise ValidationError({"new_actor": "This user already holds the role."})
        if role == "verifier" and new_user.pk == locked.author_id:
            raise ValidationError(
                {"new_actor": "The verifier must be different from the author."}
            )
        if allowed_users is not None and not allowed_users.filter(pk=new_user.pk).exists():
            raise ValidationError({"new_actor": "This user cannot be selected."})

        DocumentWorkflowEvent.objects.create(
            workflow=locked,
            step=_ROLE_STEP[role],
            actor=changed_by,
            action=Action.CHANGE_ACTOR,
            comment=comment,
            previous_actor=previous,
            new_actor=new_user,
        )
        setattr(locked, role, new_user)
        locked.save(update_fields=[role])
        return locked
