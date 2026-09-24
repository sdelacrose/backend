from django.db import models

from models.models import User

from .document import Document


class DocumentControl(models.Model):
    """Document Control metadata attached 1:1 to an existing Document.

    A row is only created the first time Document Control metadata is saved for
    a document; existing documents are never backfilled.
    """

    class DocumentType(models.TextChoices):
        PLAN = "Plan", "Plan"
        PROCEDURE = "Procedure", "Procedure"
        SPECIFICATION = "Specification", "Specification"
        TECHNICAL_NOTE = "Technical Note", "Technical Note"
        REPORT = "Report", "Report"
        MINUTES = "Minutes", "Minutes"
        CONTRACT = "Contract", "Contract"
        QUALITY_DOCUMENT = "Quality Document", "Quality Document"
        SUPPLIER_DOCUMENT = "Supplier Document", "Supplier Document"
        CALCULATION = "Calculation", "Calculation"
        DATASHEET = "Datasheet", "Datasheet"
        MANUAL = "Manual", "Manual"
        OTHER = "Other", "Other"

    class Discipline(models.TextChoices):
        PROJECT_MANAGEMENT = "Project Management", "Project Management"
        GENERAL = "General", "General"
        PROCESS = "Process", "Process"
        MECHANICAL = "Mechanical", "Mechanical"
        PIPING = "Piping", "Piping"
        ELECTRICAL = "Electrical", "Electrical"
        INSTRUMENTATION = "Instrumentation", "Instrumentation"
        CIVIL = "Civil", "Civil"
        STRUCTURAL = "Structural", "Structural"
        HVAC = "HVAC", "HVAC"
        QUALITY = "Quality", "Quality"
        HSE = "HSE", "HSE"
        PROCUREMENT = "Procurement", "Procurement"
        CONSTRUCTION = "Construction", "Construction"
        COMMISSIONING = "Commissioning", "Commissioning"
        CONTRACT = "Contract", "Contract"
        FINANCE = "Finance", "Finance"
        OTHER = "Other", "Other"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        ACTIVE = "ACTIVE", "Active"
        ARCHIVED = "ARCHIVED", "Archived"

    DEFAULT_REVISION = "A"

    document = models.OneToOneField(
        Document,
        related_name="control",
        on_delete=models.CASCADE,
    )
    reference = models.CharField(max_length=255, null=True, blank=True)
    document_type = models.CharField(
        max_length=64,
        choices=DocumentType.choices,
        null=True,
        blank=True,
    )
    discipline = models.CharField(
        max_length=64,
        choices=Discipline.choices,
        null=True,
        blank=True,
    )
    revision = models.CharField(max_length=32, default=DEFAULT_REVISION)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    owner = models.ForeignKey(
        User,
        related_name="owned_document_controls",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Supplementary Document Control data: Documents have no approver concept.
    approver = models.ForeignKey(
        User,
        related_name="approver_document_controls",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.reference or self.document_id} ({self.revision})"
