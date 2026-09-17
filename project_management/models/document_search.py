from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.utils import timezone

from .document import Document


class DocumentSearchIndex(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        SEARCHABLE = "searchable", "Searchable"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"
        UNSUPPORTED = "unsupported", "Unsupported"

    class ExtractionMethod(models.TextChoices):
        NONE = "none", "None"
        NATIVE = "native", "Native"
        OCR = "ocr", "OCR"
        MIXED = "mixed", "Mixed"

    document = models.OneToOneField(
        Document,
        related_name="search_index",
        on_delete=models.CASCADE,
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.PENDING)
    content_hash = models.CharField(max_length=64, blank=True, default="")
    mime_type = models.CharField(max_length=255, blank=True, default="")
    language = models.CharField(max_length=64, blank=True, default="")
    extraction_method = models.CharField(
        max_length=32,
        choices=ExtractionMethod.choices,
        default=ExtractionMethod.NONE,
    )
    page_count = models.PositiveIntegerField(default=0)
    parser_version = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    failure_category = models.CharField(max_length=64, blank=True, default="")
    attempt_count = models.PositiveIntegerField(default=0)
    indexed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "updated_at"], name="doc_search_status_idx"),
            models.Index(fields=["content_hash"], name="doc_search_hash_idx"),
        ]


class DocumentContentSegment(models.Model):
    class SegmentType(models.TextChoices):
        DOCUMENT = "document", "Document"
        PAGE = "page", "Page"
        SHEET = "sheet", "Sheet"
        SLIDE = "slide", "Slide"
        EMAIL_BODY = "email_body", "Email body"
        ATTACHMENT = "attachment", "Attachment"

    document = models.ForeignKey(
        Document,
        related_name="content_segments",
        on_delete=models.CASCADE,
    )
    index = models.ForeignKey(
        DocumentSearchIndex,
        related_name="segments",
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
        ordering = ["document_id", "sequence", "id"]
        indexes = [
            GinIndex(fields=["search_vector"], name="doc_segment_vector_gin"),
            models.Index(fields=["document", "segment_type"], name="doc_segment_doc_type_idx"),
        ]


class DocumentIndexJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        RETRYING = "retrying", "Retrying"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        COMPLETED = "completed", "Completed"

    document = models.ForeignKey(
        Document,
        related_name="index_jobs",
        on_delete=models.CASCADE,
    )
    status = models.CharField(max_length=32, choices=Status.choices, default=Status.QUEUED)
    content_hash = models.CharField(max_length=64, blank=True, default="")
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
        ordering = ["id"]
        indexes = [
            models.Index(fields=["status", "available_at"], name="doc_job_status_ready_idx"),
            models.Index(fields=["document", "content_hash"], name="doc_job_doc_hash_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "content_hash"],
                condition=models.Q(status__in=["queued", "running", "retrying"]),
                name="doc_job_unique_active_hash",
            ),
        ]


class DocumentIndexAttempt(models.Model):
    job = models.ForeignKey(
        DocumentIndexJob,
        related_name="attempts",
        on_delete=models.CASCADE,
    )
    document = models.ForeignKey(
        Document,
        related_name="index_attempts",
        on_delete=models.CASCADE,
    )
    status = models.CharField(max_length=32)
    parser = models.CharField(max_length=64, blank=True, default="")
    failure_category = models.CharField(max_length=64, blank=True, default="")
    error = models.TextField(blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    source_size_bytes = models.BigIntegerField(default=0)
    extracted_size_bytes = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["document", "created_at"], name="doc_attempt_doc_created_idx"),
        ]
