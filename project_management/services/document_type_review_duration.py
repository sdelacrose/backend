"""L3H-1: resolution + validation for Document Review Durations configuration.

`resolve_review_durations`/`list_effective_review_durations` are read-only: neither ever
creates a database row as a side effect (L3H-1 section 10/Q) -- a null override always
resolves to DEFAULT_REVIEW_DURATION_DAYS without needing a persisted row at all. Only
`upsert_review_duration` persists, and only the fields explicitly present in its `values` dict
(L3H-1 section 5: editing one duration must never touch the other three).
"""

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models.document_type_review_duration import (
    DEFAULT_REVIEW_DURATION_DAYS,
    DURATION_FIELD_NAMES,
    DocumentTypeReviewDuration,
)

# FINISH-B section 7/22: the one user Save action persists every dirty row in a single request
# -- narrow and transactional, reusing upsert_review_duration's own validation verbatim (never a
# second, parallel validation path). A generous but real cap, same defensive-limit convention as
# MAX_BULK_DOCUMENTS elsewhere, even though a project's own configured Document Types list is
# realistically far smaller.
MAX_BULK_REVIEW_DURATION_ROWS = 200


def _effective_field_name(field_name):
    # "supplier_review_duration_days" -> "supplier_review_duration_effective_days"
    return f"{field_name[: -len('_days')]}_effective_days"


def _validate_duration_value(field_name, value):
    """L3H-1 section 9: a positive integer number of calendar days, or null (meaning "use the
    system default", section 8). Model-field validators never run on a plain .save() call, so
    this is the one place every write path (the service's own upsert) is actually validated."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError({field_name: "Must be a whole number of days."})
    if value < 1:
        raise ValidationError({field_name: "Must be at least 1 day."})
    return value


def _project_document_types(project):
    """ECPMPConfig.document_types entries may be a plain label ("Drawing") or a "Label:CODE"
    pair ("Drawing:DWG") -- same `choice.split(":")[0]` extraction already used by
    serializers.document.DocumentSerializer.validate_document_type, reused verbatim here rather
    than re-deriving a second, possibly-inconsistent rule (L3H-1 section 11)."""
    from project_management.models import ECPMPConfig

    config = ECPMPConfig.objects.filter(project=project).first()
    if not config or not config.document_types:
        return []
    return [choice.split(":")[0] for choice in config.document_types]


def assert_document_type_known(project, document_type):
    """L3H-1 section 11: reuses ECPMPConfig.document_types for validation -- never a second,
    independent Document Type list."""
    if document_type not in _project_document_types(project):
        raise ValidationError(
            {"document_type": "This Document Type is not configured for this project."}
        )


def resolve_review_durations(*, project, document_type):
    """The 4 effective values for (project, document_type) -- never creates a row."""
    row = DocumentTypeReviewDuration.objects.filter(
        project=project, document_type=document_type,
    ).first()
    result = {"document_type": document_type}
    for field_name in DURATION_FIELD_NAMES:
        raw = getattr(row, field_name) if row is not None else None
        result[field_name] = raw
        result[_effective_field_name(field_name)] = (
            raw if raw is not None else DEFAULT_REVIEW_DURATION_DAYS
        )
    return result


def list_effective_review_durations(project):
    """One dict per the project's own configured Document Types (ECPMPConfig.document_types),
    each merged with any existing override row -- read-only, no row created merely by listing
    (L3H-1 section 10/Q)."""
    return [
        resolve_review_durations(project=project, document_type=document_type)
        for document_type in _project_document_types(project)
    ]


def upsert_review_duration(*, project, document_type, values):
    """`values`: a dict possibly containing any of DURATION_FIELD_NAMES (keys left out are
    never touched -- L3H-1 section 5/18-F). A value of None resets that one field to the system
    default by storing NULL, never the literal 15 (section 8/19)."""
    assert_document_type_known(project, document_type)
    cleaned = {}
    for field_name in DURATION_FIELD_NAMES:
        if field_name in values:
            cleaned[field_name] = _validate_duration_value(field_name, values[field_name])

    row, _created = DocumentTypeReviewDuration.objects.get_or_create(
        project=project, document_type=document_type,
    )
    if cleaned:
        for field_name, value in cleaned.items():
            setattr(row, field_name, value)
        row.save(update_fields=[*cleaned.keys(), "updated_at"])
    return row


def bulk_upsert_review_durations(*, project, rows):
    """FINISH-B section 7: one Save action persists every dirty row. `rows`: a list of dicts,
    each `{"document_type": ..., <any of DURATION_FIELD_NAMES>: ...}` -- exactly the existing
    `upsert_review_duration` call, once per row, inside one outer transaction with a per-row
    savepoint (the same "one bad row never rolls back the others" shape
    services.document_bulk_update.DocumentBulkUpdateService already uses). Only rows the caller
    actually sends are touched -- the frontend is responsible for sending dirty rows only
    (section 7's "do not send requests for unchanged rows"); this function has no opinion on
    which rows are "dirty", it only guarantees each one it IS given is applied/validated
    independently and never partially.
    """
    if len(rows) > MAX_BULK_REVIEW_DURATION_ROWS:
        raise ValidationError(
            {"rows": f"At most {MAX_BULK_REVIEW_DURATION_ROWS} rows per request."}
        )
    updated = []
    failures = []
    with transaction.atomic():
        for row in rows:
            document_type = row.get("document_type")
            values = {
                field_name: row[field_name]
                for field_name in DURATION_FIELD_NAMES
                if field_name in row
            }
            try:
                with transaction.atomic():
                    upsert_review_duration(
                        project=project, document_type=document_type, values=values,
                    )
            except ValidationError as error:
                failures.append(
                    {"document_type": document_type, "error": _error_payload(error)}
                )
            else:
                updated.append(document_type)
    return {"updated": updated, "failures": failures}


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}
