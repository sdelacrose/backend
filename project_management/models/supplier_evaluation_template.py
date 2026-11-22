"""QUALITY Q2: Supplier Evaluation Template library -- Template -> Section -> Criterion.

Company-scoped, like the Supplier Surveillance templates it mirrors (same shape, deliberately NOT the
same tables: the two processes have different fields and must evolve independently).

CRITICAL: an existing SupplierEvaluation never depends on these rows after creation. Q3 copies the
template once into `SupplierEvaluation.template_snapshot` (see services.supplier_evaluation_template
.build_template_snapshot); editing, deactivating or deleting a template, a section or a criterion here
never alters an evaluation that already exists.
"""

from django.db import models

from models.models import CompanyProfile, User

# Fixed rating scale. Labels are translated by the interface; N/A is an answer, not a score.
SCALE_MIN = 1
SCALE_MAX = 5
SCALE = [
    (1, "Very Poor"),
    (2, "Poor"),
    (3, "Acceptable"),
    (4, "Good"),
    (5, "Excellent"),
]
NOT_APPLICABLE = "N/A"


class EvaluationTemplate(models.Model):
    company = models.ForeignKey(
        CompanyProfile, related_name="evaluation_templates", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Stored, explicit counter: +1 on every content edit so a snapshot can say which version it froze.
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    # A score strictly below this value needs a comment (null = no template-wide threshold).
    comment_required_below = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        User, related_name="created_evaluation_templates",
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.name} (v{self.version})"


class EvaluationTemplateSection(models.Model):
    template = models.ForeignKey(
        EvaluationTemplate, related_name="sections", on_delete=models.CASCADE
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    # Optional weight (0 < w <= 100); null = unweighted. Its meaning in the score is a Q4 decision.
    weight = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class EvaluationTemplateCriterion(models.Model):
    section = models.ForeignKey(
        EvaluationTemplateSection, related_name="criteria", on_delete=models.CASCADE
    )
    label = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    weight = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    required = models.BooleanField(default=True)
    # A critical criterion is always required (enforced by the serializer).
    critical = models.BooleanField(default=False)
    allow_na = models.BooleanField(default=True)
    # Overrides the template threshold for this criterion (null = use the template's).
    comment_required_below = models.PositiveSmallIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.label
