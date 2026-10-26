"""HISTORY-1: ClientIssue -- the stable identity of one source DocumentRevision as distributed
to one exact Client company. Survives retransmission (the SAME ClientIssue is reused by every
TransmittalItem that carries the same DocumentRevision to the same Client company, L3H-History
architecture checkpoint sections 4/9/13/14); a different Client company, or a different source
DocumentRevision (a new Internal/Supplier revision), always gets its own, separate row.

Deliberately NOT OneToOne with DocumentRevision (the same revision may legitimately be issued
to more than one Client company) -- identity is instead the (document_revision, client_company)
pair, enforced by the UniqueConstraint below. Created/reused ONLY at successful OUT
finalization (services.client_issue.get_or_create_client_issue, called from
services.transmittal_finalize) -- never when a Document/DocumentRevision is created, never for
a Transmittal draft, never for IN ("ready to send != sent", checkpoint section 18).

client_revision_label stays NULL until explicitly recorded by a future lot (HISTORY-2/3):
HISTORY-1 never fabricates it (no "A"/"0"/"Unspecified" default) and never derives it from
revision_label, external_revision, a Transmittal number, a Document version or a count.
"""

from django.db import models

from models.models import User

from .contact import ContactCompany
from .document_revision import DocumentRevision


class ClientIssue(models.Model):
    document_revision = models.ForeignKey(
        DocumentRevision, related_name="client_issues", on_delete=models.PROTECT,
    )
    # The exact external Client company this Issue was first created for -- same model/FK
    # target as Transmittal.external_company. Part of the Issue's own business identity, not a
    # denormalization: the current architecture does not restrict a project to a single Client
    # company, so the same source revision issued to two different Client companies must
    # resolve to two different ClientIssue rows (checkpoint section 4/14).
    client_company = models.ForeignKey(
        ContactCompany, related_name="client_issues", on_delete=models.PROTECT,
    )
    # Nullable: a future lot assigns this explicitly. Never auto-generated, never derived.
    client_revision_label = models.CharField(max_length=20, null=True, blank=True)
    # Same role/on_delete convention as SupplierSurveillance.created_by -- informational
    # provenance, never load-bearing for the Issue's own business meaning.
    created_by = models.ForeignKey(
        User,
        related_name="created_client_issues",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document_revision", "client_company"],
                name="client_issue_unique_per_revision_and_client",
            ),
        ]

    def __str__(self):
        return f"ClientIssue({self.document_revision_id}, {self.client_company_id})"
