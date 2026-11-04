"""Explicit nature of a version change, recorded in its existing creation audit reason.

Do not infer this business declaration from legacy major/minor counters. History is
only written by Document.save at version creation; existing rows are never changed.
"""
from django.core.exceptions import ValidationError

CONTEXTS = {
    "internal_iteration": ("minor", "New document version: internal iteration"),
    "client_feedback": ("major", "New document version: following client feedback"),
}

def validate_change_context(document, context, revision_type):
    if context in (None, ""):
        return None
    if not isinstance(context, str) or context not in CONTEXTS:
        raise ValidationError({"change_context": "Use internal_iteration or client_feedback."})
    if document.document_origin != "INTERNAL":
        raise ValidationError({"change_context": "This change context applies only to internal documents."})
    if CONTEXTS[context][0] != revision_type:
        raise ValidationError({"change_context": "The declared change context conflicts with the version type."})
    return context

def change_contexts_for_files(document, file_names):
    if not file_names:
        return {}
    reasons = {reason: context for context, (_kind, reason) in CONTEXTS.items()}
    contexts = {}
    # One query for an entire history response, independent of its number of versions.
    rows = document.history.filter(file__in=file_names).order_by("history_date", "history_id").values(
        "file", "history_change_reason")
    for row in rows:
        if row["file"] not in contexts:
            contexts[row["file"]] = reasons.get(row["history_change_reason"])
    return contexts
