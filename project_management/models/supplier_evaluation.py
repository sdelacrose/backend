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
from .supplier_evaluation_template import EvaluationTemplate


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

    # Q2 prepares the snapshot structure; Q3 fills it. The FK is SET_NULL: deleting a template never
    # touches an evaluation, which only ever reads its own frozen copy.
    template = models.ForeignKey(
        EvaluationTemplate, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="evaluations",
    )
    template_version = models.PositiveIntegerField(null=True, blank=True)
    template_snapshot = models.JSONField(null=True, blank=True)

    reference = models.CharField(max_length=64, blank=True, default="")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    evaluation_type = models.CharField(
        max_length=10, choices=EvaluationType.choices, default=EvaluationType.PARTIAL
    )

    # Q3: owner of the evaluation (the user who started it) and the explicitly typed evaluated period.
    owner = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="owned_supplier_evaluations",
    )
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)

    # FNC (non-conformity). Q3: "follows an FNC" + its mandatory reference when yes. The counters below are
    # kept for the later FNC lot.
    follows_fnc = models.BooleanField(default=False)
    fnc_reference = models.CharField(max_length=128, blank=True, default="")
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
        constraints = [
            # References are generated, never typed; unique per company. Several evaluations of the same order
            # are allowed on purpose (partial evaluations, re-evaluations): no constraint on purchase_order.
            models.UniqueConstraint(
                fields=["company", "reference"],
                condition=~models.Q(reference=""),
                name="supplier_eval_unique_reference_per_company",
            ),
        ]

    def __str__(self):
        return self.reference or f"SupplierEvaluation #{self.pk}"


class EvaluationReferenceCounter(models.Model):
    """One row per company: the sequence of EVAL-YYYYMM-NNNNN, locked with select_for_update (same approach as
    POReferenceCounter). The sequence never resets when the month changes."""

    company = models.OneToOneField(
        CompanyProfile, on_delete=models.CASCADE, related_name="evaluation_reference_counter"
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class EvaluationResponse(models.Model):
    """The answer to ONE criterion of the evaluation own snapshot. `criterion_id` is the id stored inside
    `SupplierEvaluation.template_snapshot`, deliberately not a foreign key: the live template rows may be
    edited or deleted without touching an evaluation."""

    evaluation = models.ForeignKey(
        SupplierEvaluation, on_delete=models.CASCADE, related_name="responses"
    )
    criterion_id = models.PositiveIntegerField()
    score = models.PositiveSmallIntegerField(null=True, blank=True)
    not_applicable = models.BooleanField(default=False)
    comment = models.TextField(blank=True, default="")
    answered_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["criterion_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["evaluation", "criterion_id"], name="evaluation_response_unique_criterion"
            ),
            models.CheckConstraint(
                check=models.Q(score__isnull=True) | (models.Q(score__gte=1) & models.Q(score__lte=5)),
                name="evaluation_response_score_1_5",
            ),
            models.CheckConstraint(
                check=~(models.Q(score__isnull=False) & models.Q(not_applicable=True)),
                name="evaluation_response_score_xor_na",
            ),
        ]
