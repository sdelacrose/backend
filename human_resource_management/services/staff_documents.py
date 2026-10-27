"""L03 -- write paths and resolution helpers for private/shared HR
documents. Permission checks (can_view_staff_document/can_update_staff_
document/can_upload_staff_document_file/can_transition_staff_document_file_
status) live in permissions.py and are ALWAYS checked by the caller (views)
before invoking these functions -- these functions assume authorization has
already been granted and focus on correctness (immutability, concurrency-
safe versioning, monotonic status transitions, same-company grants)."""
import hashlib
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Max

from human_resource_management.models.staff_documents import (
    StaffDocument, StaffDocumentEvent, StaffDocumentFile, StaffDocumentFileStatusEvent, StaffDocumentGrant,
)


class StaffDocumentError(Exception):
    """Raised for a confirmed business-rule violation (distinct from a
    permission refusal, which callers check beforehand)."""


_STATUS_RANK = {
    StaffDocumentFileStatusEvent.STATUS_SUBMITTED: 0,
    StaffDocumentFileStatusEvent.STATUS_VERIFIED: 1,
    StaffDocumentFileStatusEvent.STATUS_ACCEPTED: 2,
}


def resolve_file_status(file):
    """The file's CURRENT status is always its LATEST event -- never a
    cached/mutable field on StaffDocumentFile itself."""
    latest = file.status_events.order_by("-created_at", "-id").first()
    return latest.status if latest else StaffDocumentFileStatusEvent.STATUS_SUBMITTED


def create_staff_document(*, staff, company_profile, category, title, actor, sst_case=None):
    """Creates the "document slot" shell. `staff`/`company_profile`
    exclusivity (exactly one set, matching the category's scope) is
    enforced by StaffDocument.clean() at save time -- not re-validated
    here. `sst_case` (LOT L04): only ever passed for an `is_sst` category,
    by human_resource_management.services.sst -- never set from anywhere
    else."""
    document = StaffDocument(
        staff=staff, company_profile=company_profile, category=category, title=title,
        created_by=actor, sst_case=sst_case,
    )
    document.save()
    return document


def create_staff_document_file(*, staff_document, actor, uploaded_file, valid_from=None, valid_until=None):
    """Appends an IMMUTABLE new version. Version numbering is protected
    against a genuine concurrent-upload race the same way L01-B1 resolves a
    concurrent Staff-provisioning race: the DB UniqueConstraint
    (staff_document, version_number) rejects whichever of two concurrent
    attempts loses, and this function simply retries once with the next
    available number rather than surfacing a raw IntegrityError to the
    caller. A brand-new version ALWAYS starts at SUBMITTED -- never
    inherits a prior version's ACCEPTED status."""
    file_name = getattr(uploaded_file, "name", None) or "document"
    sha256 = hashlib.sha256()
    for chunk in uploaded_file.chunks():
        sha256.update(chunk)
    uploaded_file.seek(0)

    last_error = None
    for _attempt in range(5):
        next_version = (
            StaffDocumentFile.objects.filter(staff_document=staff_document)
            .aggregate(Max("version_number"))["version_number__max"] or 0
        ) + 1
        try:
            with transaction.atomic():
                doc_file = StaffDocumentFile(
                    staff_document=staff_document,
                    version_number=next_version,
                    file_name=file_name,
                    file_size=getattr(uploaded_file, "size", None),
                    file_sha256=sha256.hexdigest(),
                    valid_from=valid_from,
                    valid_until=valid_until,
                    uploaded_by=actor,
                )
                doc_file.file = uploaded_file
                doc_file.save()
                StaffDocumentFileStatusEvent.objects.create(
                    file=doc_file, status=StaffDocumentFileStatusEvent.STATUS_SUBMITTED, actor=actor,
                )
            return doc_file
        except IntegrityError as exc:
            last_error = exc
            continue
    raise StaffDocumentError("Could not allocate a version number after several concurrent attempts.") from last_error


def transition_staff_document_file_status(*, file, new_status, actor, note=""):
    """Monotonic, forward-only: SUBMITTED -> VERIFIED -> ACCEPTED (skipping
    VERIFIED to go straight to ACCEPTED is allowed; going BACKWARDS is not
    -- status is evidence of a review that happened, never silently undone).
    Callers MUST have already confirmed can_transition_staff_document_file_
    status (DEC-04: the employee concerned can never call this for their
    own document, checked by the caller BEFORE this function, same ordering
    convention as every other DEC-04 refusal in this project)."""
    current = resolve_file_status(file)
    if _STATUS_RANK[new_status] < _STATUS_RANK[current]:
        raise StaffDocumentError(f"Cannot move status backwards from '{current}' to '{new_status}'.")
    return StaffDocumentFileStatusEvent.objects.create(file=file, status=new_status, actor=actor, note=note)


def grant_staff_document_access(*, staff_document, user, actor):
    """A READ-ONLY, per-document, revocable grant -- the grantee must
    already belong to the SAME company (never an arbitrary external
    account, never a new external-provider pathway). Callers must have
    already confirmed can_update_staff_document(actor, staff_document)."""
    target_company_id = staff_document.company_profile_id
    if getattr(user, "company_id", None) != target_company_id:
        raise StaffDocumentError("The grantee must belong to the same company as this document.")
    grant = StaffDocumentGrant.objects.create(staff_document=staff_document, user=user, granted_by=actor)
    StaffDocumentEvent.objects.create(
        staff_document=staff_document, event_type=StaffDocumentEvent.EVENT_GRANT_CREATED, actor=actor,
        detail=f"granted to user {user.id}",
    )
    return grant


def revoke_staff_document_access(*, grant, actor):
    if grant.revoked_at is not None:
        raise StaffDocumentError("This grant is already revoked.")
    from django.utils import timezone

    grant.revoked_at = timezone.now()
    grant.revoked_by = actor
    grant.save(update_fields=["revoked_at", "revoked_by"])
    StaffDocumentEvent.objects.create(
        staff_document=grant.staff_document, event_type=StaffDocumentEvent.EVENT_GRANT_REVOKED, actor=actor,
        detail=f"revoked for user {grant.user_id}",
    )
    return grant


def rename_staff_document(*, staff_document, new_title, actor):
    old_title = staff_document.title
    staff_document.title = new_title
    staff_document.save(update_fields=["title"])
    StaffDocumentEvent.objects.create(
        staff_document=staff_document, event_type=StaffDocumentEvent.EVENT_TITLE_CHANGED, actor=actor,
        detail=f"'{old_title}' -> '{new_title}'",
    )
    return staff_document
