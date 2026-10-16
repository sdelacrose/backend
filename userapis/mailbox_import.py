"""MAIL-1.3D-C2-A/B3: importing an Outlook message a user found (C1) into LINKS, on their explicit order.

Three modes, each ending with the message's row marked `imported` and pointing at the correspondence
(`MailboxMessage.imported_correspondence`, never the OUT link `correspondence`, which this module
never touches):
  - create: a new IN correspondence, built by the EXISTING CorrespondenceSerializer (numbering
    rules, parent/scope validation, response tracking -- nothing re-implemented). The sender's
    `external_reference` is optional here: without one the IN gets a LINKS reference from the
    existing counter (MAIL-1.3D-C2-B2). Only an Inbox message can become an IN.
  - attach: the message becomes the recorded source of an EXISTING correspondence (an IN for an
    Inbox message, an OUT for a Sent Items message). Its fields are not changed.
  - complete: finishes an import already made -- adds what is missing (the text if the
    correspondence has none, the original .eml, further selected attachments), never recreates the
    correspondence and never overwrites anything.

A message found in Sent Items is NEVER turned into a new correspondence here. An OUT is created
through the validation and send path (reference, approval, send evidence, response tracking
starting at the confirmed send); recording a mail LINKS never sent as an issued OUT would bypass
all of it. That mode is explicitly blocked (`out_creation_not_supported`); attaching to an existing
OUT is still possible.

MAIL-1.3D-C2-B3 -- what is kept, always: the plain text of the message (never HTML, at most 100 KB
of UTF-8) in `Correspondence.message` when that is empty, and the ORIGINAL message as an `.eml`
(there is no switch to turn that off); plus, only if the user selected them by their signed
references, file attachments. All of it is stored as ordinary CorrespondenceAttachment rows --
evidence of the correspondence, never a controlled Document -- with a provenance row
(MailboxImportedFile). Everything is downloaded BEFORE the database transaction, into bounded
temporary files, under strict size limits, safety checks (userapis.mailbox_import_files) and
SHA-256 de-duplication; the transaction then writes the correspondence, the files and the
`imported` state together. A failure rolls the database back and deletes the files that attempt
had written (file storage itself is not transactional: a crash between the write and the commit
can leave an unreferenced file, which a separate clean-up lot will address).

The signed references prove only that the server showed this message/attachment to this user
recently. Before anything happens the user's identity, the mailbox READ right (with the project
assignment), the correspondence create/update right, the project's company, and the message and
each chosen attachment in Outlook are all re-verified. Contacts and companies are never guessed
from an address: only ids the user sent are used.

One message is imported once: the row is locked, a repeat returns the existing result (same
project) or a conflict (another project / another correspondence), and a second mailbox holding the
same Internet message cannot import it again into the same project.
"""

import hashlib
import logging
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass, field

from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from userapis import mailbox_preview as limits
from userapis.mailbox_import_files import (
    FileRefused, InvalidEml, check_content, check_name_and_type, eml_file_name, validate_eml,
)
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import MailboxGraphReader, MailboxReadError, authorize_mailbox_read
from userapis.mailbox_search import InvalidSearchReference, read_search_reference
from userapis.models import MailboxConnection, MailboxImportedFile, MailboxMessage

logger = logging.getLogger(__name__)

MODE_CREATE = "create"
MODE_ATTACH = "attach"
MODE_COMPLETE = "complete"
NO_SUBJECT = "(no subject)"
DEFAULT_MAX_EML_BYTES = 50 * 1024 * 1024
TEMP_FILE_MEMORY = 2 * 1024 * 1024        # a temporary file stays in memory up to this size, then goes to disk


class MailboxImportError(Exception):
    """A refusal with a stable code; `extra` carries ids the caller is entitled to (never content)."""

    def __init__(self, code, detail, status=400, extra=None):
        super().__init__(detail)
        self.code, self.detail, self.status, self.extra = code, detail, status, extra or {}


@dataclass
class ImportResult:
    outcome: str    # created | attached | completed | already_imported | already_complete
    correspondence: object
    message: object
    documents: dict = field(default_factory=dict)


@dataclass
class _Prepared:
    kind: str
    name: str
    size: int
    sha256: str
    content_type: str
    fh: object
    graph_attachment_id: str = ""


def _can(user, action, project):
    """The correspondence_<action> right, exactly as CorrespondencePermission decides it."""
    from project_management.permissions import CorrespondencePermission

    permission = CorrespondencePermission(action)
    scope, context = permission._scope_and_context(project)
    return permission._check_action_or_full_access(user, f"correspondence_{action}", scope, context)


def _lock_import(project, meta):
    """Serialise concurrent imports of the same Internet message into the same project, even from
    two different mailboxes (transaction-scoped advisory lock; PostgreSQL only)."""
    if connection.vendor != "postgresql":
        return
    key = f"mailbox-import:{project.pk}:{meta.internet_message_id or meta.graph_id}"
    number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [number])


def _documents_summary(row, **more):
    return {
        "eml_stored": MailboxImportedFile.objects.filter(
            mailbox_message=row, kind=MailboxImportedFile.Kind.ORIGINAL_EML
        ).exists(),
        **more,
    }


def _existing(row, project):
    """Retrying an import that already succeeded: the existing result for the same project, an
    explicit conflict for anything else -- never a second correspondence."""
    corr = row.imported_correspondence
    if corr.project_id == project.pk:
        return ImportResult("already_imported", corr, row, _documents_summary(row))
    raise MailboxImportError("already_imported", "This message has already been imported elsewhere.", 409)


def _create_in(request, project, meta, fields, text):
    from project_management.serializers.correspondence import CorrespondenceSerializer

    data = {
        "direction": "IN", "status": "ISSUED", "project": project.pk,
        "subject": meta.subject or NO_SUBJECT,
    }
    if text:
        data["message"] = text
    # The sender's own reference when there is one; otherwise none is invented: the serializer is
    # told (by this service only, after the authorization above) to give the IN a LINKS reference.
    external_reference = (fields.get("external_reference") or "").strip()
    context = {"request": request}
    if external_reference:
        data["external_reference"] = external_reference
    else:
        context["allow_missing_external_reference"] = True
    if meta.received_at:
        data["received_date"] = timezone.localtime(meta.received_at).date().isoformat()
    for key in ("correspondence_type", "sender_company", "sender_contact", "parent_correspondence", "response_required"):
        if fields.get(key) not in (None, ""):
            data[key] = fields[key]
    serializer = CorrespondenceSerializer(data=data, context=context)
    serializer.is_valid(raise_exception=True)
    return serializer.save()


# --- the files: prepared BEFORE the transaction ---------------------------------------------------------

def _temp_file(stack):
    return stack.enter_context(tempfile.SpooledTemporaryFile(max_size=TEMP_FILE_MEMORY))


def _eml_limit():
    return int(getattr(settings, "MAIL_IMPORT_MAX_EML_BYTES", DEFAULT_MAX_EML_BYTES))


def _prepare_eml(reader, ref, meta, budget, stack):
    fh = _temp_file(stack)
    cap = min(_eml_limit(), budget)
    try:
        size, digest = reader.download_message_mime(ref["graph_id"], fh, cap)
    except MailboxReadError as exc:
        if exc.code == "content_too_large":
            raise MailboxImportError(
                "eml_too_large", "The original message is larger than the allowed size; it cannot be imported.", 413,
            )
        if exc.code == "not_found":
            raise MailboxImportError("message_gone", "This message no longer exists in that folder of the mailbox.", 404)
        raise
    try:
        if size <= 0:
            raise InvalidEml()
        validate_eml(fh)          # the HTTP Content-Type is ignored: only the structure counts
    except InvalidEml:
        raise MailboxImportError("invalid_eml", "Microsoft 365 did not return a valid original message.", 502)
    return _Prepared(
        kind=MailboxImportedFile.Kind.ORIGINAL_EML, name=eml_file_name(meta.subject), size=size,
        sha256=digest, content_type="message/rfc822", fh=fh,
    )


def _prepare_attachments(reader, ref, mailbox, user, references, skip_ids, seen_hashes, budget, stack):
    """Validate every selected reference, then download only the files still needed. Returns
    (prepared, skipped, budget_left). Any refusal refuses the whole import: the user deselects."""
    from userapis.mailbox_preview import InvalidAttachmentReference, attachment_policy, read_attachment_reference

    selected = []
    for index, token in enumerate(references):
        try:
            payload = read_attachment_reference(token, user=user, mailbox_id=mailbox.pk, message_graph_id=ref["graph_id"])
        except InvalidAttachmentReference:
            raise MailboxImportError("invalid_attachment_reference", "An attachment selection is no longer valid; preview again.", 400, {"index": index})
        if payload["folder"] != ref["folder"]:
            raise MailboxImportError("invalid_attachment_reference", "An attachment selection is no longer valid; preview again.", 400, {"index": index})
        if payload["attachment_id"] not in [s[1] for s in selected]:
            selected.append((index, payload["attachment_id"]))
    if not selected:
        return [], [], budget

    listing = {a.graph_id: a for a in reader.list_attachments(ref["graph_id"]).attachments}
    prepared, skipped = [], []
    for index, attachment_id in selected:
        meta = listing.get(attachment_id)
        if meta is None:
            raise MailboxImportError("attachment_gone", "A selected attachment no longer exists.", 404, {"index": index})
        importable, reason = attachment_policy(meta)          # file, not inline, declared size within the cap
        if not importable:
            raise MailboxImportError("attachment_not_importable", "A selected attachment cannot be imported.", 400, {"index": index, "reason": reason})
        try:
            name = check_name_and_type(meta.name, meta.content_type)
        except FileRefused as exc:
            raise MailboxImportError("attachment_not_allowed", "A selected file type is not allowed.", 400, {"index": index, "reason": exc.code})
        if attachment_id in skip_ids:
            skipped.append({"name": name, "reason": "already_present"})
            continue
        fh = _temp_file(stack)
        cap = min(limits.MAX_FILE_BYTES, budget)
        try:
            size, digest = reader.download_attachment(ref["graph_id"], attachment_id, fh, cap)
        except MailboxReadError as exc:
            if exc.code == "content_too_large":
                code = "file_too_large" if cap == limits.MAX_FILE_BYTES else "import_too_large"
                raise MailboxImportError(code, "A file, or the whole import, is larger than the allowed size.", 413, {"index": index})
            if exc.code == "not_found":
                raise MailboxImportError("attachment_gone", "A selected attachment no longer exists.", 404, {"index": index})
            raise
        try:
            check_content(fh, size)                           # what was really received, not what was announced
        except FileRefused as exc:
            raise MailboxImportError("attachment_not_allowed", "A selected file is not allowed.", 400, {"index": index, "reason": exc.code})
        if digest in seen_hashes:
            skipped.append({"name": name, "reason": "duplicate_content"})
            continue
        seen_hashes.add(digest)
        budget -= size
        prepared.append(_Prepared(
            kind=MailboxImportedFile.Kind.ATTACHMENT, name=name, size=size, sha256=digest,
            content_type=str(meta.content_type or "")[:255], fh=fh, graph_attachment_id=attachment_id,
        ))
    return prepared, skipped, budget


def _store_file(correspondence, row, user, item, stored):
    """One CorrespondenceAttachment (the existing evidence mechanism) and its provenance row."""
    from project_management.models import CorrespondenceAttachment

    attachment = CorrespondenceAttachment(
        correspondence=correspondence, file_name=item.name, file_size=item.size, uploaded_by=user,
    )
    item.fh.seek(0)
    attachment.file.save(item.name, File(item.fh), save=False)        # the write to storage happens here
    stored.append((attachment.file.storage, attachment.file.name))
    attachment.save()
    MailboxImportedFile.objects.create(
        mailbox_message=row, correspondence=correspondence, correspondence_attachment=attachment,
        kind=item.kind, graph_attachment_id=item.graph_attachment_id, file_name=item.name,
        file_size=item.size, sha256=item.sha256, content_type=item.content_type, created_by=user,
    )


def _cleanup(stored):
    """Delete the files THIS attempt wrote (storage is not transactional). Never anything else."""
    for storage, name in stored:
        try:
            storage.delete(name)
        except Exception:                          # a failed delete must not hide the original error
            logger.warning("Could not remove a file written by a failed mailbox import")


def import_message(
    request, *, reference, project_id, mode, target_id=None, fields=None, attachment_references=(), reader=None,
):
    from project_management.models import Correspondence, Project
    from project_management.views.correspondence import correspondence_queryset_for_user
    from userapis.mailbox_preview import truncate_utf8

    user = request.user
    fields = fields or {}
    attachment_references = list(attachment_references or [])
    if len(attachment_references) > limits.MAX_FILES:
        raise MailboxImportError("too_many_attachments", f"At most {limits.MAX_FILES} attachments can be imported.", 400)

    # 1. the reference: signature, age, and that it was issued to THIS user
    try:
        ref = read_search_reference(reference, user=user)
    except InvalidSearchReference:
        raise MailboxImportError("invalid_reference", "This selection is no longer valid; search again.", 400)

    # 2. company, mailbox and project: the generic refusal for anything not theirs
    company_id = getattr(user, "company_id", None)
    mailbox = (
        MailboxConnection.objects.filter(pk=ref["mailbox_id"], company_profile_id=company_id).first()
        if company_id else None
    )
    project = Project.objects.filter(pk=project_id, company_profile_id=company_id).first() if company_id else None
    if mailbox is None or project is None:
        raise MailboxReadError("not_authorized")
    authorize_mailbox_read(user, mailbox, project=project)       # READ right + project assignment

    # 3. the correspondence rights and the mode's own rules
    folder = ref["folder"]
    scoped = correspondence_queryset_for_user(user)
    target = None
    existing_row = (
        MailboxMessage.objects.filter(mailbox=mailbox, graph_message_id=ref["graph_id"])
        .select_related("imported_correspondence").first()
    )
    if mode == MODE_CREATE:
        if folder != "inbox":
            raise MailboxImportError(
                "out_creation_not_supported",
                "A sent message cannot be turned into a new correspondence; attach it to an existing "
                "outgoing correspondence instead.", 409,
            )
        if not _can(user, "create", project):
            raise MailboxImportError("correspondence_permission_denied", "You may not create correspondences in this project.", 403)
        parent_id = fields.get("parent_correspondence")
        if parent_id and not scoped.filter(pk=parent_id, project_id=project.pk).exists():
            raise MailboxImportError("invalid_target", "The correspondence to answer was not found in this project.", 400)
    else:
        if mode == MODE_COMPLETE:
            if existing_row is None or not existing_row.imported_correspondence_id:
                raise MailboxImportError("not_imported", "This message has not been imported yet.", 409)
            target_id = existing_row.imported_correspondence_id
        target = scoped.filter(pk=target_id, project_id=project.pk).first() if target_id else None
        if target is None:
            raise MailboxImportError("invalid_target", "The target correspondence was not found in this project.", 400)
        expected = Correspondence.Direction.IN if folder == "inbox" else Correspondence.Direction.OUT
        if mode == MODE_ATTACH and target.direction != expected:
            raise MailboxImportError(
                "direction_mismatch",
                "A received message attaches to an incoming correspondence, a sent one to an outgoing correspondence.", 400,
            )
        if not _can(user, "update", project):
            raise MailboxImportError("correspondence_permission_denied", "You may not modify correspondences in this project.", 403)
        # MAIL-1.3D-SENT-ITEMS-ATTACH: no check of the validation lock here. The lock protects the APPROVED
        # content of an OUT (its text, recipients, attachments to send); the files kept by this import are
        # the source message and its attachments, recorded as provenance (MailboxImportedFile) and never
        # part of what is sent -- see CorrespondenceAttachment exclusion in services/correspondence_send.py.
        # The right to modify the correspondence is still required, and nothing of the OUT itself is saved.

    # 4. a retry of a finished import answers from what is stored -- no Graph call needed
    if mode != MODE_COMPLETE and existing_row is not None and existing_row.imported_correspondence_id:
        return _existing(existing_row, project)

    # 5. what a completion still needs, so nothing already kept is fetched again
    skip_ids, seen_hashes, need_eml, need_text = set(), set(), True, mode != MODE_ATTACH
    if mode == MODE_COMPLETE:
        kept = MailboxImportedFile.objects.filter(mailbox_message=existing_row)
        skip_ids = set(kept.filter(kind=MailboxImportedFile.Kind.ATTACHMENT).values_list("graph_attachment_id", flat=True))
        need_eml = not kept.filter(kind=MailboxImportedFile.Kind.ORIGINAL_EML).exists()
        seen_hashes = set(MailboxImportedFile.objects.filter(correspondence=target).values_list("sha256", flat=True))
        need_text = not (target.message or "").strip()
    if mode == MODE_ATTACH:
        need_text = not (target.message or "").strip()
    if mode == MODE_COMPLETE and not need_eml and not need_text and not attachment_references:
        return ImportResult("already_complete", target, existing_row, _documents_summary(existing_row, attachments_stored=0, skipped=[]))

    # 6. read the message and fetch the files: one message, bounded, BEFORE any database write
    with ExitStack() as stack:
        try:
            reader = reader or MailboxGraphReader(mailbox, actor=user, project=project)
            content = reader.get_message_content(ref["graph_id"], folder)
        except MailboxReadError as exc:
            if exc.code == "not_found":
                raise MailboxImportError("message_gone", "This message no longer exists in that folder of the mailbox.", 404)
            raise
        meta = content.meta
        text, _, _ = truncate_utf8(content.body_text, limits.MAX_BODY_BYTES)
        text = text.strip()

        budget = limits.MAX_TOTAL_BYTES
        prepared = []
        if need_eml:
            eml = _prepare_eml(reader, ref, meta, budget, stack)
            prepared.append(eml)
            budget -= eml.size
            seen_hashes.add(eml.sha256)
        try:
            files, skipped, budget = _prepare_attachments(
                reader, ref, mailbox, user, attachment_references, skip_ids, seen_hashes, budget, stack,
            )
        except MailboxReadError as exc:
            if exc.code == "not_found":
                raise MailboxImportError("message_gone", "This message no longer exists in that folder of the mailbox.", 404)
            raise
        prepared += files

        # 7. all-or-nothing: the row only becomes `imported` once everything else succeeded
        stored, text_written = [], False
        try:
            with transaction.atomic():
                _lock_import(project, meta)
                row, _ = store_message_metadata(mailbox, meta)
                row = MailboxMessage.objects.select_for_update().get(pk=row.pk)
                if mode != MODE_COMPLETE:
                    if row.imported_correspondence_id:                  # lost a race with an identical import
                        row = MailboxMessage.objects.select_related("imported_correspondence").get(pk=row.pk)
                        return _existing(row, project)
                    if row.internet_message_id:
                        twin = MailboxMessage.objects.filter(
                            company_profile_id=company_id, internet_message_id=row.internet_message_id,
                            imported_correspondence__project=project,
                        ).exclude(pk=row.pk).first()
                        if twin is not None:
                            raise MailboxImportError(
                                "already_imported", "This message has already been imported into this project.", 409,
                                {"correspondence_id": twin.imported_correspondence_id},
                            )
                if mode == MODE_CREATE:
                    correspondence, outcome = _create_in(request, project, meta, fields, text), "created"
                    text_written = bool(text)
                else:
                    correspondence = Correspondence.objects.select_for_update().get(pk=target.pk)
                    if mode == MODE_ATTACH:
                        if correspondence.imported_from_messages.exists():
                            raise MailboxImportError(
                                "target_already_linked", "This correspondence already has a source message.", 409,
                            )
                        outcome = "attached"
                    else:
                        outcome = "completed"
                    if (
                        text and correspondence.direction == Correspondence.Direction.IN
                        and not (correspondence.message or "").strip()           # never overwrite existing text
                    ):
                        correspondence.message = text
                        correspondence.save(update_fields=["message", "updated_at"])
                        text_written = True
                if mode == MODE_COMPLETE:                                       # what a concurrent completion just added
                    kept_now = MailboxImportedFile.objects.filter(mailbox_message=row)
                    present = set(kept_now.values_list("graph_attachment_id", flat=True))
                    has_eml = kept_now.filter(kind=MailboxImportedFile.Kind.ORIGINAL_EML).exists()
                    prepared = [p for p in prepared if not (
                        (p.kind == MailboxImportedFile.Kind.ORIGINAL_EML and has_eml)
                        or (p.kind == MailboxImportedFile.Kind.ATTACHMENT and p.graph_attachment_id in present)
                    )]
                else:
                    MailboxMessage.objects.filter(pk=row.pk).update(
                        state=MailboxMessage.ProcessingState.IMPORTED, imported_correspondence=correspondence,
                        imported_by=user, imported_at=timezone.now(), updated_at=timezone.now(),
                    )
                for item in prepared:
                    _store_file(correspondence, row, user, item, stored)
                row.refresh_from_db()
        except IntegrityError as exc:
            _cleanup(stored)
            if "mailbox_imported_file" in str(exc):
                raise MailboxImportError("duplicate_file", "A file of this message is already kept in this correspondence.", 409)
            raise MailboxImportError("target_already_linked", "This correspondence already has a source message.", 409)
        except BaseException:
            _cleanup(stored)
            raise

    documents = {
        "text_stored": text_written,
        "eml_stored": any(p.kind == MailboxImportedFile.Kind.ORIGINAL_EML for p in prepared) or not need_eml,
        "attachments_stored": sum(p.kind == MailboxImportedFile.Kind.ATTACHMENT for p in prepared),
        "skipped": skipped,
    }
    logger.info(
        "Mailbox import mailbox_id=%s user_id=%s mode=%s outcome=%s files=%s",
        mailbox.pk, user.pk, mode, outcome, len(prepared),
    )
    return ImportResult(outcome, correspondence, row, documents)
