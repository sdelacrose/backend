from django.conf import settings
from django.db import models

from .document import Document
from .document_search import DocumentSearchIndex
from .project import Project, SolutionDemand


class ProductAttributeExtractionRun(models.Model):
    class SourceSelectionMode(models.TextChoices):
        ALL_CURRENT = "all_current", "All current eligible documents"
        EXPLICIT = "explicit", "Explicitly selected documents"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        EXTRACTING = "extracting", "Extracting"
        REVIEW_READY = "review_ready", "Ready for review"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        STALE = "stale", "Source documents or Customer Demands changed"

    project = models.ForeignKey(
        Project,
        related_name="product_attribute_extraction_runs",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.QUEUED,
        db_index=True,
    )
    schema_version = models.CharField(max_length=16, default="1")
    project_language = models.CharField(max_length=16, default="en")
    source_snapshot_hash = models.CharField(max_length=64)
    source_selection_mode = models.CharField(
        max_length=16,
        choices=SourceSelectionMode.choices,
        default=SourceSelectionMode.ALL_CURRENT,
    )
    matrix_snapshot_hash = models.CharField(max_length=64)
    hint_only = models.BooleanField(default=False)
    raw_result = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_product_attribute_extraction_runs",
        on_delete=models.SET_NULL,
        null=True,
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="modified_product_attribute_extraction_runs",
        on_delete=models.SET_NULL,
        null=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["project", "status", "created_at"],
                name="attribute_run_proj_status_idx",
            ),
        ]


class ProductAttributeExtractionSource(models.Model):
    run = models.ForeignKey(
        ProductAttributeExtractionRun,
        related_name="sources",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        Document,
        related_name="product_attribute_extraction_sources",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    search_index = models.ForeignKey(
        DocumentSearchIndex,
        related_name="product_attribute_extraction_sources",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    document_id_snapshot = models.PositiveBigIntegerField()
    search_index_id_snapshot = models.PositiveBigIntegerField()
    document_name = models.CharField(max_length=500, blank=True, default="")
    content_hash = models.CharField(max_length=64)
    parser_version = models.CharField(max_length=64, blank=True, default="")
    segment_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["document_id_snapshot"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "document_id_snapshot"],
                name="attribute_run_unique_source_doc",
            ),
        ]


class ProductAttributeAppliedColumn(models.Model):
    run = models.ForeignKey(
        ProductAttributeExtractionRun,
        related_name="applied_columns",
        on_delete=models.CASCADE,
    )
    proposal_key = models.CharField(max_length=128)
    column_key = models.CharField(max_length=128)
    applied_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "proposal_key"],
                name="attribute_run_unique_proposal_key",
            ),
            models.UniqueConstraint(
                fields=["run", "column_key"],
                name="attribute_run_unique_column_key",
            ),
        ]


class ProductAttributeAppliedValue(models.Model):
    applied_column = models.ForeignKey(
        ProductAttributeAppliedColumn,
        related_name="applied_values",
        on_delete=models.CASCADE,
    )
    solution_demand = models.ForeignKey(
        SolutionDemand,
        related_name="product_attribute_extraction_values",
        on_delete=models.PROTECT,
    )
    value = models.TextField()
    applied_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["applied_column", "solution_demand"],
                name="attribute_column_unique_demand",
            ),
        ]
