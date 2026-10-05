"""LOT 3C: Transmittal -- a formal transmission record / bordereau.

Records WHO transmitted WHAT exact DocumentRevisions TO/FROM WHOM and WHEN. It is evidence of
transmission/receipt ONLY: it never gains a review cycle (no APPROVED/COMMENTED/REJECTED/
ANSWERED/OVERDUE) and never uses Correspondence.response_required. Kept as a fully separate
domain model from Correspondence (LOT 3B) -- no shared base "Communication" model, no shared
counters/permissions -- even though both will later be displayed together in a single
Documents -> Correspondances register.

LOT 3C amendment: every Transmittal is project-scoped (no company/library scope). Its
`transmittal_number` means two different things by direction (LOT 3C amendment section 4):
OUT -- a LINKS-generated permanent reference (TR-{PROJECT_CODE}-SEQUENCE, via
TransmittalReferenceCounter); IN -- the SUPPLIER's own transmittal reference, entered manually
and never replaced with a generated one. Both are frozen the moment they are set (at
creation), by the same save() guard.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from simple_history.models import HistoricalRecords

from models.models import User

from .contact import ContactCompany, ContactUser
from .document_revision import DocumentRevision
from .project import Project


class Transmittal(models.Model):
    """A formal IN/OUT transmission record. Evidence only -- no review/approval status."""

    class Direction(models.TextChoices):
        IN = "IN", "Incoming"
        OUT = "OUT", "Outgoing"

    class Status(models.TextChoices):
        """Minimal one-way lifecycle, matching Correspondence (LOT 3B stabilization): a
        prepared OUT transmittal vs. one formally issued/received. Never a workflow engine."""

        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"

    # LOT 3C amendment section 1: every Transmittal is project-scoped -- no company/library
    # scope exists for this LOT.
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="transmittals")

    # OUT: LINKS-generated, permanent (TR-{PROJECT_CODE}-SEQUENCE). IN: the Supplier's own
    # reference, entered manually and preserved exactly -- never replaced with a generated one
    # (LOT 3C amendment sections 2/3/4). Frozen the moment it is first set (see save()).
    transmittal_number = models.CharField(max_length=255, blank=True, default="", db_index=True)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)

    # Neutral name so the same field works for both directions: OUT -> the recipient Client;
    # IN -> the sender Supplier. Canonical Network, never duplicated. company_role is free
    # text on ContactCompany, so it is not relied on as the integrity mechanism: tenant
    # ownership is validated instead, against the Transmittal's own project (LOT 3C amendment
    # section 6) -- see serializers.transmittal.
    external_company = models.ForeignKey(
        ContactCompany, on_delete=models.SET_NULL, related_name="transmittals",
        null=True, blank=True,
    )
    external_contacts = models.ManyToManyField(
        ContactUser, related_name="transmittals", blank=True,
    )

    subject = models.CharField(max_length=500, null=True, blank=True)
    message = models.TextField(null=True, blank=True)

    issued_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    # Header-level evidence frozen at finalization (external party display names). Per-item
    # document/revision evidence lives on TransmittalItem instead.
    evidence_snapshot = models.JSONField(null=True, blank=True)

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name="created_transmittals",
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # LOT 3C amendment section 5: an OUT reference is unique per project (guaranteed
            # by the locked counter already; this is the DB-level backstop, mirroring
            # Document's/Correspondence's own "runtime protection on top of the locked
            # counter"). An IN (supplier) reference is NOT globally unique -- only the
            # composite (project, external Supplier company, supplier reference) must be
            # unique, so the same supplier reference legitimately exists for another project
            # or another supplier.
            models.UniqueConstraint(
                fields=["project", "transmittal_number"],
                condition=Q(direction="OUT"),
                name="transmittal_out_number_unique_per_project",
            ),
            models.UniqueConstraint(
                fields=["project", "external_company", "transmittal_number"],
                condition=Q(direction="IN"),
                name="transmittal_in_supplier_reference_unique",
            ),
        ]

    def __str__(self):
        return self.transmittal_number or f"Transmittal #{self.pk}"

    @property
    def is_issued(self):
        return self.status == self.Status.ISSUED

    def save(self, *args, **kwargs):
        if self.pk:
            original = Transmittal.objects.get(pk=self.pk)
            if original.transmittal_number and self.transmittal_number != original.transmittal_number:
                raise ValidationError("The transmittal number/reference cannot be changed.")
            if original.transmittal_number and self.project_id != original.project_id:
                # LOT 3C amendment section 2: "Once allocated: reference cannot change,
                # project cannot change" -- the reference is always allocated at creation for
                # both directions, so this is effectively permanent from creation.
                raise ValidationError("The transmittal project cannot be changed.")
            if self.direction != original.direction:
                raise ValidationError("The transmittal direction cannot be changed.")
            if original.status == self.Status.ISSUED and self.status == self.Status.DRAFT:
                raise ValidationError("An issued transmittal cannot be reverted to draft.")
            if original.status == self.Status.ISSUED:
                # Frozen evidence: nothing else about an issued transmittal may change either.
                # Items/contacts are protected separately (FK/M2M below).
                for field in (
                    "project_id", "external_company_id", "subject", "message",
                    "issued_at", "received_at",
                ):
                    if getattr(self, field) != getattr(original, field):
                        raise ValidationError("An issued transmittal is immutable.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.status == self.Status.ISSUED:
            raise ValidationError("An issued transmittal cannot be deleted.")
        super().delete(*args, **kwargs)


@receiver(m2m_changed, sender=Transmittal.external_contacts.through)
def protect_issued_transmittal_contacts(sender, instance, action, **kwargs):
    """Model-level guard (not just serializer/view): once ISSUED, external_contacts is frozen
    evidence -- block any add/remove/clear (guards against the M2M mutation loophole)."""
    if action in ("pre_add", "pre_remove", "pre_clear") and instance.status == Transmittal.Status.ISSUED:
        raise ValidationError("An issued transmittal's external contacts cannot be changed.")


class TransmittalItem(models.Model):
    """One exact DocumentRevision included in a Transmittal, with its intended treatment.

    `distribution_purpose` describes the treatment of the REVISION, not a Transmittal-level
    review status: no PENDING/APPROVED/COMMENTED/REJECTED belongs here. LOT 3C amendment
    section 7: nullable, because FOR_REVIEW/FOR_INFORMATION does not make business sense for
    an incoming Supplier item (no false value is forced there) -- still required for OUT,
    enforced in services.transmittal_items rather than at the DB layer.
    """

    class DistributionPurpose(models.TextChoices):
        FOR_REVIEW = "FOR_REVIEW", "For review"
        FOR_INFORMATION = "FOR_INFORMATION", "For information"

    transmittal = models.ForeignKey(Transmittal, related_name="items", on_delete=models.CASCADE)
    document_revision = models.ForeignKey(
        DocumentRevision, related_name="transmittal_items", on_delete=models.PROTECT,
    )
    distribution_purpose = models.CharField(
        max_length=20, choices=DistributionPurpose.choices, null=True, blank=True,
    )
    # Frozen at finalization only: document permanent reference, revision label, file name,
    # file SHA-256, distribution purpose. Never touches DocumentRevision.
    evidence_snapshot = models.JSONField(null=True, blank=True)
    # L3H-3: the frozen Client Review timing snapshot -- populated ONLY at successful OUT
    # finalization (never on a DRAFT item), and ONLY for a FOR_REVIEW item (never
    # FOR_INFORMATION, section 7/10/11). Computed ONCE from the L3H-1 effective
    # client_review_duration_days + this exact Transmittal's own issued_at -- never recomputed
    # later, even if the Project/Document Type configuration subsequently changes (section 9).
    # Both nullable: a DRAFT item, a FOR_INFORMATION item, and every historical row created
    # before L3H-3 (never backfilled, section 26) legitimately have neither value. `is_overdue`
    # is deliberately NOT a field here -- it stays a derived read-time condition (serializers.
    # transmittal.TransmittalItemSerializer.get_is_overdue), never a stored column and never
    # folded into the Client business status (section 16).
    review_duration_days = models.PositiveIntegerField(null=True, blank=True)
    review_due_date = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["transmittal", "document_revision"],
                name="transmittal_item_unique_revision",
            ),
        ]

    def delete(self, *args, **kwargs):
        if self.transmittal.status == Transmittal.Status.ISSUED:
            raise ValidationError("An item of an issued transmittal cannot be removed.")
        super().delete(*args, **kwargs)


class TransmittalEvent(models.Model):
    """Append-only evidence/history of a Transmittal -- never editable/deletable through the
    application. Supplements, never replaces, HistoricalRecords."""

    class EventType(models.TextChoices):
        CREATED = "CREATED", "Created"
        ITEM_ADDED = "ITEM_ADDED", "Item added"
        ITEM_REMOVED = "ITEM_REMOVED", "Item removed"
        ISSUED = "ISSUED", "Issued"
        RECEIVED = "RECEIVED", "Received"

    transmittal = models.ForeignKey(Transmittal, related_name="events", on_delete=models.CASCADE)
    event_type = models.CharField(max_length=20, choices=EventType.choices)
    actor = models.ForeignKey(
        User, related_name="transmittal_events", on_delete=models.SET_NULL, null=True, blank=True,
    )
    metadata = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Transmittal events cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Transmittal events cannot be deleted.")


class TransmittalReferenceCounter(models.Model):
    """A locked, per-project sequence counter for OUT Transmittal numbers -- a fully separate
    table/state from Document's or Correspondence's own counters. LOT 3C amendment section 1:
    project-only (no company/library scope), so one counter per project (OneToOne) rather than
    the owner-scope-triple pattern used elsewhere. Never consulted for IN (LOT 3C amendment
    section 3/5): a supplier reference is entered manually and allocates no sequence here.
    """

    project = models.OneToOneField(
        Project, related_name="transmittal_reference_counter", on_delete=models.CASCADE,
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
