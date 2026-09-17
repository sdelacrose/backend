from django.conf import settings
from django.db import models

from .project import Project, Requirement


class ProcessAssemblyExtractionRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        EXTRACTING = "extracting", "Extracting"
        REVIEW_READY = "review_ready", "Ready for review"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        STALE = "stale", "Demand, Solution Space, or Requirements changed"

    project = models.ForeignKey(
        Project,
        related_name="process_assembly_extraction_runs",
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
    input_snapshot_hash = models.CharField(max_length=64)
    input_snapshot = models.JSONField(default=dict, blank=True)
    hint_only = models.BooleanField(default=False)
    raw_result = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_process_assembly_extraction_runs",
        on_delete=models.SET_NULL,
        null=True,
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="modified_process_assembly_extraction_runs",
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
                name="process_assembly_run_idx",
            ),
        ]


class ProcessAssemblyAppliedRow(models.Model):
    run = models.ForeignKey(
        ProcessAssemblyExtractionRun,
        related_name="applied_rows",
        on_delete=models.CASCADE,
    )
    proposal_key = models.CharField(max_length=128)
    applied_payload = models.JSONField(default=dict, blank=True)
    requirement = models.ForeignKey(
        Requirement,
        related_name="process_assembly_extraction_rows",
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "proposal_key"],
                name="process_run_unique_proposal",
            ),
            models.UniqueConstraint(
                fields=["run", "requirement"],
                name="process_run_unique_requirement",
            ),
        ]


class ProcessAttributeExtractionRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        EXTRACTING = "extracting", "Extracting"
        REVIEW_READY = "review_ready", "Ready for review"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        STALE = "stale", "Requirements, Demands, or Solution Space changed"

    project = models.ForeignKey(
        Project,
        related_name="process_attribute_extraction_runs",
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
    input_snapshot_hash = models.CharField(max_length=64)
    input_snapshot = models.JSONField(default=dict, blank=True)
    hint_only = models.BooleanField(default=False)
    raw_result = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_process_attribute_extraction_runs",
        on_delete=models.SET_NULL,
        null=True,
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="modified_process_attribute_extraction_runs",
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
                name="process_attribute_run_idx",
            ),
        ]


class ProcessAttributeAppliedColumn(models.Model):
    run = models.ForeignKey(
        ProcessAttributeExtractionRun,
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
                name="process_attr_unique_proposal",
            ),
            models.UniqueConstraint(
                fields=["run", "column_key"],
                name="process_attr_unique_column",
            ),
        ]


class ProcessAttributeAppliedValue(models.Model):
    applied_column = models.ForeignKey(
        ProcessAttributeAppliedColumn,
        related_name="applied_values",
        on_delete=models.CASCADE,
    )
    requirement = models.ForeignKey(
        Requirement,
        related_name="process_attribute_extraction_values",
        on_delete=models.PROTECT,
    )
    value = models.TextField()
    applied_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["applied_column", "requirement"],
                name="process_attr_unique_value",
            ),
        ]
