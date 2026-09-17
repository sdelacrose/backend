from django.conf import settings
from django.db import models


class CatalogRecommendationRun(models.Model):
    """Reviewable catalog proposals, independent of workstation creation."""

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        REVIEW_READY = "review_ready", "Ready for review"
        APPLIED = "applied", "Confirmed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        STALE = "stale", "Inputs changed"

    project = models.ForeignKey("project_management.Project", on_delete=models.CASCADE)
    status = models.CharField(
        max_length=32, choices=Status.choices, default=Status.QUEUED
    )
    project_language = models.CharField(max_length=16, default="en")
    input_hash = models.CharField(max_length=64)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    selected_ids = models.JSONField(default=list)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="catalog_recommendation_runs",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="confirmed_catalog_recommendation_runs",
    )
    started_at = models.DateTimeField(null=True)
    completed_at = models.DateTimeField(null=True)
    applied_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["project", "status"], name="catalog_run_project_status"
            )
        ]
