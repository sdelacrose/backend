"""L3D: Supplier Surveillance Reviewers & Execution.

SupplierSurveillanceReviewerAssignment / SupplierSurveillanceResponse / SupplierSurveillanceEvent
all belong to ONE SupplierSurveillance (never to Document, never to the live Template or
TemplateItem -- the authoritative questionnaire is SupplierSurveillance.template_snapshot,
frozen at creation, L3C). A Response keys its Item by `item_snapshot_id` (the snapshot's own
copied `id`, not a live FK) precisely so a later Template/Item edit or deletion can never affect
an in-progress or completed review (L3C's own immutability guarantee, carried through here).

Still completely separate from DocumentWorkflow: no Author/Verifier/Approver concept, no shared
audit trail, no shared actor model (L3A/L3B/L3C section 2).
"""

from django.db import models

from models.models import User

from .supplier_surveillance import SupplierSurveillance


class SupplierSurveillanceReviewerAssignment(models.Model):
    """One internal User's own, independent review of a Surveillance's complete snapshot.

    Multiple Reviewers each get their own assignment and their own Responses (L3D section 9) --
    never a shared/mutable answer. One User can only ever have one (active) assignment per
    Surveillance (the UniqueConstraint below).
    """

    class Status(models.TextChoices):
        ASSIGNED = "ASSIGNED", "Assigned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        COMPLETED = "COMPLETED", "Completed"

    surveillance = models.ForeignKey(
        SupplierSurveillance, related_name="reviewer_assignments", on_delete=models.CASCADE,
    )
    reviewer = models.ForeignKey(
        User, related_name="supplier_surveillance_reviewer_assignments", on_delete=models.PROTECT,
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ASSIGNED)
    assigned_by = models.ForeignKey(
        User,
        related_name="assigned_supplier_surveillance_reviewers",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["assigned_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["surveillance", "reviewer"],
                name="supplier_surveillance_reviewer_unique",
            ),
        ]

    def __str__(self):
        return f"{self.reviewer} on {self.surveillance_id} ({self.status})"


class SupplierSurveillanceResponse(models.Model):
    """One Reviewer's current answer to ONE snapshot Item.

    `item_snapshot_id` is the Item's `id` as copied into template_snapshot at Surveillance
    creation (L3C) -- never a live FK to SupplierSurveillanceTemplateItem, which may later
    change or be removed entirely without affecting this row (L3D section 10).
    """

    class CheckValue(models.TextChoices):
        COMPLIANT = "COMPLIANT", "Compliant"
        NON_COMPLIANT = "NON_COMPLIANT", "Non-compliant"
        NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"

    assignment = models.ForeignKey(
        SupplierSurveillanceReviewerAssignment, related_name="responses", on_delete=models.CASCADE,
    )
    item_snapshot_id = models.PositiveIntegerField()
    # Copied from the snapshot Item at save time (never the live Template) purely so a response
    # row is self-describing; validation always re-reads the snapshot itself (L3D section 26).
    item_type = models.CharField(max_length=20)
    check_value = models.CharField(
        max_length=20, choices=CheckValue.choices, null=True, blank=True,
    )
    text_value = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["item_snapshot_id", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["assignment", "item_snapshot_id"],
                name="supplier_surveillance_response_unique_item",
            ),
        ]

    def __str__(self):
        return f"Response to item {self.item_snapshot_id} ({self.assignment_id})"


class SupplierSurveillanceEventQuerySet(models.QuerySet):
    """Audit records are evidence: never bulk-edited or bulk-deleted (same pattern as
    DocumentWorkflowEvent.ImmutableQuerySet)."""

    def update(self, **kwargs):
        from django.core.exceptions import ValidationError

        raise ValidationError("Supplier Surveillance events cannot be modified.")

    def delete(self):
        from django.core.exceptions import ValidationError

        raise ValidationError("Supplier Surveillance events cannot be deleted.")


class SupplierSurveillanceEvent(models.Model):
    """One immutable audit record of a Surveillance's execution (L3D section 20/21)."""

    class EventType(models.TextChoices):
        SURVEILLANCE_CREATED = "SURVEILLANCE_CREATED", "Surveillance created"
        REVIEWER_ASSIGNED = "REVIEWER_ASSIGNED", "Reviewer assigned"
        REVIEWER_REMOVED = "REVIEWER_REMOVED", "Reviewer removed"
        REVIEW_STARTED = "REVIEW_STARTED", "Review started"
        RESPONSE_UPDATED = "RESPONSE_UPDATED", "Response updated"
        REVIEW_COMPLETED = "REVIEW_COMPLETED", "Review completed"
        READY_FOR_CLOSURE = "READY_FOR_CLOSURE", "Ready for closure"
        SURVEILLANCE_CLOSED = "SURVEILLANCE_CLOSED", "Surveillance closed"
        # L3G-1: enum added now, never emitted until L3G-2 actually creates the evidence file.
        PDF_GENERATED = "PDF_GENERATED", "Evidence PDF generated"

    surveillance = models.ForeignKey(
        SupplierSurveillance, related_name="events", on_delete=models.CASCADE,
    )
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    actor = models.ForeignKey(
        User, related_name="supplier_surveillance_events", on_delete=models.PROTECT,
    )
    assignment = models.ForeignKey(
        SupplierSurveillanceReviewerAssignment,
        related_name="events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = SupplierSurveillanceEventQuerySet.as_manager()

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.event_type} on {self.surveillance_id}"
