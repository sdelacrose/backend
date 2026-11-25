"""QUALITY Q2: helpers shared by the template API and (from Q3) the evaluation snapshot."""

from decimal import Decimal

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


# ---------------------------------------------------------------------------------------------- weights
WEIGHT_TOTAL = Decimal("100")


def weight_set_problem(weights):
    """Rule for one set of siblings (the sections of a template, or the criteria of a section):
    either NO weight at all, or EVERY weight present and summing to exactly 100.
    Returns None when valid, else "partial" or "sum"."""
    if not weights:
        return None
    given = [w for w in weights if w is not None]
    if not given:
        return None
    if len(given) != len(weights):
        return "partial"
    return None if sum(given) == WEIGHT_TOTAL else "sum"


def template_weight_problem(sections):
    """`sections`: iterable of (section_weight, [criterion_weight, ...]). Returns a readable message or None."""
    sections = list(sections)
    problem = weight_set_problem([weight for weight, _ in sections])
    if problem:
        return f"section weights: {_WEIGHT_MESSAGES[problem]}"
    for position, (_, criteria) in enumerate(sections, start=1):
        problem = weight_set_problem(list(criteria))
        if problem:
            return f"criterion weights of section {position}: {_WEIGHT_MESSAGES[problem]}"
    return None


_WEIGHT_MESSAGES = {
    "partial": "either no weight at all or a weight on every item (partial weights are not allowed).",
    "sum": "the weights must add up to exactly 100.",
}


def stored_template_weight_problem(template):
    return template_weight_problem(
        (section.weight, [criterion.weight for criterion in section.criteria.all()])
        for section in template.sections.all()
    )
