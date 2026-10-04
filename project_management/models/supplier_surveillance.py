"""L3B: Supplier Surveillance -- foundation only.

Supplier Surveillance is a completely separate business process from the INTERNAL
DocumentWorkflow (L3A architecture checkpoint, confirmed in L3B's ticket section 2): it belongs
to an exact SUPPLIER-origin DocumentRevision, never to DocumentWorkflow.author/.verifier/
.approver or to DocumentWorkflow itself -- no shared base model, no shared counter, no shared
audit trail, even though both are "review processes" of a DocumentRevision.

This module is deliberately the smallest possible root: reviewers, template library, audit
events, PDF, closure automation, due dates and notifications are all out of scope for L3B and
remain future lots (L3C-L3G, per the L3A mapping). `template_snapshot`, `result` and
`new_revision_required` exist now purely to stabilize the schema so later lots never need a
destructive migration -- L3B itself never populates them.
"""

from django.db import models

from models.models import User

from .project import Project
from .document_revision import DocumentRevision


class SupplierSurveillance(models.Model):
    """One Supplier Surveillance cycle, permanently attached to an exact DocumentRevision.

    A later Supplier Revision of the same Document gets its own, separate SupplierSurveillance
    row -- this one is never reused or rewritten (L3A section 3/15): the OneToOneField below is
    the V1 "one Surveillance per revision" business rule, enforced at the database level.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        READY_FOR_CLOSURE = "READY_FOR_CLOSURE", "Ready for closure"
        CLOSED = "CLOSED", "Closed"

    class Result(models.TextChoices):
        REVIEWED = "REVIEWED", "Reviewed"
        COMMENTED = "COMMENTED", "Commented"
        REJECTED = "REJECTED", "Rejected"

    # Denormalized for security scoping/filtering/numbering (L3B section 5) -- derived once at
    # creation from document_revision.document.folder.project and never independently editable
    # (no field here is exposed by any update endpoint; there is no update endpoint at all yet).
    project = models.ForeignKey(
        Project, related_name="supplier_surveillances", on_delete=models.CASCADE,
    )
    document_revision = models.OneToOneField(
        DocumentRevision, related_name="supplier_surveillance", on_delete=models.PROTECT,
    )
    # Permanent, unique, assigned once by services.supplier_surveillance and never reassigned.
    # Exact format deliberately not finalized (L3A section 10 / L3B section 6) -- see
    # services.supplier_surveillance.generate_supplier_surveillance_reference.
    reference = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    # Reserved for L3E closure: NULL until a Surveillance is actually closed.
    result = models.CharField(max_length=20, choices=Result.choices, null=True, blank=True)
    # Reserved for L3E closure: NULL until `result` is set (REVIEWED -> False, COMMENTED/
    # REJECTED -> True). Never a computed "next revision label" -- just this boolean fact.
    new_revision_required = models.BooleanField(null=True, blank=True)
    # Reserved for L3D (template library): an immutable copy of the template's sections/items
    # at creation time, so a later template edit can never alter an existing Surveillance. L3B
    # never writes to this field.
    template_snapshot = models.JSONField(null=True, blank=True)
    # Same role as DocumentWorkflow.created_by (a business row's creator, not itself evidence of
    # an action the way DocumentWorkflowEvent.actor is) -- same on_delete choice for the same
    # reason: informational, never load-bearing for the Surveillance's own business meaning.
    created_by = models.ForeignKey(
        User,
        related_name="created_supplier_surveillances",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Reserved for L3E closure: never set by L3B.
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.reference


class SupplierSurveillanceReferenceCounter(models.Model):
    """A locked, per-project sequence counter for Supplier Surveillance references --

    mirrors TransmittalReferenceCounter exactly (same project-only scope, same shape): a fully
    separate table/state from Document's, Correspondence's or Transmittal's own counters, so a
    future change to any of those numbering schemes can never affect this one and vice versa.
    """

    project = models.OneToOneField(
        Project,
        related_name="supplier_surveillance_reference_counter",
        on_delete=models.CASCADE,
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
