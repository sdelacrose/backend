"""QUALITY Q2: helpers shared by the template API and (from Q3) the evaluation snapshot."""

from django.db import transaction

from project_management.models import (
    EvaluationTemplate,
    EvaluationTemplateCriterion,
    EvaluationTemplateSection,
)
from project_management.models.supplier_evaluation_template import NOT_APPLICABLE, SCALE


def _number(value):
    return None if value is None else str(value)


def build_template_snapshot(template):
    """Pure-data, JSON-serialisable frozen copy of a template (Q3 stores it on the evaluation).

    Ids are kept only so Q3 can reference a criterion inside its own snapshot; the copy never reads the
    live rows again. Ordering is the declared one, so two calls on an unchanged template are equal.
    """
    return {
        "template_id": template.id,
        "version": template.version,
        "name": template.name,
        "description": template.description,
        "comment_required_below": template.comment_required_below,
        "scale": [{"value": value, "label": label} for value, label in SCALE],
        "not_applicable": NOT_APPLICABLE,
        "sections": [
            {
                "id": section.id,
                "title": section.title,
                "description": section.description,
                "order": section.order,
                "weight": _number(section.weight),
                "criteria": [
                    {
                        "id": criterion.id,
                        "label": criterion.label,
                        "description": criterion.description,
                        "order": criterion.order,
                        "weight": _number(criterion.weight),
                        "required": criterion.required,
                        "critical": criterion.critical,
                        "allow_na": criterion.allow_na,
                        "comment_required_below": criterion.comment_required_below,
                    }
                    for criterion in section.criteria.all()
                ],
            }
            for section in template.sections.all()
        ],
    }


@transaction.atomic
def duplicate_template(template, *, user, name):
    copy = EvaluationTemplate.objects.create(
        company_id=template.company_id,
        name=name,
        description=template.description,
        comment_required_below=template.comment_required_below,
        is_active=True,
        created_by=user,
    )
    for section in template.sections.all():
        new_section = EvaluationTemplateSection.objects.create(
            template=copy, title=section.title, description=section.description,
            order=section.order, weight=section.weight,
        )
        EvaluationTemplateCriterion.objects.bulk_create(
            EvaluationTemplateCriterion(
                section=new_section, label=c.label, description=c.description, order=c.order,
                weight=c.weight, required=c.required, critical=c.critical, allow_na=c.allow_na,
                comment_required_below=c.comment_required_below,
            )
            for c in section.criteria.all()
        )
    return copy
