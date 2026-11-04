"""GED-L1.4: logical withdrawal of a Document (Company or Project library).

A withdrawn document is never deleted: its identifier, reference, versions, files, workflows,
events, transmittal items and proofs stay exactly as they are. The withdrawal only records who,
when and why (Document.withdrawn_by / withdrawn_at / withdrawal_reason) and closes the document to
every change, new version, validation and transmission. There is no restore in this lot.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from project_management.models import Document

MAX_REASON_LENGTH = 2000

WITHDRAWN_MESSAGE = (
    "This document has been withdrawn and can no longer be modified, versioned, validated "
    "or transmitted."
)
ALREADY_WITHDRAWN_MESSAGE = "This document is already withdrawn."
REASON_REQUIRED_MESSAGE = "A reason is required to withdraw a document."
WITHDRAWN_DELETE_MESSAGE = (
    "A withdrawn document is kept for the record and cannot be deleted."
)
TRANSMITTED_DELETE_MESSAGE = (
    "This document has been transmitted and cannot be deleted. Withdraw it instead."
)

# ?withdrawn=exclude (default) | include | only
WITHDRAWN_FILTER_MODES = ("exclude", "include", "only")


def is_withdrawn(document):
    """Fresh read: an instance loaded before a concurrent withdrawal must not slip through."""
    if not getattr(document, "pk", None):
        return False
    return Document.objects.filter(pk=document.pk, withdrawn_at__isnull=False).exists()


def assert_not_withdrawn(document):
    if is_withdrawn(document):
        raise ValidationError(WITHDRAWN_MESSAGE)


def document_is_transmitted(document):
    """Any version of the document is an item of a transmittal (draft or issued)."""
    from project_management.models import TransmittalItem

    if not getattr(document, "pk", None):
        return False
    return TransmittalItem.objects.filter(document_revision__document=document).exists()


def assert_no_active_document_workflow(document):
    from project_management.models import DocumentWorkflow
    if DocumentWorkflow.objects.filter(
        revision__document_id=document.pk, status__in=DocumentWorkflow.ACTIVE_STATUSES
    ).exists():
        raise ValidationError("This document is locked by an active workflow.")


def assert_physically_deletable(document):
    """Withdrawn and transmitted documents are never deleted: a business refusal, not a 500."""
    if not getattr(document, "pk", None):
        return
    if getattr(document, "withdrawn_at", None) or is_withdrawn(document):
        raise ValidationError(WITHDRAWN_DELETE_MESSAGE)
    assert_no_active_document_workflow(document)
    if document_is_transmitted(document):
        raise ValidationError(TRANSMITTED_DELETE_MESSAGE)


def filter_by_withdrawal(queryset, mode):
    """Active documents by default; `include` shows both, `only` the withdrawn ones."""
    mode = (mode or "exclude").strip().lower()
    if mode not in WITHDRAWN_FILTER_MODES:
        raise ValidationError(
            {"withdrawn": f"Unknown value. Use one of: {', '.join(WITHDRAWN_FILTER_MODES)}."}
        )
    if mode == "exclude":
        return queryset.filter(withdrawn_at__isnull=True)
    if mode == "only":
        return queryset.filter(withdrawn_at__isnull=False)
    return queryset


def withdraw_document(document, user, reason):
    """Withdraw one document. Atomic and row-locked; versions, files and proofs are not touched."""
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        raise ValidationError({"reason": REASON_REQUIRED_MESSAGE})
    if len(reason) > MAX_REASON_LENGTH:
        raise ValidationError(
            {"reason": f"The reason cannot exceed {MAX_REASON_LENGTH} characters."}
        )
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        if locked.withdrawn_at:
            raise ValidationError(ALREADY_WITHDRAWN_MESSAGE)
        assert_no_active_document_workflow(locked)
        locked.withdrawn_at = timezone.now()
        locked.withdrawn_by = user
        locked.withdrawal_reason = reason
        # simple_history: who, when (history_date) and a short reason on the historical row
        locked._history_user = user
        locked._change_reason = "Withdrawn"
        locked.save(
            update_fields=["withdrawn_at", "withdrawn_by", "withdrawal_reason", "updated_at"]
        )
    return locked
