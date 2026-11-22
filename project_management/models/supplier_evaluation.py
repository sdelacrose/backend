"""QUALITY Q1: Supplier Evaluation -- foundation only.

One SupplierEvaluation row evaluates a supplier (an existing ContactCompany), optionally on a real
outgoing PurchaseOrder and Project. It never duplicates the supplier, order or project: it only
points at them. Questionnaires, scoring, the validation workflow and PDF reports are out of scope
for Q1; the status/type/FNC/history columns exist now so later lots do not need a destructive
migration.
"""

from django.db import models

from models.models import CompanyProfile, User

from .contact import ContactCompany
from .project import Project, PurchaseOrder


class SupplierEvaluation(models.Model):
    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PENDING_REVIEW = "PENDING_REVIEW", "Pending review"
        APPROVED = "APPROVED", "Approved"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"

    class EvaluationType(models.TextChoices):
        PARTIAL = "PARTIAL", "Partial"
        FINAL = "FINAL", "Final"

    # Owning tenant (the evaluating company). Every query filters on it.
    company = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="supplier_evaluations"
    )
    supplier = models.ForeignKey(
        ContactCompany, on_delete=models.PROTECT, related_name="supplier_evaluations"
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.PROTECT, null=True, blank=True,
        related_name="supplier_evaluations",
    )
    project = models.ForeignKey(
        Project, on_delete=models.PROTECT, null=True, blank=True,
        related_name="supplier_evaluations",
    )

    reference = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    evaluation_type = models.CharField(
        max_length=10, choices=EvaluationType.choices, default=EvaluationType.PARTIAL
    )

    # FNC (non-conformity) information. Counted and commented only; the FNC process is a later lot.
    fnc_count = models.PositiveIntegerField(default=0)
    fnc_comment = models.TextField(blank=True, default="")

    # History.
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_supplier_evaluations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="closed_supplier_evaluations",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "status"], name="supplier_eval_company_status")]

    def __str__(self):
        return self.reference or f"SupplierEvaluation #{self.pk}"
