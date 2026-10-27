"""L03 -- plain allowlist serialization (never a raw storage URL -- only a
link back to the gated download action, same convention as
serialize_leave_attachment)."""
from rest_framework.reverse import reverse

from project_management.serializers.file_name import PATH_SEPARATORS, CONTROL_CHARACTERS

from human_resource_management.services.staff_documents import resolve_file_status

MAX_UPLOAD_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB -- a new, narrow default for this lot (no pre-existing generic cap found to reuse).
ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}


def validate_upload(uploaded_file):
    """Reuses project_management.serializers.file_name's control-character/
    path-separator checks on the filename; size and extension allowlisting
    are NEW, narrow checks specific to this lot (no pre-existing generic
    mechanism for either was found to reuse)."""
    errors = []
    name = getattr(uploaded_file, "name", "") or ""
    if CONTROL_CHARACTERS.search(name) or PATH_SEPARATORS.search(name):
        errors.append("File name cannot contain path separators or control characters.")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in ALLOWED_EXTENSIONS:
        errors.append(f"File type '.{extension}' is not allowed.")
    size = getattr(uploaded_file, "size", 0) or 0
    if size > MAX_UPLOAD_SIZE_BYTES:
        errors.append(f"File is too large (max {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB).")
    return errors


def serialize_category(category):
    return {
        "id": category.id,
        "name": category.name,
        "scope": category.scope,
        "requires_validity_date": category.requires_validity_date,
        "is_active": category.is_active,
        "is_sst": category.is_sst,
    }


def serialize_grant(grant):
    return {
        "id": grant.id,
        "user": {"id": grant.user_id, "name": getattr(grant.user, "full_name", None)},
        "granted_by": {"id": grant.granted_by_id, "name": getattr(grant.granted_by, "full_name", None)} if grant.granted_by_id else None,
        "granted_at": grant.granted_at,
        "revoked_by": {"id": grant.revoked_by_id, "name": getattr(grant.revoked_by, "full_name", None)} if grant.revoked_by_id else None,
        "revoked_at": grant.revoked_at,
        "is_active": grant.is_active,
    }


def serialize_file(file, request):
    return {
        "id": file.id,
        "version_number": file.version_number,
        "file_name": file.file_name,
        "file_size": file.file_size,
        "valid_from": file.valid_from,
        "valid_until": file.valid_until,
        "status": resolve_file_status(file),
        "uploaded_by": {"id": file.uploaded_by_id, "name": getattr(file.uploaded_by, "full_name", None)} if file.uploaded_by_id else None,
        "uploaded_at": file.uploaded_at,
        "download_url": reverse(
            "staff-document-file-download",
            kwargs={"staff_document_id": file.staff_document_id, "file_id": file.id},
            request=request,
        ),
    }


def serialize_document(document, request, *, include_files=True, include_grants=False):
    data = {
        "id": document.id,
        "staff_id": document.staff_id,
        "company_profile_id": document.company_profile_id,
        "category": serialize_category(document.category),
        "title": document.title,
        "created_at": document.created_at,
        "sst_case_id": document.sst_case_id,
    }
    if include_files:
        data["files"] = [serialize_file(f, request) for f in document.files.order_by("-version_number")]
    if include_grants:
        data["grants"] = [serialize_grant(g) for g in document.grants.all()]
    return data
