"""Reject content-identical versions unless a significant, traced metadata change exists.

Document history already records the metadata when the current file became current.
The first historical row for that exact storage key is the baseline. A successful upload
gets a distinct storage key and a new history row, so a metadata change cannot be reused.
No approved snapshot, historical row or existing revision is rewritten.
"""
from django.core.exceptions import ValidationError

# Stored documentary information, not the upload name, version counters, workflow
# decisions/assignees, generated references, attachments or automatic timestamps.
METADATA_FIELDS = (
    "file_name", "description", "tags", "author_id", "document_type", "category",
    "discipline", "discipline_group", "discipline_group_id", "discipline_option_id",
    "discipline_code", "document_origin", "external_issuer_company_id",
    "identification_number", "client_reference", "received_date", "status",
    "transmittal_category", "supplier_transmittal_category",
)

def metadata_values(instance):
    values = {}
    for field in METADATA_FIELDS:
        value = getattr(instance, field, None)
        if field == "tags":
            value = sorted({str(tag).strip() for tag in (value or []) if str(tag).strip()})
        elif value is None:
            value = ""
        elif isinstance(value, str):
            value = value.strip()
        else:
            value = str(value)
        values[field] = value
    return values

def checked_revision_upload_hash(document, uploaded_file, external_revision=None):
    from project_management.services.document_revision import sha256_of_file
    uploaded_hash = sha256_of_file(uploaded_file)
    if not document.file:
        return uploaded_hash  # First controlled file: preserve the existing exception.
    try:
        document.file.open("rb")
        try:
            current_hash = sha256_of_file(document.file)
        finally:
            document.file.close()
    except Exception as error:
        raise ValidationError({"file": "The current file cannot be read to compare its content. No version was created."}) from error
    if uploaded_hash != current_hash:
        return uploaded_hash

    baseline = document.history.filter(file=document.file.name).order_by("history_date", "history_id").first()
    if baseline is not None and metadata_values(baseline) != metadata_values(document):
        return uploaded_hash

    # An external issuer's explicit revision designation is meaningful version metadata.
    # It must never be used to bypass duplicate detection for INTERNAL documents.
    if document.document_origin in ("SUPPLIER", "CLIENT"):
        latest = document.revisions.order_by("-version_number", "-id").first()
        previous = (latest.external_revision or "").strip() if latest else ""
        if (external_revision or "").strip() != previous:
            return uploaded_hash

    raise ValidationError({
        "file": "The file content and significant document information are unchanged. Choose a modified file or update the document information first.",
        "code": "unchanged_document_revision",
    })
