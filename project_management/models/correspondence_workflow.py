"""LOT 3D-3D1: internal validation workflow of an OUT Correspondence.

Deliberately NOT DocumentWorkflow (which is keyed to DocumentRevision -- Correspondence has no
revision concept). Follows the same proven shape (a locked workflow row + an immutable event
audit trail, one active workflow at a time, history preserved across a reject-and-restart) but
is its own small model, not a shared/parameterized one: the two domains stay free to diverge
without risking a DocumentWorkflow regression, per the ticket's own instruction not to duplicate
Document workflow code blindly while also not coupling the two.

Design choice (see the LOT 3D-3D1 report for the full reasoning): "Start validation" is ONE
action, not two -- it both creates the workflow AND records the author's submission in the same
call, landing directly in PENDING_VERIFICATION (2-step) or PENDING_APPROVAL (1-step). There is
no separate stored "workflow created but not yet submitted" row state; "Draft" (section 5) is
the correspondence-level state when no workflow row exists at all yet, derived, never stored
here.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from models.models import User

from .correspondence import Correspondence


class CorrespondenceWorkflowEvidenceQuerySet(models.QuerySet):
    """Workflow rows are evidence: transitions go through the service layer (instance save);
    nothing is bulk-edited or bulk-deleted. Small, local copy of DocumentWorkflow's own
    EvidenceQuerySet -- not imported, so the two models never share mutable state."""

    def update(self, **kwargs):
        raise ValidationError("Correspondence validation workflow records cannot be modified.")

    def delete(self):
        raise ValidationError("Correspondence validation workflow records cannot be deleted.")


class CorrespondenceValidationWorkflow(models.Model):
    """The internal validation workflow of ONE OUT Correspondence.

    NOT Correspondence.response_status (reply/response tracking, untouched) and NOT
    Correspondence.status (lifecycle/issue state, untouched -- approval never sets it to
    ISSUED; that is reserved for the future Send lot). One active (non-terminal) workflow at
    most per Correspondence; a rejected workflow is never reopened -- restarting creates a new
    row, so full history (including the rejection) is always preserved.
    """

    class WorkflowType(models.TextChoices):
        ONE_STEP = "ONE_STEP", "Author -> Approver"
        TWO_STEP = "TWO_STEP", "Author -> Verifier -> Approver"
        # CORR-SIGN-1: the author alone signs. Same engine as ONE_STEP with author == approver
        # fixed server-side to the user who starts it; no verifier. An internal validation
        # signature, NOT a certified electronic signature.
        SIGN_AUTHOR = "SIGN_AUTHOR", "Simple signature -> Author"

    class Status(models.TextChoices):
        PENDING_VERIFICATION = "PENDING_VERIFICATION", "Pending verification"
        PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    ACTIVE_STATUSES = (Status.PENDING_VERIFICATION, Status.PENDING_APPROVAL)

    correspondence = models.ForeignKey(
        Correspondence, related_name="validation_workflows", on_delete=models.CASCADE
    )
    workflow_type = models.CharField(max_length=20, choices=WorkflowType.choices)
    status = models.CharField(max_length=25, choices=Status.choices)
    author = models.ForeignKey(
        User, related_name="authored_correspondence_workflows", on_delete=models.PROTECT
    )
    verifier = models.ForeignKey(
        User, related_name="verifier_correspondence_workflows", on_delete=models.PROTECT,
        null=True, blank=True,
    )
    approver = models.ForeignKey(
        User, related_name="approver_correspondence_workflows", on_delete=models.PROTECT
    )
    started_by = models.ForeignKey(
        User, related_name="started_correspondence_workflows", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    objects = CorrespondenceWorkflowEvidenceQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["correspondence"],
                condition=Q(status__in=["PENDING_VERIFICATION", "PENDING_APPROVAL"]),
                name="correspondence_workflow_one_active_per_correspondence",
            ),
            models.CheckConstraint(
                condition=(
                    Q(workflow_type__in=["ONE_STEP", "SIGN_AUTHOR"], verifier__isnull=True)
                    | Q(workflow_type="TWO_STEP", verifier__isnull=False)
                ),
                name="correspondence_workflow_verifier_matches_type",
            ),
        ]

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = (
                CorrespondenceValidationWorkflow.objects.filter(pk=self.pk)
                .values_list("status", flat=True)
                .first()
            )
            if stored is not None and stored not in self.ACTIVE_STATUSES:
                raise ValidationError("A completed validation workflow cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Correspondence validation workflows cannot be deleted.")

    def __str__(self):
        return f"validation workflow {self.pk} correspondence {self.correspondence_id} {self.status}"


class CorrespondenceValidationEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Correspondence validation events cannot be modified.")

    def delete(self):
        raise ValidationError("Correspondence validation events cannot be deleted.")


class CorrespondenceValidationEvent(models.Model):
    """One immutable audit record of a CorrespondenceValidationWorkflow (start/submit,
    verification, approval, rejection)."""

    class Step(models.TextChoices):
        START = "START", "Start"
        VERIFY = "VERIFY", "Verification"
        APPROVE = "APPROVE", "Approval"

    class Action(models.TextChoices):
        SUBMIT = "SUBMIT", "Submitted"
        VERIFY = "VERIFY", "Verified"
        APPROVE = "APPROVE", "Approved"
        REJECT = "REJECT", "Rejected"

    workflow = models.ForeignKey(
        CorrespondenceValidationWorkflow, related_name="events", on_delete=models.CASCADE
    )
    step = models.CharField(max_length=20, choices=Step.choices)
    actor = models.ForeignKey(
        User, related_name="correspondence_validation_events", on_delete=models.PROTECT
    )
    action = models.CharField(max_length=20, choices=Action.choices)
    # Mandatory for REJECT; optional ("No comment" behaviour, section 9) for the positive
    # actions -- the frontend inserts a literal "No comment" when the actor leaves it blank
    # rather than the backend forcing one, so an intentionally blank comment stays blank here.
    comment = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CorrespondenceValidationEventQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Correspondence validation events cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Correspondence validation events cannot be deleted.")
