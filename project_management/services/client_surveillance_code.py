"""GED-WF.4.1: effective client surveillance codes of a project.

The four categories are fixed and carry the business rules; a project only customises the visible code
and the EN/FR/DE labels. Without a stored row the defaults (A/B/C/D + the translations shipped with the
application) apply, and nothing is written. The consequences below are DERIVED from the category and
read-only: they describe what GED-WF.5 will apply, nothing here changes a document, a revision or a
status. Internal and supplier documents are treated differently, so no universal "internal validation"
flag is exposed.
"""

import re

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models import ClientSurveillanceCode

Category = ClientSurveillanceCode.Category
CODE_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
LABEL_FIELDS = ("label_en", "label_fr", "label_de")
MAX_LABEL = 100

# (category, default visible code), in display order
CATEGORIES = (
    (Category.ACCEPTED, "A"),
    (Category.ACCEPTED_WITH_COMMENTS, "B"),
    (Category.COMMENTED, "C"),
    (Category.REJECTED, "D"),
)
DEFAULT_CODE = dict(CATEGORIES)

# internal_validation: NONE (no new validation: code A keeps the validated index) / APPLICABLE_WORKFLOW (the full
# applicable internal validation is ALWAYS run again on the new revision, even for minor comments).
# applicable_status: IMMEDIATE (code A: the same index is applicable once the return is recorded) / AFTER_BPE_SEND
# (code B: applicable only once its BPE file was really sent) / NONE (PREL cycle, never applicable). LOT 1: BPE distributions are sent FOR_INFORMATION, PREL ones FOR_REVIEW.
_INTERNAL = {
    Category.ACCEPTED: {
        "keeps_revision_index": True, "new_revision_required": False, "manual_comment_integration": False,
        "internal_validation": "NONE", "applicable_status": "IMMEDIATE",
        "new_distribution_required": True, "redistribution_for_review": False,
    },
    Category.ACCEPTED_WITH_COMMENTS: {
        "keeps_revision_index": False, "new_revision_required": True, "manual_comment_integration": True,
        "internal_validation": "APPLICABLE_WORKFLOW", "applicable_status": "AFTER_BPE_SEND",
        "new_distribution_required": True, "redistribution_for_review": False,
    },
    Category.COMMENTED: {
        "keeps_revision_index": False, "new_revision_required": True, "manual_comment_integration": True,
        "internal_validation": "APPLICABLE_WORKFLOW", "applicable_status": "NONE",
        "new_distribution_required": True, "redistribution_for_review": True,
    },
    Category.REJECTED: {
        "keeps_revision_index": False, "new_revision_required": True, "manual_comment_integration": False,
        "internal_validation": "APPLICABLE_WORKFLOW", "applicable_status": "NONE",
        "new_distribution_required": True, "redistribution_for_review": True,
    },
}


def category_consequences(category):
    return {"internal_documents": dict(_INTERNAL[category]), "supplier_documents": "SPECIFIC_SUPPLIER_CIRCUIT"}


def _row_payload(category, row):
    default = DEFAULT_CODE[category]
    return {
        "category": category,
        "default_code": default,
        "code": row.code if row else default,
        "label_en": row.label_en if row else None,
        "label_fr": row.label_fr if row else None,
        "label_de": row.label_de if row else None,
        "customized": row is not None,
        "updated_at": row.updated_at if row else None,
        "updated_by": (row.updated_by.full_name if row and row.updated_by_id else None),
        "consequences": category_consequences(category),
    }


def list_effective_codes(project):
    rows = {r.category: r for r in ClientSurveillanceCode.objects.filter(project=project).select_related("updated_by")}
    return [_row_payload(category, rows.get(category)) for category, _ in CATEGORIES]


def effective_code(project, category):
    """Visible code of a category for a project (default when not customised)."""
    row = ClientSurveillanceCode.objects.filter(project=project, category=category).first()
    return row.code if row else DEFAULT_CODE[category]


def _clean_code(value):
    value = (value or "").strip()
    if not CODE_RE.match(value):
        raise ValidationError({"code": "The code is 1 to 10 letters or digits."})
    return value


def _clean_label(value, field):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    if len(value) > MAX_LABEL:
        raise ValidationError({field: f"A label is {MAX_LABEL} characters at most."})
    return value


def _check_unique_codes(final):
    """`final` is {category: code} for the four categories (case-insensitive uniqueness)."""
    seen = {}
    for category, code in final.items():
        key = code.lower()
        if key in seen:
            raise ValidationError({"code": f"The code {code} is already used by another category of this project."})
        seen[key] = category


@transaction.atomic
def save_codes(project, changes, actor):
    """Apply customisations in one operation: `changes` is a list of {category, code, label_en/fr/de}.

    The final set of four codes is validated as a whole (case-insensitive uniqueness), so two
    categories can swap their codes in one save. A category set back to its default code with no
    label is stored as "not customised" (no row). The category itself is never changed."""
    valid = {c for c, _ in CATEGORIES}
    existing = {r.category: r for r in ClientSurveillanceCode.objects.select_for_update().filter(project=project)}
    prepared = {}
    for change in changes:
        category = change.get("category")
        if category not in valid:
            raise ValidationError({"category": "Unknown category."})
        if category in prepared:
            raise ValidationError({"category": "A category is listed twice."})
        prepared[category] = {
            "code": _clean_code(change.get("code")),
            **{field: _clean_label(change.get(field), field) for field in LABEL_FIELDS},
        }
    final = {c: (prepared[c]["code"] if c in prepared else (existing[c].code if c in existing else DEFAULT_CODE[c]))
             for c, _ in CATEGORIES}
    _check_unique_codes(final)
    # Rows are removed before others are rewritten, so a swap never trips the unique index.
    for category, values in prepared.items():
        default = values["code"] == DEFAULT_CODE[category] and not any(values[f] for f in LABEL_FIELDS)
        if default and category in existing:
            existing.pop(category).delete()
    # two passes for the codes themselves: park the rows whose code changes on a unique temporary value
    changing = [c for c, v in prepared.items() if c in existing and existing[c].code.lower() != v["code"].lower()]
    for index, category in enumerate(changing):
        row = existing[category]
        ClientSurveillanceCode.objects.filter(pk=row.pk).update(code=f"~{index}{row.pk}"[:10])
    for category, values in prepared.items():
        if values["code"] == DEFAULT_CODE[category] and not any(values[f] for f in LABEL_FIELDS):
            continue
        row = existing.get(category)
        if row is None:
            ClientSurveillanceCode(project=project, category=category, updated_by=actor, **values).save()
        else:
            row.refresh_from_db()
            for field, value in values.items():
                setattr(row, field, value)
            row.updated_by = actor
            row.save()
    return list_effective_codes(project)


@transaction.atomic
def reset_category(project, category, actor):
    """Back to the defaults for one category. Refused if another category currently uses the
    default code of this one (free that code first), so the codes always stay unique."""
    if category not in DEFAULT_CODE:
        raise ValidationError({"category": "Unknown category."})
    default = DEFAULT_CODE[category]
    others = ClientSurveillanceCode.objects.filter(project=project).exclude(category=category)
    if any(r.code.lower() == default.lower() for r in others):
        raise ValidationError({"code": f"The default code {default} is used by another category: change that category first."})
    ClientSurveillanceCode.objects.filter(project=project, category=category).delete()
    return list_effective_codes(project)
