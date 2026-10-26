"""L3G-1: Supplier Surveillance final evidence foundation.

SupplierSurveillanceEvidence is the ONE official final evidence record of a CLOSED Surveillance
(L3G architecture checkpoint, section 2/3) -- never a Livrable, never an ordinary Document/
DocumentRevision: it must never appear in Project -> Documents -> Livrables, so it deliberately
stores its file independently of the Document/Folder system entirely (same "plain FileField on
a dedicated, non-Document model" shape as project_management.models.project.ObjectAssessment.file,
reused as the storage precedent -- see ecpmpapp.constants.UPLOAD_PATH_SUPPLIER_SURVEILLANCE_
EVIDENCE).

CREATE once, READ thereafter (L3G-1 section 5): no UPDATE, no DELETE, no REPLACE through the
normal application. Enforced at BOTH the QuerySet level (same ImmutableQuerySet shape as
SupplierSurveillanceEvent) AND the instance level (same stronger save()/delete() override shape
as DocumentWorkflow/DocumentWorkflowEvent) -- a QuerySet-only guard does not protect against a
single instance's own .save()/.delete() call, and this is explicitly described as "the official
historical record", warranting the stronger of the two existing precedents in this codebase.

L3G-1 only prepares this model + its metadata-snapshot builder (services.
supplier_surveillance_evidence) -- no PDF rendering, no file write, no row creation happens yet
(L3G-2's responsibility).
"""

from django.core.exceptions import ValidationError
from django.db import models

from models.models import User

from .supplier_surveillance import SupplierSurveillance


def get_supplier_surveillance_evidence_upload_path(instance, filename):
    from ecpmpapp.constants import UPLOAD_PATH_SUPPLIER_SURVEILLANCE_EVIDENCE

    return UPLOAD_PATH_SUPPLIER_SURVEILLANCE_EVIDENCE.format(
        project_id=instance.surveillance.project_id,
        surveillance_reference=instance.surveillance.reference,
        filename=filename,
    )


class SupplierSurveillanceEvidenceQuerySet(models.QuerySet):
    """The official final evidence record is never bulk-edited or bulk-deleted (same
    ImmutableQuerySet pattern as SupplierSurveillanceEvent / DocumentWorkflowEvent)."""

    def update(self, **kwargs):
        raise ValidationError("Supplier Surveillance evidence cannot be modified.")

    def delete(self):
        raise ValidationError("Supplier Surveillance evidence cannot be deleted.")


class SupplierSurveillanceEvidence(models.Model):
    """The one official final evidence PDF of a CLOSED Supplier Surveillance.

    OneToOne: at most one official final evidence record per Surveillance (L3G-1 section 3).
    """

    surveillance = models.OneToOneField(
        SupplierSurveillance, related_name="evidence", on_delete=models.CASCADE,
    )
    file = models.FileField(upload_to=get_supplier_surveillance_evidence_upload_path)
    generated_at = models.DateTimeField(auto_now_add=True)
    # Informational provenance only (same on_delete choice as SupplierSurveillance.created_by/
    # DocumentWorkflow.created_by) -- never load-bearing for the evidence's own meaning.
    generated_by = models.ForeignKey(
        User,
        related_name="generated_supplier_surveillance_evidence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Same frozen-hash convention as DocumentRevision.file_sha256 -- integrity evidence of the
    # exact stored bytes, not an identity field.
    file_sha256 = models.CharField(max_length=64, null=True, blank=True)
    # Immutable identity-sensitive snapshot (L3G-1 section 6) -- see services.
    # supplier_surveillance_evidence.build_evidence_metadata. Never the detailed Q&A, which
    # remains authoritative in template_snapshot + SupplierSurveillanceResponse (section 11).
    evidence_metadata = models.JSONField(default=dict, blank=True)

    objects = SupplierSurveillanceEvidenceQuerySet.as_manager()

    class Meta:
        ordering = ["-generated_at", "-id"]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Supplier Surveillance evidence cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Supplier Surveillance evidence cannot be deleted.")

    def __str__(self):
        return f"Evidence for {self.surveillance_id}"
