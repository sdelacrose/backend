"""L3H-1: Document Review Durations configuration -- Project + Document Type scoped.

Four independent durations (L3H architecture checkpoint section 5 / L3H-1 section 5): never
collapsed into one generic "review_duration_days". This lot is CONFIGURATION ONLY -- no
operational due date or snapshot field belongs here (those are L3H-2's SupplierSurveillance
fields and L3H-3's TransmittalItem fields).

Document Type remains a plain per-project string in ECPMPConfig.document_types (L3H checkpoint
section 2/11) -- no first-class DocumentType model is introduced here, and none of the known
"renaming a Document Type string can orphan related configuration" debt is solved here either.
"""

from django.db import models

from .project import Project

DEFAULT_REVIEW_DURATION_DAYS = 15

# The four independent duration fields, in the order the configuration UI/API presents them.
DURATION_FIELD_NAMES = (
    "supplier_review_duration_days",
    "client_review_duration_days",
    "internal_client_comment_integration_days",
    "supplier_client_comment_return_days",
)


class DocumentTypeReviewDuration(models.Model):
    """One row per (project, document_type) -- never global, never company-level (L3H
    checkpoint section 6/7, L3H-1 section 6). A null duration field means "not configured, use
    the system default" (section 7/8); a row is never created, and a field is never persisted
    as the literal 15, merely because the configuration page was opened or resolved (L3H-1
    section 10/17/19) -- that is enforced by services.document_type_review_duration, never by
    this model itself accepting writes directly.
    """

    project = models.ForeignKey(
        Project, related_name="document_type_review_durations", on_delete=models.CASCADE,
    )
    # Plain string, validated against the project's own ECPMPConfig.document_types at write
    # time (L3H-1 section 11) -- never a second, independent Document Type list.
    document_type = models.CharField(max_length=255)

    supplier_review_duration_days = models.PositiveIntegerField(null=True, blank=True)
    client_review_duration_days = models.PositiveIntegerField(null=True, blank=True)
    internal_client_comment_integration_days = models.PositiveIntegerField(
        null=True, blank=True,
    )
    supplier_client_comment_return_days = models.PositiveIntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["document_type", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "document_type"],
                name="document_type_review_duration_unique_per_project",
            ),
        ]

    def __str__(self):
        return f"{self.document_type} review durations for project {self.project_id}"
