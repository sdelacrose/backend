from uuid import uuid4

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from models.models import CompanyProfile, User
from project_management.models import ActionItem, Document, Folder, Project, Requirement


def project_template_upload_path(instance, filename):
    return (
        f"project-content/templates/{instance.company_id}/"
        f"{instance.kind}/{instance.locale}/{instance.id}/{filename}"
    )


class CompanyProjectDocumentTemplate(models.Model):
    class Kind(models.TextChoices):
        OFFER = "offer", "Offer"
        REQUIREMENTS_SPECIFICATION = (
            "requirements_specification",
            "Requirements specification",
        )

    class Locale(models.TextChoices):
        ENGLISH = "en", "English"
        GERMAN = "de", "German"
        FRENCH = "fr", "French"

    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        VALID = "valid", "Valid"
        INVALID = "invalid", "Invalid"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    company = models.ForeignKey(
        CompanyProfile,
        related_name="project_document_templates",
        on_delete=models.CASCADE,
    )
    kind = models.CharField(max_length=40, choices=Kind.choices)
    locale = models.CharField(max_length=8, choices=Locale.choices, default=Locale.ENGLISH)
    name = models.CharField(max_length=255)
    version = models.PositiveIntegerField(default=1)
    template_file = models.FileField(
        upload_to=project_template_upload_path,
        max_length=500,
    )
    original_file_name = models.CharField(max_length=500)
    file_hash = models.CharField(max_length=64, blank=True, default="")
    is_active = models.BooleanField(default=False)
    validation_status = models.CharField(
        max_length=16,
        choices=ValidationStatus.choices,
        default=ValidationStatus.PENDING,
    )
    validation_report = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_project_document_templates",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["company_id", "kind", "locale", "-version"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "kind", "locale", "version"],
                name="project_template_unique_version",
            ),
            models.UniqueConstraint(
                fields=["company", "kind", "locale"],
                condition=Q(is_active=True),
                name="project_template_unique_active",
            ),
        ]
        indexes = [
            models.Index(
                fields=["company", "kind", "locale", "is_active"],
                name="project_template_lookup_idx",
            )
        ]


class GeneratedProjectDocument(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        GENERATING = "generating", "Generating"
        REVIEW_READY = "review_ready", "Review ready"
        CURRENT = "current", "Current"
        OUT_OF_DATE = "out_of_date", "Out of date"
        FAILED = "failed", "Failed"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        related_name="generated_project_documents",
        on_delete=models.CASCADE,
    )
    kind = models.CharField(
        max_length=40,
        choices=CompanyProjectDocumentTemplate.Kind.choices,
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.DRAFT)
    source_hash = models.CharField(max_length=64, blank=True, default="")
    current_revision = models.ForeignKey(
        "GeneratedProjectDocumentRevision",
        related_name="current_for_documents",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "kind"],
                name="generated_project_document_unique_kind",
            )
        ]
        indexes = [
            models.Index(
                fields=["project", "kind", "status"],
                name="gen_doc_proj_status_idx",
            )
        ]


class GeneratedProjectDocumentRevision(models.Model):
    class ApprovalStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        SUPERSEDED = "superseded", "Superseded"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    generated_document = models.ForeignKey(
        GeneratedProjectDocument,
        related_name="revisions",
        on_delete=models.CASCADE,
    )
    revision = models.PositiveIntegerField()
    source_hash = models.CharField(max_length=64)
    template = models.ForeignKey(
        CompanyProjectDocumentTemplate,
        related_name="generated_revisions",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    template_version = models.CharField(max_length=64)
    structured_content = models.JSONField(default=dict)
    validation_report = models.JSONField(default=dict, blank=True)
    docx_document = models.ForeignKey(
        Document,
        related_name="generated_docx_revisions",
        on_delete=models.PROTECT,
    )
    pdf_document = models.ForeignKey(
        Document,
        related_name="generated_pdf_revisions",
        on_delete=models.PROTECT,
    )
    docx_hash = models.CharField(max_length=64)
    pdf_hash = models.CharField(max_length=64)
    approval_status = models.CharField(
        max_length=16,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.DRAFT,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_project_document_revisions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    approved_by = models.ForeignKey(
        User,
        related_name="approved_project_document_revisions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-revision"]
        constraints = [
            models.UniqueConstraint(
                fields=["generated_document", "revision"],
                name="generated_revision_unique_number",
            ),
            models.UniqueConstraint(
                fields=["generated_document", "source_hash", "template_version"],
                name="generated_revision_unique_source",
            ),
        ]


class ProjectDocumentGenerationRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        REVIEW_READY = "review_ready", "Review ready"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        STALE = "stale", "Stale"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        related_name="project_document_generation_runs",
        on_delete=models.CASCADE,
    )
    generated_document = models.ForeignKey(
        GeneratedProjectDocument,
        related_name="generation_runs",
        on_delete=models.CASCADE,
    )
    kind = models.CharField(
        max_length=40,
        choices=CompanyProjectDocumentTemplate.Kind.choices,
    )
    locale = models.CharField(
        max_length=8,
        choices=CompanyProjectDocumentTemplate.Locale.choices,
        default=CompanyProjectDocumentTemplate.Locale.ENGLISH,
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    source_snapshot = models.JSONField(default=dict)
    source_hash = models.CharField(max_length=64)
    request_options = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    draft_content = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    hint_only = models.BooleanField(default=False)
    applied_revision = models.ForeignKey(
        GeneratedProjectDocumentRevision,
        related_name="applied_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_project_document_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_project_document_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["project", "kind", "status", "created_at"],
                name="project_doc_run_lookup_idx",
            ),
            models.Index(fields=["source_hash"], name="project_doc_run_hash_idx"),
        ]


class AcceptanceChecklist(models.Model):
    class Phase(models.TextChoices):
        FACTORY = "factory_acceptance", "Factory Acceptance Test (FAT)"
        SITE = "site_acceptance", "Customer Acceptance Test (CAT)"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        related_name="acceptance_checklists",
        on_delete=models.CASCADE,
    )
    phase = models.CharField(max_length=32, choices=Phase.choices)
    display_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    source_revision = models.ForeignKey(
        GeneratedProjectDocumentRevision,
        related_name="acceptance_checklists",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_acceptance_checklists",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "phase"],
                name="acceptance_checklist_unique_phase",
            )
        ]


class AcceptanceGenerationRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        GENERATING = "generating", "Generating"
        REVIEW_READY = "review_ready", "Review ready"
        APPLYING = "applying", "Applying"
        APPLIED = "applied", "Applied"
        FAILED = "failed", "Failed"
        STALE = "stale", "Stale"
        CANCELLED = "cancelled", "Cancelled"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        related_name="acceptance_generation_runs",
        on_delete=models.CASCADE,
    )
    fat_phase = models.ForeignKey(
        "project_management.Milestone",
        related_name="fat_acceptance_generation_runs",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    cat_phase = models.ForeignKey(
        "project_management.Milestone",
        related_name="cat_acceptance_generation_runs",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.QUEUED)
    schema_version = models.CharField(max_length=16, default="2")
    locale = models.CharField(
        max_length=8,
        choices=CompanyProjectDocumentTemplate.Locale.choices,
        default=CompanyProjectDocumentTemplate.Locale.ENGLISH,
    )
    source_snapshot = models.JSONField(default=dict)
    source_hash = models.CharField(max_length=64)
    request_options = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    draft_content = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_code = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    hint_only = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        User,
        related_name="created_acceptance_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_acceptance_generation_runs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["project", "status", "created_at"],
                name="acceptance_run_lookup_idx",
            )
        ]


class AcceptanceActionItemProvenance(models.Model):
    class PhaseRole(models.TextChoices):
        FAT = "fat", "FAT"
        CAT = "cat", "CAT"

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    generation_run = models.ForeignKey(
        AcceptanceGenerationRun,
        related_name="action_item_provenance",
        on_delete=models.PROTECT,
    )
    proposal_key = models.CharField(max_length=128)
    phase_role = models.CharField(max_length=8, choices=PhaseRole.choices)
    pairing_key = models.UUIDField(default=uuid4, db_index=True)
    action_item = models.ForeignKey(
        ActionItem,
        related_name="acceptance_provenance_history",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    applied_fields = models.JSONField(default=dict)
    source_evidence = models.JSONField(default=list)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_acceptance_action_item_provenance",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["generation_run", "proposal_key", "phase_role"],
                name="acceptance_provenance_unique_role",
            )
        ]
        indexes = [
            models.Index(
                fields=["generation_run", "phase_role"],
                name="acceptance_provenance_run_idx",
            )
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Applied acceptance provenance is immutable.")
        return super().save(*args, **kwargs)


class AcceptanceCriterion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    checklist = models.ForeignKey(
        AcceptanceChecklist,
        related_name="criteria",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="children",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    stable_key = models.CharField(max_length=128)
    pair_key = models.UUIDField(default=uuid4, db_index=True)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    name = models.CharField(max_length=255)
    acceptance_criteria = models.TextField()
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    forecast_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=100, default="Draft")
    priority = models.CharField(max_length=255, null=True, blank=True)
    labels = ArrayField(models.CharField(max_length=255), default=list, blank=True)
    assignees = models.ManyToManyField(
        User,
        related_name="assigned_acceptance_criteria",
        blank=True,
    )
    requirements = models.ManyToManyField(
        Requirement,
        related_name="acceptance_criteria",
        blank=True,
    )
    documents = models.ManyToManyField(
        Document,
        related_name="acceptance_criteria",
        blank=True,
    )
    linked_folders = models.ManyToManyField(
        Folder,
        related_name="acceptance_criteria",
        blank=True,
    )
    source_run = models.ForeignKey(
        AcceptanceGenerationRun,
        related_name="applied_criteria",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    manual_fields = models.JSONField(default=list, blank=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_acceptance_criteria",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_acceptance_criteria",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["checklist", "stable_key"],
                name="acceptance_criterion_unique_key",
            )
        ]
        indexes = [
            models.Index(
                fields=["checklist", "parent", "sort_order"],
                name="acceptance_criterion_tree_idx",
            ),
            models.Index(
                fields=["checklist", "status"],
                name="accept_criterion_status_idx",
            ),
        ]


class AcceptanceCriterionEvidence(models.Model):
    criterion = models.ForeignKey(
        AcceptanceCriterion,
        related_name="evidence",
        on_delete=models.CASCADE,
    )
    requirement = models.ForeignKey(
        Requirement,
        related_name="acceptance_evidence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    document = models.ForeignKey(
        Document,
        related_name="acceptance_evidence",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    generation_run = models.ForeignKey(
        AcceptanceGenerationRun,
        related_name="evidence_records",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    segment_id = models.PositiveIntegerField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True, default="")
    quote = models.TextField(blank=True, default="")
    excerpt_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(Q(requirement__isnull=False) | Q(document__isnull=False)),
                name="acceptance_evidence_has_source",
            )
        ]


class AcceptanceCriterionComment(models.Model):
    criterion = models.ForeignKey(
        AcceptanceCriterion,
        related_name="comments",
        on_delete=models.CASCADE,
    )
    sender = models.ForeignKey(
        User,
        related_name="acceptance_criterion_comments",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    parent = models.ForeignKey(
        "self",
        related_name="replies",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    content = models.TextField()
    attachments = models.ManyToManyField(
        Document,
        related_name="acceptance_comment_attachments",
        blank=True,
    )
    mentions = models.ManyToManyField(
        User,
        related_name="mentioned_acceptance_comments",
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at", "id"]


class AcceptanceCriterionCommentReaction(models.Model):
    comment = models.ForeignKey(
        AcceptanceCriterionComment,
        related_name="reactions",
        on_delete=models.CASCADE,
    )
    user = models.ForeignKey(
        User,
        related_name="acceptance_comment_reactions",
        on_delete=models.CASCADE,
    )
    emoji = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["comment", "user", "emoji"],
                name="acceptance_comment_unique_reaction",
            )
        ]


class ProjectContentAuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    project = models.ForeignKey(
        Project,
        related_name="project_content_audit_events",
        on_delete=models.CASCADE,
    )
    event_type = models.CharField(max_length=64)
    object_type = models.CharField(max_length=64)
    object_id = models.CharField(max_length=64)
    actor = models.ForeignKey(
        User,
        related_name="project_content_audit_events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["project", "object_type", "object_id", "created_at"],
                name="project_content_audit_obj_idx",
            ),
            models.Index(
                fields=["project", "event_type", "created_at"],
                name="project_content_audit_evt_idx",
            ),
        ]
