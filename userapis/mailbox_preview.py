"""MAIL-1.3D-C2-B1: previewing ONE Outlook message the user selected, before any import.

Read-only and store-nothing: the plain-text body (never HTML, capped at 100 KB of UTF-8) and the
attachments' METADATA (never their content, never a download) are read from Microsoft Graph for
this one message and handed back; no row, file, log line or state is written. Importing -- and
with it the original .eml, the attachment downloads and every content check on what was actually
downloaded -- belongs to MAIL-1.3D-C2-B3.

The preview needs the right to READ the mailbox and nothing else: the project is optional, and a
correspondence right never stands in for a mailbox right.

Attachment references are signed, expiring tokens bound to the user, the mailbox, the message and
the attachment. They prepare the later import selection (`read_attachment_reference`, reusable
there) and, like the message reference, prove only that this server showed that attachment to that
user recently -- they authorize nothing.
"""

import logging

from django.core import signing

from userapis.mailbox_read import MailboxGraphReader, MailboxReadError, authorize_mailbox_read
from userapis.mailbox_search import InvalidSearchReference, read_search_reference
from userapis.models import MailboxConnection, MailboxMessage

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 100 * 1024
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_FILES = 20

ATTACHMENT_REFERENCE_SALT = "links.mailbox-attachment-reference"
ATTACHMENT_REFERENCE_MAX_AGE_SECONDS = 30 * 60

# Why an attachment cannot be selected (stable codes; the frontend words them).
NOT_IMPORTABLE_REASONS = {
    "item": "item_attachment_not_supported",
    "reference": "reference_attachment_not_supported",
    "unknown": "unknown_attachment_type",
}


class InvalidAttachmentReference(Exception):
    """Forged, altered, expired, issued to someone else, or for another mailbox/message."""


def truncate_utf8(text, limit=MAX_BODY_BYTES):
    """(text, truncated, total_bytes): `text` cut to at most `limit` UTF-8 bytes without ever
    splitting a character; `total_bytes` is the full text's size."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False, len(data)
    # The slice can only end inside ONE character; "ignore" drops that incomplete tail.
    return data[:limit].decode("utf-8", errors="ignore"), True, len(data)


def attachment_policy(attachment):
    """(importable, reason) -- the V1 selection rules known BEFORE downloading. The declared size
    and type are Graph's word only: the import must check the bytes it really receives."""
    if attachment.kind != "file":
        return False, NOT_IMPORTABLE_REASONS.get(attachment.kind, "unknown_attachment_type")
    if attachment.is_inline:
        return False, "inline_image"        # kept inside the original .eml, never a separate file
    if attachment.size is None or attachment.size < 0:
        return False, "size_unknown"
    if attachment.size == 0:
        return False, "empty_file"
    if attachment.size > MAX_FILE_BYTES:
        return False, "file_too_large"
    return True, ""


def build_attachment_reference(user, mailbox, message_graph_id, attachment_id, folder):
    return signing.dumps(
        {"u": user.pk, "m": mailbox.pk, "g": message_graph_id, "a": attachment_id, "f": folder},
        salt=ATTACHMENT_REFERENCE_SALT, compress=True,
    )


def read_attachment_reference(
    token, *, user, mailbox_id=None, message_graph_id=None, max_age=ATTACHMENT_REFERENCE_MAX_AGE_SECONDS,
):
    """Decode an attachment reference issued to `user`: {"mailbox_id", "message_id",
    "attachment_id", "folder"}. Signature, age and user are always checked; when given, the mailbox
    and message it must belong to are too. NOT an authorization: the caller re-authorizes."""
    try:
        payload = signing.loads(str(token or ""), salt=ATTACHMENT_REFERENCE_SALT, max_age=max_age)
    except signing.BadSignature:
        raise InvalidAttachmentReference()
    if not isinstance(payload, dict) or payload.get("u") != getattr(user, "pk", None):
        raise InvalidAttachmentReference()
    if mailbox_id is not None and payload.get("m") != mailbox_id:
        raise InvalidAttachmentReference()
    if message_graph_id is not None and payload.get("g") != message_graph_id:
        raise InvalidAttachmentReference()
    return {
        "mailbox_id": payload.get("m"), "message_id": payload.get("g"),
        "attachment_id": payload.get("a"), "folder": payload.get("f"),
    }


def _import_status(user, mailbox, meta, company_id):
    """Whether this message was already imported -- and, only if the user may see that
    correspondence, which one. Reads LINKS data only; touches nothing."""
    from userapis.mailbox_import import _can      # imported here: mailbox_import itself uses this module's limits

    rows = MailboxMessage.objects.filter(
        company_profile_id=company_id, imported_correspondence__isnull=False
    ).select_related("imported_correspondence")
    row = rows.filter(mailbox=mailbox, graph_message_id=meta.graph_id).first()
    if row is None and meta.internet_message_id:
        row = rows.filter(internet_message_id=meta.internet_message_id).first()    # same mail, another mailbox
    if row is None:
        return {"imported": False, "correspondence_id": None}
    corr = row.imported_correspondence
    visible = _can(user, "view", corr.project) if corr.project_id else False
    return {"imported": True, "correspondence_id": corr.pk if visible else None}


def preview_message(user, *, reference, project_id=None, reader=None):
    """The preview payload, or a MailboxReadError / InvalidSearchReference for every refusal."""
    from project_management.models import Project

    payload = read_search_reference(reference, user=user)           # InvalidSearchReference otherwise
    company_id = getattr(user, "company_id", None)
    mailbox = (
        MailboxConnection.objects.filter(pk=payload["mailbox_id"], company_profile_id=company_id).first()
        if company_id else None
    )
    project = None
    if project_id is not None and company_id:
        project = Project.objects.filter(pk=project_id, company_profile_id=company_id).first()
        if project is None:
            mailbox = None
    if mailbox is None:
        raise MailboxReadError("not_authorized")                     # unknown, foreign, unreadable: one answer
    authorize_mailbox_read(user, mailbox, project=project)

    folder = payload["folder"]
    reader = reader or MailboxGraphReader(mailbox, actor=user, project=project)
    content = reader.get_message_content(payload["graph_id"], folder)
    listing = reader.list_attachments(payload["graph_id"])

    body_text, body_truncated, body_size = truncate_utf8(content.body_text)
    attachments = []
    for item in listing.attachments:
        importable, reason = attachment_policy(item)
        attachments.append({
            "reference": build_attachment_reference(user, mailbox, payload["graph_id"], item.graph_id, folder),
            "name": item.name, "size": item.size, "content_type": item.content_type,
            "kind": item.kind, "is_inline": item.is_inline,
            "importable": importable, "reason": reason,
        })
    meta = content.meta
    # Counts only: neither the body nor any name or address is ever logged.
    logger.info(
        "Mailbox preview mailbox_id=%s user_id=%s attachments=%s truncated=%s",
        mailbox.pk, user.pk, len(attachments), body_truncated,
    )
    return {
        "mailbox": {
            "id": mailbox.pk, "email_address": mailbox.email_address,
            "display_name": mailbox.display_name, "connection_type": mailbox.connection_type,
        },
        "folder": folder,
        "message": {
            "sender_address": meta.sender_address, "sender_name": meta.sender_name,
            "to_addresses": list(meta.to_addresses), "cc_addresses": list(meta.cc_addresses),
            "subject": meta.subject, "received_at": meta.received_at,
            "has_attachments": meta.has_attachments,
        },
        "body_text": body_text, "body_truncated": body_truncated, "body_size": body_size,
        "attachments": attachments, "attachments_truncated": listing.truncated,
        "limits": {
            "max_body_bytes": MAX_BODY_BYTES, "max_file_bytes": MAX_FILE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES, "max_files": MAX_FILES,
        },
        "import_status": _import_status(user, mailbox, meta, company_id),
    }
