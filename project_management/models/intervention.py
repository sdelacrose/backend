"""DEMO-01: intervention requests (demandes d'intervention) attached to a Project, and the
reviewed CSV import that creates them together with their generated Action Items.

An InterventionRequest is NOT a second task engine: its work is carried by the existing
ActionItem (see ActionItem.intervention_request / intervention_action_key). Three dates are kept
apart on purpose:
  * received_date        -- business date the request was first received (never overwritten);
  * created_at           -- when LINKS stored it (import time);
  * source_modified_at   -- last modification date announced by the source system, if any.
"""
from uuid import uuid4

from django.db import models
from django.db.models import Q

from models.models import CompanyProfile, User

from .project import Project


class InterventionReferenceCounter(models.Model):
    """Locked per-project sequence for LINKS intervention references (own table, like
    CorrespondenceReferenceCounter)."""

    project = models.OneToOneField(
        Project, related_name="intervention_reference_counter", on_delete=models.CASCADE
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class InterventionImportBatch(models.Model):
    class Status(models.TextChoices):
        PREVIEWED = "previewed", "Previewed"
        CONFIRMED = "confirmed", "Confirmed"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project, related_name="intervention_import_batches", on_delete=models.PROTECT
    )
    created_by = models.ForeignKey(
        User, related_name="intervention_import_batches", on_delete=models.SET_NULL, null=True, blank=True
    )
    source = models.CharField(max_length=50, default="csv")
    file_name = models.CharField(max_length=255, blank=True, default="")
    file_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PREVIEWED)
    summary = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class InterventionRequest(models.Model):
    project = models.ForeignKey(
        Project, related_name="intervention_requests", on_delete=models.PROTECT
    )
    # Copied from the project at creation; not editable (the project is the single owner of the scope).
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="intervention_requests",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
    )
    reference = models.CharField(max_length=60, editable=False)
    title = models.CharField(max_length=500)
    received_date = models.DateField()
    expected_end_date = models.DateField()
    budget = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, blank=True, default="")
    description = models.TextField(blank=True, default="")
    source = models.CharField(max_length=50, blank=True, default="")
    external_reference = models.CharField(max_length=255, blank=True, default="")
    source_modified_at = models.DateTimeField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    import_batch = models.ForeignKey(
        InterventionImportBatch,
        related_name="requests",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        User, related_name="created_intervention_requests", on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "reference"], name="unique_intervention_reference_per_project"
            ),
            models.UniqueConstraint(
                fields=["project", "source", "external_reference"],
                condition=~Q(external_reference=""),
                name="unique_intervention_external_ref_per_project",
            ),
        ]
        indexes = [models.Index(fields=["project", "received_date"], name="interv_project_received_idx")]

    def __str__(self):
        return f"{self.reference} {self.title}"


class InterventionImportRow(models.Model):
    class Outcome(models.TextChoices):
        NEW = "new", "New"
        ERROR = "error", "Error"
        DUPLICATE_IN_FILE = "duplicate_in_file", "Duplicate in file"
        ALREADY_IMPORTED = "already_imported", "Already imported"
        SOURCE_CHANGED = "source_changed", "Source data changed"
        CREATED = "created", "Created"

    batch = models.ForeignKey(InterventionImportBatch, related_name="rows", on_delete=models.CASCADE)
    line_number = models.PositiveIntegerField()
    external_reference = models.CharField(max_length=255, blank=True, default="")
    data = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    outcome = models.CharField(max_length=24, choices=Outcome.choices)
    errors = models.JSONField(default=list, blank=True)
    existing_request = models.ForeignKey(
        InterventionRequest, related_name="+", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["line_number"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "line_number"], name="unique_intervention_row_line")
        ]
