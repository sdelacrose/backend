"""GED-WF.4.3.1: client returns received through an incoming (IN) Transmittal.

A parallel path to the supplier receipt (which stays untouched): a line states "this client answered about THIS
ClientIssue" and is kept with its own result, comments and files. The IN confirmation (see
services.transmittal_receipt.confirm_incoming_receipt) records the reception and, for a user who may manage client
surveillances, turns each line into an immutable ClientSurveillanceReturn in the SAME transaction; otherwise the line
stays "to qualify" and can be qualified later, once, by such a user. Nothing here creates a document, a revision or an
item, changes a status or opens a surveillance, and no SEND_SUCCEEDED is ever invented: a distribution without any
recorded real send needs an explicit, written justification that is kept on the line and in the history.
"""
import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from project_management.models import (
    ClientIssue, ClientSurveillance, Transmittal, TransmittalClientReturnLine, TransmittalClientReturnLineFile,
    TransmittalItem,
)
from project_management.services.client_distribution_history import _source_revision
from project_management.services.client_surveillance_opening import can_process_client_return
from project_management.services.client_surveillance_return import (
    COMMENT_REQUIRED, Category, _discard_created_files, _store_blob, _text, detect_file_type, issue_requires_category,
    issue_send_events, prepare_files, record_client_return,
)
from project_management.services.transmittal_send_proof import _limit

logger = logging.getLogger(__name__)

Kind = TransmittalClientReturnLineFile.Kind
MAX_RESULTS = 50


def _document(issue):
    return issue.document_revision.document


def _project_id(issue):
    return _document(issue).folder.project_id


def line_state(line):
    if line.client_return_id:
        return "QUALIFIED"
    return "TO_QUALIFY" if line.transmittal.status == Transmittal.Status.ISSUED else "DRAFT"


def _check_issue(transmittal, issue):
    from project_management.services.document_withdrawal import is_withdrawn

    if issue is None:
        raise ValidationError({"client_issue": "Choose a client distribution."})
    if _project_id(issue) != transmittal.project_id:
        raise ValidationError({"client_issue": "This distribution belongs to another project."})
    if issue.client_company_id != transmittal.external_company_id:
        raise ValidationError({"client_issue": "This distribution was sent to another client company."})
    if is_withdrawn(_document(issue)):
        raise ValidationError({"client_issue": "This document has been withdrawn."})


def find_client_return_matches(transmittal, accessible_documents, reference="", revision="", out_reference=""):
    """Candidates for a received return: the ClientIssues of this project and client company whose document the user
    can read. Several are returned as such -- nothing is chosen here. Every given criterion must match."""
    issues = ClientIssue.objects.filter(
        client_company_id=transmittal.external_company_id,
        document_revision__document__folder__project_id=transmittal.project_id,
        document_revision__document__withdrawn_at__isnull=True,
        document_revision__document_id__in=accessible_documents.order_by().values("pk"),
    ).select_related("document_revision__document", "client_company").order_by("-pk")
    wanted = match_criteria(reference, revision, out_reference)
    already = set(transmittal.client_return_lines.values_list("client_issue_id", flat=True))
    results = []
    for issue in issues:
        row = match_row(issue, wanted, already)
        if row is None:
            continue
        results.append(row)
        if len(results) >= MAX_RESULTS:
            break
    return results


def match_criteria(reference="", revision="", out_reference=""):
    from project_management.services.transmittal_receipt import normalize_reference

    return {"reference": normalize_reference(reference), "revision": normalize_reference(revision), "out": normalize_reference(out_reference)}


def match_row(issue, wanted, already=frozenset()):
    """The candidate row of one ClientIssue when it matches every given criterion (None otherwise). Shared by the
    incoming-transmittal matching and the proof-centric search (SURVEILLANCE IN LOT B): same normalisation, same fields."""
    from project_management.services.transmittal_receipt import normalize_reference

    document, revision_row = _document(issue), issue.document_revision
    source_revision = _source_revision(revision_row)
    sends = issue_send_events(issue)
    refs = [normalize_reference(v) for v in (document.identification_number, document.full_document_number, document.file_name) if v]
    if wanted["reference"] and not any(wanted["reference"] in r for r in refs):
        return None
    if wanted["revision"] and wanted["revision"] not in (normalize_reference(source_revision), normalize_reference(issue.client_revision_label)):
        return None
    if wanted["out"] and not any(wanted["out"] in normalize_reference(e.transmittal.transmittal_number) for e in sends):
        return None
    purposes = {i.transmittal_id: i.distribution_purpose for i in TransmittalItem.objects.filter(client_issue=issue)}
    surveillance = ClientSurveillance.objects.filter(client_issue=issue).first()
    return {
        "client_issue_id": issue.pk, "document_id": document.pk,
        "reference": document.full_document_number or document.identification_number or document.file_name,
        "file_name": document.file_name, "document_origin": document.document_origin,
        "source_revision": source_revision, "client_revision": issue.client_revision_label,
        "client_company_name": issue.client_company.company_name,
        "surveillance_status": surveillance.status if surveillance else None,
        "requires_category": issue_requires_category(issue), "already_on_transmittal": issue.pk in already,
        "sends": [{"id": e.pk, "transmittal_id": e.transmittal_id, "transmittal_reference": e.transmittal.transmittal_number,
                   "sent_at": e.created_at, "distribution_purpose": purposes.get(e.transmittal_id)} for e in sends],
    }


def _validate_received_at(received_at, send_event):
    if received_at is None or timezone.is_naive(received_at):
        raise ValidationError({"received_at": "A timezone-aware date and time is required."})
    if received_at > timezone.now():
        raise ValidationError({"received_at": "The return date cannot be in the future."})
    if send_event is not None and received_at < send_event.created_at:
        raise ValidationError({"received_at": "The return cannot be earlier than the send it answers."})


def add_client_return_line(transmittal, actor, *, client_issue_id, category, received_at, source_channel, comment="",
                           client_reference="", client_revision="", send_event_id=None, no_send_justification="",
                           uploads=(), kinds=(), can_view_document=None):
    """Prepare a client return on an incoming draft. Creates NOTHING canonical: only the line and its pending files."""
    category = category or None
    if category is not None and category not in Category.values:
        raise ValidationError({"category": "Unknown result category."})
    if source_channel not in TransmittalClientReturnLine.Channel.values:
        raise ValidationError({"source_channel": "Choose the reception channel."})
    comment = _text(comment, "comment", 10000)
    client_reference = _text(client_reference, "client_reference", 255)
    client_revision = _text(client_revision, "client_revision", 100)
    no_send_justification = _text(no_send_justification, "no_send_justification", 2000)
    if category in COMMENT_REQUIRED and not comment:
        raise ValidationError({"comment": "Comments are required for this result."})
    prepared = prepare_files(uploads, kinds)

    written = []
    try:
        with transaction.atomic():
            locked = Transmittal.objects.select_for_update().get(pk=transmittal.pk)
            if locked.direction != Transmittal.Direction.IN or locked.status != Transmittal.Status.DRAFT:
                raise ValidationError("Client returns are only prepared on an incoming draft transmittal.")
            if not locked.external_company_id:
                raise ValidationError({"external_company": "The issuing company is required before preparing a return."})
            issue = ClientIssue.objects.select_related(
                "document_revision__document__folder", "client_company").filter(pk=client_issue_id).first()
            _check_issue(locked, issue)
            if can_view_document is not None and not can_view_document(_document(issue)):
                raise PermissionDenied("You cannot view this document.")
            if locked.client_return_lines.filter(client_issue=issue).exists():
                raise ValidationError({"client_issue": "This distribution already has a return on this transmittal."})
            if prepared and not locked.project.company_profile_id:
                raise ValidationError({"files": "Files can only be kept for a company project."})
            if category is None and not comment and not prepared:
                raise ValidationError({"comment": "Record a result, a comment or a file."})

            sends = issue_send_events(issue)
            send_event = None
            if send_event_id:
                send_event = next((e for e in sends if e.pk == int(send_event_id)), None)
                if send_event is None:
                    raise ValidationError({"send_event": "This send does not belong to this client distribution."})
            elif len(sends) == 1:
                send_event = sends[0]
            elif len(sends) > 1:
                raise ValidationError({"send_event": "Several sends exist: choose the one this return answers."})
            elif not no_send_justification:
                # D4: never an invented send. The absence of a recorded real send must be confirmed in writing.
                raise ValidationError({"no_send_justification": "No successful send is recorded for this distribution: confirm it and explain why."})
            _validate_received_at(received_at, send_event)

            line = TransmittalClientReturnLine.objects.create(
                transmittal=locked, client_issue=issue, send_event=send_event, category=category, received_at=received_at,
                source_channel=source_channel, comment=comment, client_reference=client_reference,
                client_revision=client_revision, no_send_justification="" if send_event else no_send_justification,
                created_by=actor,
            )
            for item in prepared:
                row = TransmittalClientReturnLineFile(
                    line=line, file_name=item["name"], content_type=item["content_type"], file_size=item["size"],
                    sha256=item["sha256"], kind=item["kind"])
                row.file.save(item["name"], ContentFile(item["content"]), save=False)
                written.append((row.file.storage, row.file.name))
                row.save()
            return line
    except BaseException:
        for storage, name in written:
            try:
                storage.delete(name)
            except Exception:  # traced; the file stays under the dedicated pending prefix
                logger.exception("Pending client return file written by a failed attempt could not be removed: %s", name)
        raise


@transaction.atomic
def remove_client_return_line(line):
    """Remove a line of an incoming DRAFT with its pending files (deleted from storage once the removal commits)."""
    locked = TransmittalClientReturnLine.objects.select_for_update().select_related("transmittal").get(pk=line.pk)
    if locked.transmittal.status != Transmittal.Status.DRAFT or locked.client_return_id:
        raise ValidationError("Only a line of an incoming draft can be removed.")
    for row in list(locked.files.all()):
        row.delete()
    locked.delete()


def delete_draft_client_lines(transmittal):
    """Deleting an incoming draft removes its prepared client lines first (their relation is PROTECT)."""
    with transaction.atomic():
        for line in list(transmittal.client_return_lines.select_related("transmittal")):
            remove_client_return_line(line)


# --------------------------------------------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------------------------------------------

def client_line_anomalies(transmittal, actor=None):
    """Blocking anomalies of the client lines of an IN draft. When `actor` can manage the surveillance of the line's
    document, the return will be created now, so its own rules (result, comments) are checked too."""
    anomalies = []
    for line in transmittal.client_return_lines.select_related("client_issue__document_revision__document__folder", "send_event"):
        issue, document = line.client_issue, line.client_issue.document_revision.document

        def add(code, message, _line=line):
            anomalies.append({"code": code, "message": message, "line_id": _line.pk})

        try:
            _check_issue(transmittal, issue)
        except ValidationError as error:
            add("client_line_withdrawn" if "withdrawn" in " ".join(error.messages) else "client_line_issue_invalid", " ".join(error.messages))
            continue
        sends = issue_send_events(issue)
        if line.send_event_id and line.send_event_id not in {e.pk for e in sends}:
            add("client_line_send_invalid", "The send this return answers is no longer valid.")
        elif not line.send_event_id and sends:
            add("client_line_send_required", "A send is now recorded for this distribution: choose the one this return answers.")
        elif not line.send_event_id and not (line.no_send_justification or "").strip():
            add("client_line_no_send_unconfirmed", "No successful send is recorded: confirm it and explain why.")
        try:
            _validate_received_at(line.received_at, line.send_event)
        except ValidationError as error:
            add("client_line_date_invalid", " ".join(error.messages))
        for row in line.files.all():
            if row.blob_id is None and not (row.file and row.file.storage.exists(row.file.name)):
                add("client_line_file_missing", f"{row.file_name}: the prepared file is no longer available.")
        if actor is not None and can_process_client_return(actor, document):
            if not line.category and issue_requires_category(issue):
                add("client_line_category_required", "The result is required for a distribution for review.")
            if line.category in COMMENT_REQUIRED and not (line.comment or "").strip():
                add("client_line_comment_required", "Comments are required for this result.")
    return anomalies


def lock_client_issues(lines):
    """Lock the ClientIssues of the lines in ASCENDING id order: two confirmations touching the same issues can
    never wait for each other in opposite orders."""
    ids = sorted({line.client_issue_id for line in lines})
    if ids:
        list(ClientIssue.objects.select_for_update(of=("self",)).filter(pk__in=ids).order_by("pk"))


def client_lines_metadata(lines, actor):
    """Trace kept on the immutable RECEIVED event."""
    return {"client_return_lines": [
        {"line": line.pk, "client_issue": line.client_issue_id, "send_event": line.send_event_id,
         "no_send_proof": line.send_event_id is None, "category": line.category,
         "qualified_now": can_process_client_return(actor, line.client_issue.document_revision.document)}
        for line in lines]}


def _keep_file(row, company_id, created_files):
    """Turn the pending copy into an EvidenceBlob (deduplicated per company and SHA-256) and clear the pending file."""
    import hashlib

    with row.file.open("rb") as handle:
        content = handle.read(_limit() + 1)
    if len(content) > _limit() or hashlib.sha256(content).hexdigest() != row.sha256 or len(content) != row.file_size or detect_file_type(content) is None:
        raise ValidationError({"files": f"{row.file_name}: the prepared file is no longer the file that was received."})
    blob = _store_blob(company_id, {"name": row.file_name, "content": content, "sha256": row.sha256, "size": row.file_size,
                                    "content_type": row.content_type}, created_files)
    storage, name = row.file.storage, row.file.name
    row.blob = blob
    row.file = None
    row.save()
    # the pending copy is removed only once the whole confirmation is committed: a rollback keeps it for the retry
    transaction.on_commit(lambda: _safe_delete(storage, name))


def _safe_delete(storage, name):
    try:
        storage.delete(name)
    except Exception:  # pragma: no cover - traced; the cleanup command can retry
        logger.exception("Pending client return file could not be deleted: %s", name)


def _blob_files(line):
    return [{"blob": row.blob, "name": row.file_name, "content_type": row.content_type, "size": row.file_size,
             "sha256": row.sha256, "kind": row.kind} for row in line.files.select_related("blob")]


def _create_return(line, actor, created_files, *, category, comment, qualified_later):
    issue = line.client_issue
    record = record_client_return(
        issue, actor, category=category, received_at=line.received_at, source_channel=line.source_channel,
        comment=comment, source_reference=line.client_reference, send_event_id=line.send_event_id,
        in_transmittal_id=line.transmittal_id, blob_files=_blob_files(line), created_files=created_files,
        auto_retain=True, extra_event_data={"in_line": line.pk, "in_transmittal": line.transmittal_id,
                          "no_send_proof": line.send_event_id is None,
                          "no_send_justification": line.no_send_justification, "client_revision": line.client_revision,
                          "qualified_later": qualified_later},
    )
    line.client_return = record
    line.save(update_fields=["client_return"])
    return record


def confirm_client_lines(transmittal, actor, created_files):
    """Called by the IN confirmation AFTER the reception is recorded (status ISSUED + RECEIVED), inside its
    transaction: keep every file as an EvidenceBlob, then create the returns for the lines the actor may qualify.
    Lines the actor cannot qualify keep their data and files and stay "to qualify". Returns (qualified, pending)."""
    lines = list(transmittal.client_return_lines.select_related(
        "client_issue__document_revision__document__folder__project", "client_issue__client_company").order_by("client_issue_id", "id"))
    qualified = pending = 0
    for line in lines:
        company_id = line.client_issue.document_revision.document.folder.project.company_profile_id
        for row in list(line.files.filter(blob__isnull=True)):
            _keep_file(row, company_id, created_files)
        if can_process_client_return(actor, line.client_issue.document_revision.document):
            _create_return(line, actor, created_files, category=line.category, comment=line.comment, qualified_later=False)
            qualified += 1
        else:
            pending += 1
    return qualified, pending


def qualify_client_return_line(line_id, actor, *, category=None, comment=None):
    """Later qualification of a received line (the caller has checked documents.update and the
    access to the document): creates the line's single initial return, with the result completed or confirmed."""
    created_files = []
    try:
        with transaction.atomic():
            line = TransmittalClientReturnLine.objects.select_for_update(of=("self",)).select_related(
                "transmittal", "client_issue__document_revision__document__folder__project", "client_issue__client_company").get(pk=line_id)
            if line.transmittal.direction != Transmittal.Direction.IN or line.transmittal.status != Transmittal.Status.ISSUED:
                raise ValidationError("The incoming transmittal is not confirmed yet.")
            if line.client_return_id:
                raise ValidationError("This client return line has already been qualified.")
            _check_issue(line.transmittal, line.client_issue)
            final_category = category or line.category
            final_comment = line.comment if comment is None else _text(comment, "comment", 10000)
            if final_category is not None and final_category not in Category.values:
                raise ValidationError({"category": "Unknown result category."})
            if not final_category and issue_requires_category(line.client_issue):
                raise ValidationError({"category": "Choose the result: it is required for a distribution for review."})
            if final_category in COMMENT_REQUIRED and not final_comment.strip():
                raise ValidationError({"comment": "Comments are required for this result."})
            if not line.files.exists() and not final_category and not final_comment.strip():
                raise ValidationError({"comment": "Record a result, a comment or a file."})
            if line.files.filter(blob__isnull=True).exists():
                raise ValidationError({"files": "The received files are not kept yet."})
            return _create_return(line, actor, created_files, category=final_category, comment=final_comment, qualified_later=True)
    except BaseException:
        _discard_created_files(created_files)
        raise
