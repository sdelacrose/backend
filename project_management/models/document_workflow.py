from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from models.models import User

from .document_revision import DocumentRevision


class DocumentWorkflow(models.Model):
    """Internal validation workflow of ONE DocumentRevision.

    Each revision keeps its own workflow and history; nothing here is stored on the
    global Document. The status is the step currently waiting for an action:
    DRAFT (author signs) -> VERIFY (THREE_STEP only) -> APPROVE -> APPROVED,
    or REJECTED at any active step. It is NOT a user-facing Document status.
    """

    class WorkflowType(models.TextChoices):
        TWO_STEP = "TWO_STEP", "Author -> Approver"
        THREE_STEP = "THREE_STEP", "Author -> Verifier -> Approver"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        VERIFY = "VERIFY", "Verify"
        APPROVE = "APPROVE", "Approve"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    ACTIVE_STATUSES = (Status.DRAFT, Status.VERIFY, Status.APPROVE)

    revision = models.ForeignKey(
        DocumentRevision, related_name="workflows", on_delete=models.CASCADE
    )
    workflow_type = models.CharField(max_length=20, choices=WorkflowType.choices)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    author = models.ForeignKey(
        User, related_name="authored_document_workflows", on_delete=models.PROTECT
    )
    verifier = models.ForeignKey(
        User,
        related_name="verifier_document_workflows",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    approver = models.ForeignKey(
        User, related_name="approver_document_workflows", on_delete=models.PROTECT
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_document_workflows",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One active workflow at most per revision.
            models.UniqueConstraint(
                fields=["revision"],
                condition=Q(status__in=["DRAFT", "VERIFY", "APPROVE"]),
                name="document_workflow_one_active_per_revision",
            ),
            models.CheckConstraint(
                condition=(
                    Q(workflow_type="TWO_STEP", verifier__isnull=True)
                    | Q(workflow_type="THREE_STEP", verifier__isnull=False)
                ),
                name="document_workflow_verifier_matches_type",
            ),
        ]

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def __str__(self):
        return f"workflow {self.pk} rev {self.revision_id} {self.status}"


class ImmutableQuerySet(models.QuerySet):
    """Audit records are never bulk-edited or bulk-deleted by application code."""

    def update(self, **kwargs):
        raise ValidationError("Workflow events cannot be modified.")

    def delete(self):
        raise ValidationError("Workflow events cannot be deleted.")


class DocumentWorkflowEvent(models.Model):
    """One immutable audit record of a workflow (start, signature, decision)."""

    class Step(models.TextChoices):
        START = "START", "Start"
        AUTHOR_SIGN = "AUTHOR_SIGN", "Author signature"
        VERIFY = "VERIFY", "Verification"
        APPROVE = "APPROVE", "Approval"

    class Action(models.TextChoices):
        START = "START", "Started"
        SIGN = "SIGN", "Signed"
        VALIDATE = "VALIDATE", "Validated"
        APPROVE = "APPROVE", "Approved"
        REJECT = "REJECT", "Rejected"
        CHANGE_ACTOR = "CHANGE_ACTOR", "Actor changed"

    workflow = models.ForeignKey(
        DocumentWorkflow, related_name="events", on_delete=models.CASCADE
    )
    step = models.CharField(max_length=20, choices=Step.choices)
    actor = models.ForeignKey(
        User, related_name="document_workflow_events", on_delete=models.PROTECT
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    comment = models.TextField(blank=True, default="")
    # Only for CHANGE_ACTOR events: `actor` is who made the change.
    previous_actor = models.ForeignKey(
        User,
        related_name="+",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    new_actor = models.ForeignKey(
        User,
        related_name="+",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Workflow events cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Workflow events cannot be deleted.")

    def __str__(self):
        return f"{self.workflow_id} {self.step} {self.action}"
