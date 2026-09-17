from django.conf import settings
from django.db import models
from django.db.models import Q


class BomGenerationRun(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_PREVIEW = "preview"
    STATUS_APPLIED = "applied"
    STATUS_STALE = "stale"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_PREVIEW, "Preview"),
        (STATUS_APPLIED, "Applied"),
        (STATUS_STALE, "Stale"),
        (STATUS_FAILED, "Failed"),
    )

    project = models.ForeignKey(
        "project_management.Project",
        related_name="bom_generation_runs",
        on_delete=models.CASCADE,
    )
    workstation_snapshots = models.ManyToManyField(
        "project_management.WorkstationSnapshot",
        related_name="bom_generation_runs",
        blank=True,
    )
    source_hash = models.CharField(max_length=64, db_index=True)
    target_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_PREVIEW,
        db_index=True,
    )
    summary = models.JSONField(default=dict, blank=True)
    request_payload = models.JSONField(default=dict, blank=True)
    metrics = models.JSONField(default=dict, blank=True)
    node_count = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    error_details = models.JSONField(default=dict, blank=True)
    internal_run = models.BooleanField(default=False, editable=False)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_bom_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="applied_bom_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    claim_expires_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_project_bom_run_idempotency_key",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        internal_run=True,
                        idempotency_key__isnull=False,
                        idempotency_key__startswith="__bom_worker__:",
                    )
                    | (
                        Q(internal_run=False)
                        & ~Q(idempotency_key__startswith="__bom_worker__:")
                    )
                ),
                name="bom_internal_key_namespace",
            ),
        ]


class BomPlanOperation(models.Model):
    OPERATION_ADD = "add"
    OPERATION_UPDATE = "update"
    OPERATION_UNCHANGED = "unchanged"
    OPERATION_DETACH = "detach"
    OPERATION_CONFLICT = "conflict"
    OPERATION_CHOICES = (
        (OPERATION_ADD, "Add"),
        (OPERATION_UPDATE, "Update"),
        (OPERATION_UNCHANGED, "Unchanged"),
        (OPERATION_DETACH, "Detach"),
        (OPERATION_CONFLICT, "Conflict"),
    )

    run = models.ForeignKey(
        BomGenerationRun,
        related_name="operations",
        on_delete=models.CASCADE,
    )
    operation = models.CharField(max_length=16, choices=OPERATION_CHOICES, db_index=True)
    source_workstation = models.ForeignKey(
        "project_management.ProjectWorkstation",
        related_name="bom_plan_operations",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    source_snapshot = models.ForeignKey(
        "project_management.WorkstationSnapshot",
        related_name="bom_plan_operations",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    source_occurrence_id = models.UUIDField(null=True, blank=True, db_index=True)
    target_bom_part = models.ForeignKey(
        "project_management.BillOfMaterialPart",
        related_name="bom_plan_operations",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    before_values = models.JSONField(default=dict, blank=True)
    after_values = models.JSONField(default=dict, blank=True)
    resolution_state = models.CharField(max_length=32, blank=True, default="")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["run", "operation", "sort_order"], name="bom_plan_run_op_order_idx"),
        ]
