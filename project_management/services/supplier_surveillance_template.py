"""L3C: Supplier Surveillance Template library -- Company ownership, structure validation, and
the one explicit, stable snapshot shape captured into SupplierSurveillance.template_snapshot.
"""

from django.core.exceptions import ValidationError
from django.utils import timezone


def resolve_company_for_project(project):
    """The Company owning `project` -- same rule already used by
    services.project_codes._owner_projects / services.document_numbering._company_scope
    (`project.company_profile_id or project.user.company_id`), not reinvented here."""
    return project.company_profile_id or getattr(project.user, "company_id", None)


def assert_template_usable_for_surveillance(template, *, company_id):
    """A Template used to create a Surveillance must belong to the same Company as the
    Surveillance's Project, be active, and have a usable structure (L3C section 11)."""
    if template.company_id != company_id:
        raise ValidationError(
            "This Surveillance Template does not belong to the Project's company."
        )
    if not template.is_active:
        raise ValidationError("This Surveillance Template is not active.")
    sections = list(template.sections.all())
    if not sections:
        raise ValidationError(
            "This Surveillance Template has no Sections and cannot be used yet."
        )
    if not any(section.items.exists() for section in sections):
        raise ValidationError(
            "This Surveillance Template has no Items and cannot be used yet."
        )


def build_template_snapshot(template):
    """One explicit, stable JSON shape (L3C section 12) -- stable primitive values only, no
    ORM/model objects, no live relational dependency to render it back later. Sections/Items
    are read in their own deterministic Meta.ordering (order, then id)."""
    return {
        "template_id": template.id,
        "template_name": template.name,
        "template_version": template.version,
        "captured_at": timezone.now().isoformat(),
        "sections": [
            {
                "id": section.id,
                "title": section.title,
                "description": section.description,
                "order": section.order,
                "items": [
                    {
                        "id": item.id,
                        "label": item.label,
                        "description": item.description,
                        "type": item.item_type,
                        "required": item.required,
                        "order": item.order,
                    }
                    for item in section.items.all()
                ],
            }
            for section in template.sections.all()
        ],
    }
