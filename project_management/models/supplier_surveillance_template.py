"""L3C: Supplier Surveillance Template library -- Template -> Section -> Item.

Company-scoped (not Project-scoped): the same Template can be reused across the Projects of the
company that owns it (L3C section 4), mirroring the existing "Company owning a Project" rule
already used by services.project_codes/document_numbering
(`project.company_profile_id or project.user.company_id`) -- see
services.supplier_surveillance_template.resolve_company_for_project.

CRITICAL: an existing SupplierSurveillance never depends on these rows after creation -- it
only ever stores an immutable copy in its own `template_snapshot` JSONField (L3B). Editing a
Template, its Sections or Items here must never alter an already-created Surveillance.
"""

from django.db import models

from models.models import CompanyProfile, User


class SupplierSurveillanceTemplate(models.Model):
    company = models.ForeignKey(
        CompanyProfile,
        related_name="supplier_surveillance_templates",
        on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    # Explicit stored metadata only (L3C section 6) -- no automatic version-cloning engine.
    version = models.PositiveIntegerField(default=1)
    # Inactive Templates stay readable (historical Surveillances already hold their own
    # snapshot) but are never offered again for a new Surveillance.
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_supplier_surveillance_templates",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]

    def __str__(self):
        return f"{self.name} (v{self.version})"


class SupplierSurveillanceTemplateSection(models.Model):
    template = models.ForeignKey(
        SupplierSurveillanceTemplate, related_name="sections", on_delete=models.CASCADE,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Deterministic snapshot ordering (L3C section 30): declared order, PK as tie-breaker.
        ordering = ["order", "id"]

    def __str__(self):
        return self.title


class SupplierSurveillanceTemplateItem(models.Model):
    class ItemType(models.TextChoices):
        CHECK = "CHECK", "Check"
        TEXT = "TEXT", "Text"
        TEXTAREA = "TEXTAREA", "Textarea"

    section = models.ForeignKey(
        SupplierSurveillanceTemplateSection, related_name="items", on_delete=models.CASCADE,
    )
    label = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    item_type = models.CharField(
        max_length=20, choices=ItemType.choices, default=ItemType.CHECK,
    )
    required = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return self.label
