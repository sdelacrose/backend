from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone

from models.models import CompanyProfile


class SearchRecord(models.Model):
    class SourceType(models.TextChoices):
        DOCUMENT = "document", "Document"
        OBJECT_ASSESSMENT = "object_assessment", "Object Assessment"
        SOLUTION_DEMAND = "solution_demand", "Solution Demand"
        REQUIREMENT = "requirement", "Requirement"
        AVAILABLE_PRODUCT_PART = "available_product_part", "Available Product Part"
        MACHINE = "machine", "Machine"
        EQUIPMENT = "equipment", "Equipment"
        BOM_PART = "bom_part", "Bill of Material Part"
        COMPANY_SOLUTION_SPACE = "company_solution_space", "Company Solution Space"
        PROJECT_SOLUTION_SPACE = "project_solution_space", "Project Solution Space"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        INVOICE = "invoice", "Invoice"
        ACTION_ITEM_COMMENT = "action_item_comment", "Action Item Comment"
        MATRIX_COMMENT = "matrix_comment", "Matrix Comment"

    class IndexStatus(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SEARCHABLE = "searchable", "Searchable"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        UNSUPPORTED = "unsupported", "Unsupported"

    source_type = models.CharField(max_length=48, choices=SourceType.choices)
    source_id = models.PositiveBigIntegerField()
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="search_records",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        "project_management.Project",
        related_name="search_records",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="freelancer_search_records",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=500, blank=True, default="")
    subtitle = models.CharField(max_length=500, blank=True, default="")
    searchable_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    index_status = models.CharField(
        max_length=32,
        choices=IndexStatus.choices,
        default=IndexStatus.SEARCHABLE,
    )
    content_hash = models.CharField(max_length=64, blank=True, default="")
    mime_type = models.CharField(max_length=255, blank=True, default="")
    language = models.CharField(max_length=64, blank=True, default="")
    extraction_method = models.CharField(max_length=32, blank=True, default="")
    page_count = models.PositiveIntegerField(default=0)
    parser_version = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    failure_category = models.CharField(max_length=64, blank=True, default="")
    source_updated_at = models.DateTimeField(null=True, blank=True)
    indexed_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id"],
                name="search_record_unique_source",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(company_profile__isnull=False)
                    | models.Q(project__isnull=False)
                    | models.Q(freelancer__isnull=False)
                ),
                name="search_record_has_scope",
            ),
        ]
        indexes = [
            GinIndex(fields=["search_vector"], name="search_record_vector_gin"),
            GinIndex(
                fields=["title"],
                name="search_record_title_trgm",
                opclasses=["gin_trgm_ops"],
            ),
            GinIndex(
                fields=["searchable_text"],
                name="search_record_text_trgm",
                opclasses=["gin_trgm_ops"],
            ),
            models.Index(
                fields=["source_type", "source_id"],
                name="search_record_source_idx",
            ),
            models.Index(
                fields=["project", "source_type"],
                name="search_record_project_idx",
            ),
            models.Index(
                fields=["company_profile", "source_type"],
                name="search_record_company_idx",
            ),
            models.Index(
                fields=["freelancer", "source_type"],
                name="search_record_freelancer_idx",
            ),
            models.Index(
                fields=["index_status", "updated_at"],
                name="search_record_status_idx",
            ),
        ]


class SearchContentSegment(models.Model):
    class SegmentType(models.TextChoices):
        DOCUMENT = "document", "Document"
        PAGE = "page", "Page"
        SHEET = "sheet", "Sheet"
        SLIDE = "slide", "Slide"
        EMAIL_BODY = "email_body", "Email body"
        ATTACHMENT = "attachment", "Attachment"
        NOTE = "note", "Note"

    record = models.ForeignKey(
        SearchRecord,
        related_name="content_segments",
        on_delete=models.CASCADE,
    )
    segment_type = models.CharField(max_length=32, choices=SegmentType.choices)
    sequence = models.PositiveIntegerField(default=1)
    location_label = models.CharField(max_length=255, blank=True, default="")
    attachment_name = models.CharField(max_length=500, blank=True, default="")
    text = models.TextField()
    search_vector = SearchVectorField(null=True, blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["record_id", "sequence", "id"]
        indexes = [
            GinIndex(fields=["search_vector"], name="search_segment_vector_gin"),
            GinIndex(
                fields=["text"],
                name="search_segment_text_trgm",
                opclasses=["gin_trgm_ops"],
            ),
            models.Index(
                fields=["record", "segment_type"],
                name="search_segment_record_idx",
            ),
        ]


class SearchFieldValue(models.Model):
    record = models.ForeignKey(
        SearchRecord,
        related_name="field_values",
        on_delete=models.CASCADE,
    )
    field_ref = models.CharField(max_length=500)
    field_key = models.CharField(max_length=255)
    field_type = models.CharField(max_length=64, blank=True, default="text")
    semantic_id = models.CharField(max_length=500, blank=True, default="")
    label_en = models.CharField(max_length=500, blank=True, default="")
    label_de = models.CharField(max_length=500, blank=True, default="")
    label_fr = models.CharField(max_length=500, blank=True, default="")
    value_text = models.TextField(blank=True, default="")
    value_normalized = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True, blank=True)

    class Meta:
        indexes = [
            GinIndex(fields=["search_vector"], name="search_field_vector_gin"),
            GinIndex(
                fields=["value_normalized"],
                name="search_field_value_trgm",
                opclasses=["gin_trgm_ops"],
            ),
            models.Index(fields=["field_ref"], name="search_field_ref_idx"),
            models.Index(fields=["field_key"], name="search_field_key_idx"),
            models.Index(fields=["semantic_id"], name="search_field_semantic_idx"),
        ]


class SearchRelation(models.Model):
    record = models.ForeignKey(
        SearchRecord,
        related_name="relations",
        on_delete=models.CASCADE,
    )
    relation_type = models.CharField(max_length=64)
    related_type = models.CharField(max_length=64)
    related_id = models.PositiveBigIntegerField()
    label = models.CharField(max_length=500, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["record", "relation_type", "related_type", "related_id"],
                name="search_relation_unique_target",
            )
        ]
        indexes = [
            models.Index(
                fields=["related_type", "related_id"],
                name="search_relation_target_idx",
            ),
            models.Index(
                fields=["record", "relation_type"],
                name="search_relation_record_idx",
            ),
        ]


class SearchIndexJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        RETRYING = "retrying", "Retrying"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    source_type = models.CharField(max_length=48, choices=SearchRecord.SourceType.choices)
    source_id = models.PositiveBigIntegerField()
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.QUEUED)
    attempt_count = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=3)
    available_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True, default="")
    failure_category = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "available_at"],
                name="search_job_status_ready_idx",
            ),
            models.Index(
                fields=["source_type", "source_id"],
                name="search_job_source_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source_type", "source_id"],
                condition=models.Q(status__in=["queued", "running", "retrying"]),
                name="search_job_unique_active_source",
            )
        ]


class SearchIndexAttempt(models.Model):
    job = models.ForeignKey(
        SearchIndexJob,
        related_name="attempts",
        on_delete=models.CASCADE,
    )
    source_type = models.CharField(max_length=48, choices=SearchRecord.SourceType.choices)
    source_id = models.PositiveBigIntegerField()
    status = models.CharField(max_length=32)
    duration_ms = models.PositiveIntegerField(default=0)
    indexed_text_size = models.PositiveIntegerField(default=0)
    parser = models.CharField(max_length=64, blank=True, default="")
    failure_category = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(
                fields=["source_type", "source_id", "created_at"],
                name="search_attempt_source_idx",
            ),
        ]


class SemanticPropertyDefinition(models.Model):
    class VerificationStatus(models.TextChoices):
        VERIFIED = "verified", "Verified"
        UNKNOWN = "unknown", "Unknown"
        COMPANY_DEFINED = "company_defined", "Company defined"

    semantic_id = models.CharField(max_length=500, unique=True)
    idta_template = models.CharField(max_length=255, blank=True, default="")
    template_version = models.CharField(max_length=64, blank=True, default="")
    preferred_name_en = models.CharField(max_length=500, blank=True, default="")
    preferred_name_de = models.CharField(max_length=500, blank=True, default="")
    preferred_name_fr = models.CharField(max_length=500, blank=True, default="")
    data_type = models.CharField(max_length=64, blank=True, default="")
    verification_status = models.CharField(
        max_length=32,
        choices=VerificationStatus.choices,
        default=VerificationStatus.VERIFIED,
    )
    source_version = models.CharField(max_length=128, blank=True, default="")
    source_checksum = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["semantic_id"], name="semantic_definition_id_idx"),
            models.Index(
                fields=["idta_template", "template_version"],
                name="semantic_definition_tpl_idx",
            ),
        ]
