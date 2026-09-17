from django.conf import settings
from django.db import models


class ProcessAssemblyRecommendationRun(models.Model):
    OUTCOME_REVIEW = "review_required"
    OUTCOME_AUTOMATIC = "automatic"
    OUTCOME_MANUAL = "manual"
    OUTCOME_CHOICES = (
        (OUTCOME_REVIEW, "Review required"),
        (OUTCOME_AUTOMATIC, "Automatically selected"),
        (OUTCOME_MANUAL, "Manually selected"),
    )

    project = models.ForeignKey(
        "project_management.Project",
        related_name="process_assembly_recommendation_runs",
        on_delete=models.CASCADE,
    )
    input_hash = models.CharField(max_length=64, db_index=True)
    policy_version = models.CharField(max_length=32, default="1")
    policy = models.JSONField(default=dict)
    ranked_candidates = models.JSONField(default=list)
    selection_outcome = models.CharField(
        max_length=32,
        choices=OUTCOME_CHOICES,
        default=OUTCOME_REVIEW,
    )
    decision_reason = models.CharField(max_length=64, blank=True, default="")
    selected_candidate = models.ForeignKey(
        "project_management.CompanySolutionSpaceItem",
        related_name="recommendation_selections",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_workstation = models.ForeignKey(
        "project_management.ProjectWorkstation",
        related_name="recommendation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    override_history = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_process_assembly_recommendation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "input_hash", "policy_version"],
                name="unique_process_recommendation_input_policy",
            ),
        ]
        indexes = [
            models.Index(
                fields=["project", "selection_outcome", "created_at"],
                name="proc_rec_proj_outcome_idx",
            ),
        ]
