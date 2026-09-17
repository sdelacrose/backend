import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .project import CompanySolutionSpaceItem, Project, ProjectSolutionSpaceItem


class ProjectWorkstation(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_ARCHIVED = "archived"
    STATUS_CHOICES = (
        (STATUS_ACTIVE, "Active"),
        (STATUS_ARCHIVED, "Archived"),
    )

    project = models.ForeignKey(
        Project,
        related_name="project_workstations",
        on_delete=models.CASCADE,
    )
    source_process_assembly = models.ForeignKey(
        CompanySolutionSpaceItem,
        related_name="project_workstations",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255, db_index=True)
    source_requirement = models.OneToOneField(
        "project_management.Requirement",
        related_name="project_workstation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    status = models.CharField(
        max_length=16,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
    )
    template_hash = models.CharField(max_length=64)
    active_revision = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_project_workstations",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="modified_project_workstations",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_project_workstation_idempotency_key",
            ),
        ]
        indexes = [
            models.Index(
                fields=["project", "status", "name"],
                name="project_ws_status_name_idx",
            ),
        ]

    def __str__(self):
        return self.name


class WorkstationRevision(models.Model):
    workstation = models.ForeignKey(
        ProjectWorkstation,
        related_name="revisions",
        on_delete=models.CASCADE,
    )
    number = models.PositiveIntegerField()
    reason = models.TextField(blank=True, default="")
    source_hash = models.CharField(max_length=64)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_workstation_revisions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-number", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["workstation", "number"],
                name="unique_workstation_revision_number",
            ),
            models.UniqueConstraint(
                fields=["workstation", "idempotency_key"],
                condition=Q(idempotency_key__isnull=False),
                name="unique_workstation_revision_idempotency_key",
            ),
        ]

    def __str__(self):
        return f"{self.workstation} revision {self.number}"


class WorkstationSnapshot(models.Model):
    revision = models.OneToOneField(
        WorkstationRevision,
        related_name="snapshot",
        on_delete=models.CASCADE,
    )
    content = models.JSONField(default=dict)
    content_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.content_hash


class ProjectWorkstationNode(models.Model):
    APPROVAL_CHOICES = (("open", "Open"), ("under_discussion", "Under discussion"), ("agreed", "Agreed"))
    approval_status = models.CharField(max_length=24, choices=APPROVAL_CHOICES, default="open")

    ROLE_BASELINE = "baseline"
    ROLE_ADDED = "added"
    ROLE_REPLACEMENT = "replacement"
    ROLE_EXCLUDED = "excluded"
    ROLE_CHOICES = (
        (ROLE_BASELINE, "Baseline"),
        (ROLE_ADDED, "Added"),
        (ROLE_REPLACEMENT, "Replacement"),
        (ROLE_EXCLUDED, "Excluded"),
    )

    workstation = models.ForeignKey(
        ProjectWorkstation,
        related_name="nodes",
        on_delete=models.CASCADE,
    )
    occurrence_id = models.UUIDField(default=uuid.uuid4, editable=False, db_index=True)
    revision = models.ForeignKey(
        WorkstationRevision,
        related_name="nodes",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="children",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    source_company_item = models.ForeignKey(
        CompanySolutionSpaceItem,
        related_name="workstation_occurrences",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    source_project_item = models.ForeignKey(
        ProjectSolutionSpaceItem,
        related_name="workstation_occurrences",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    supersedes = models.ForeignKey(
        "self",
        related_name="replacements",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    name = models.CharField(max_length=255)
    assembly_type = models.CharField(max_length=32, blank=True, null=True)
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_BASELINE)
    is_active = models.BooleanField(default=True)
    quantity = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        default=Decimal("1"),
    )
    unit = models.CharField(max_length=64, blank=True, default="")
    level = models.PositiveSmallIntegerField(default=1)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    required_skills = models.JSONField(default=list, blank=True)
    provided_skills = models.JSONField(default=list, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="created_workstation_nodes",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["level", "sort_order", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quantity__gt=0),
                name="workstation_node_positive_quantity",
            ),
        ]
        indexes = [
            models.Index(
                fields=["workstation", "revision", "parent", "sort_order"],
                name="workstation_node_tree_idx",
            ),
            models.Index(
                fields=["workstation", "occurrence_id", "revision"],
                name="workstation_occurrence_rev_idx",
            ),
        ]

    def clean(self):
        errors = {}
        if self.revision_id and self.workstation_id:
            if self.revision.workstation_id != self.workstation_id:
                errors["revision"] = "Revision must belong to the same workstation."
        if self.parent_id:
            if self.parent.workstation_id != self.workstation_id:
                errors["parent"] = "Parent must belong to the same workstation."
            elif self.parent.revision_id != self.revision_id:
                errors["parent"] = "Parent must belong to the same revision."
            elif self.parent_id == self.id:
                errors["parent"] = "A node cannot be its own parent."
        if self.quantity is not None and self.quantity <= 0:
            errors["quantity"] = "Quantity must be greater than zero."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name
