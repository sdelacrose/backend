"""GED-WF.4.3: recording of the client returns (A/B/C/D result, comments, annotated files).

Only facts are recorded here. Nothing changes a document, a revision, a status or the due date: the
documentary consequences of a category belong to GED-WF.5. A return is anchored on the ClientIssue and is
immutable; a mistake is corrected by a new return that replaces (`supersedes`) the previous one, once.

Files: PDF, DOCX, XLSX, PNG, JPEG, checked on their real content (not on the declared extension/MIME), at
most the size already applied to the send proofs. Bytes are kept as EvidenceBlob (one per company and
SHA-256, never rewritten); a return only references them. The return, its files and its history event
are written in ONE transaction; a file written for an attempt that fails is removed again, but only when
no row references it (a shared blob is never touched).
"""
import hashlib
import io
import logging
import posixpath
import zipfile

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn,
    ClientSurveillanceReturnFile, EvidenceBlob, Transmittal, TransmittalEvent, TransmittalItem,
)
from project_management.services.client_surveillance_code import list_effective_codes
from project_management.services.transmittal_send_proof import _intact, _limit

logger = logging.getLogger(__name__)

Category = ClientSurveillanceReturn.Category
EVENT = ClientSurveillanceEvent.EventType
COMMENT_REQUIRED = (Category.ACCEPTED_WITH_COMMENTS, Category.COMMENTED, Category.REJECTED)
MAX_FILES = 20
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# detected real type -> (accepted extensions, canonical content type stored)
ALLOWED_TYPES = {
    "pdf": ({".pdf"}, "application/pdf"),
    "png": ({".png"}, "image/png"),
    "jpeg": ({".jpg", ".jpeg"}, "image/jpeg"),
    "docx": ({".docx"}, DOCX_MIME),
    "xlsx": ({".xlsx"}, XLSX_MIME),
}


def detect_file_type(content):
    """Real type from the leading bytes (and the package layout for Office files); None if unknown."""
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith(b"PK\x03\x04"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(content)).namelist())
        except (zipfile.BadZipFile, OSError, ValueError):
            return None
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return "docx"
        if "[Content_Types].xml" in names and "xl/workbook.xml" in names:
            return "xlsx"
    return None


def prepare_files(uploads, kinds=None):
    """Validate every upload BEFORE anything is stored: all or nothing. Returns the prepared files."""
    uploads = list(uploads or [])
    kinds = list(kinds or [])
    if len(uploads) > MAX_FILES:
        raise ValidationError({"files": f"At most {MAX_FILES} files per return."})
    prepared, seen = [], set()
    for index, upload in enumerate(uploads):
        name = posixpath.basename((getattr(upload, "name", "") or "").replace("\\", "/")).strip()
        if not name:
            raise ValidationError({"files": "A file name is required."})
        content = upload.read(_limit() + 1)
        if len(content) > _limit():
            raise ValidationError({"files": f"{name}: the file exceeds the maximum size."})
        if not content:
            raise ValidationError({"files": f"{name}: the file is empty."})
        real = detect_file_type(content)
        extensions, content_type = ALLOWED_TYPES.get(real, (set(), ""))
        extension = posixpath.splitext(name)[1].lower()
        if real is None or extension not in extensions:
            raise ValidationError({"files": f"{name}: only PDF, DOCX, XLSX, PNG and JPEG files are accepted."})
        digest = hashlib.sha256(content).hexdigest()
        if digest in seen:
            raise ValidationError({"files": f"{name}: the same file is listed twice."})
        seen.add(digest)
        kind = (kinds[index] if index < len(kinds) else "") or ClientSurveillanceReturnFile.Kind.ANNOTATED
        if kind not in ClientSurveillanceReturnFile.Kind.values:
            raise ValidationError({"files": f"{name}: unknown file kind."})
        prepared.append({"name": name[:500], "content": content, "sha256": digest, "size": len(content),
                         "content_type": content_type, "kind": kind})
    return prepared


def _store_blob(company_id, item, created_files):
    """Reuse the EvidenceBlob of this company and hash, or create it (same mechanism as the send proofs).
    A newly written file is remembered in `created_files` so a failed attempt can remove it again."""
    for _ in range(3):
        blob = EvidenceBlob.objects.filter(company_profile_id=company_id, sha256=item["sha256"]).first()
        if blob:
            if not _intact(blob):
                raise ValidationError({"files": f"{item['name']}: the stored copy of this file is corrupted."})
            return blob
        candidate = EvidenceBlob(company_profile_id=company_id, sha256=item["sha256"], file_size=item["size"],
                                 content_type=item["content_type"])
        candidate.file.save(item["sha256"], ContentFile(item["content"]), save=False)
        created_files.append((candidate.file.storage, candidate.file.name))
        try:
            with transaction.atomic():
                candidate.save()
            return candidate
        except IntegrityError:
            continue  # another transaction stored the same bytes meanwhile: reuse its blob
    raise ValidationError({"files": f"{item['name']}: the file could not be stored."})


def _discard_created_files(created_files):
    """After a rollback: remove the files this attempt wrote, unless some row references them."""
    for storage, name in created_files:
        try:
            if not EvidenceBlob.objects.filter(file=name).exists():
                storage.delete(name)
        except Exception:  # traced; the rollback itself must not fail
            logger.exception("File written by a failed client return could not be removed: %s", name)


def issue_send_events(issue):
    """The really successful OUT sends that carried this exact ClientIssue, oldest first."""
    return list(
        TransmittalEvent.objects.filter(
            event_type=TransmittalEvent.EventType.SEND_SUCCEEDED, transmittal__direction=Transmittal.Direction.OUT,
            transmittal__items__client_issue_id=issue.pk,
        ).select_related("transmittal").distinct().order_by("created_at", "pk")
    )


def issue_requires_category(issue):
    """A FOR_REVIEW distribution (or an existing surveillance) always expects a formal result."""
    return (
        ClientSurveillance.objects.filter(client_issue=issue).exists()
        or TransmittalItem.objects.filter(
            client_issue=issue, distribution_purpose=TransmittalItem.DistributionPurpose.FOR_REVIEW,
            transmittal__status=Transmittal.Status.ISSUED,
        ).exists()
    )


def eligible_in_transmittals(issue, project_id):
    """Confirmed (received) incoming transmittals of the same project and client company."""
    return Transmittal.objects.filter(
        project_id=project_id, direction=Transmittal.Direction.IN, status=Transmittal.Status.ISSUED,
        external_company_id=issue.client_company_id, received_at__isnull=False,
    ).order_by("-received_at", "-pk")


def incoming_transmittal_candidates(project_id):
    """Visible candidates are project-scoped; the view additionally enforces object permissions."""
    return Transmittal.objects.filter(project_id=project_id, direction=Transmittal.Direction.IN).select_related('external_company').order_by('-pk')


def incoming_transmittal_candidate(transmittal, client_company_id):
    same_client = transmittal.external_company_id == client_company_id
    received = transmittal.status == Transmittal.Status.ISSUED and transmittal.received_at is not None
    return {'id': transmittal.pk, 'reference': transmittal.transmittal_number, 'subject': transmittal.subject,
            'received_at': transmittal.received_at, 'status': transmittal.status,
            'client_company_name': transmittal.external_company.company_name if transmittal.external_company else None,
            'same_client': same_client, 'eligible': same_client and received,
            'blocked_reason': 'WRONG_CLIENT' if not same_client else 'RECEIPT_REQUIRED' if not received else None}


def _snapshot(project, category):
    if not category:
        return {"code_snapshot": "", "label_en_snapshot": None, "label_fr_snapshot": None, "label_de_snapshot": None}
    row = next(r for r in list_effective_codes(project) if r["category"] == category)
    return {"code_snapshot": row["code"], "label_en_snapshot": row["label_en"],
            "label_fr_snapshot": row["label_fr"], "label_de_snapshot": row["label_de"]}


def _text(value, field, maximum, required=False):
    value = (value or "").strip()
    if required and not value:
        raise ValidationError({field: "This field is required."})
    if len(value) > maximum:
        raise ValidationError({field: f"At most {maximum} characters."})
    return value


def record_client_return(issue, actor, *, category, received_at, source_channel, comment="", source_reference="",
                         send_event_id=None, in_transmittal_id=None, supersedes_id=None, correction_reason="",
                         source_correspondence_id=None, can_view_correspondence=None,
                         uploads=(), kinds=(), can_view_transmittal=None, created_files=None, blob_files=(),
                         extra_event_data=None, confirm_other_company=False, confirm_other_type=False, auto_retain=False):
    """Record one client return (new, or correcting a previous one). The caller has already checked that the
    user can manage this client surveillance and can read the document. Returns the created return.

    GED-WF.4.3.1: `created_files` is an optional list SHARED by a caller that records several returns in one
    transaction (the IN confirmation) and removes the files of a failed attempt itself; `blob_files` are files
    already kept as EvidenceBlob (a received IN line) that the return only references; `extra_event_data` is
    added to the history event."""
    category = category or None
    if category is not None and category not in Category.values:
        raise ValidationError({"category": "Unknown result category."})
    if source_channel not in ClientSurveillanceReturn.Channel.values:
        raise ValidationError({"source_channel": "Choose the reception channel."})
    if received_at is None or timezone.is_naive(received_at):
        raise ValidationError({"received_at": "A timezone-aware date and time is required."})
    if received_at > timezone.now():
        raise ValidationError({"received_at": "The return date cannot be in the future."})
    comment = _text(comment, "comment", 10000)
    source_reference = _text(source_reference, "source_reference", 255)
    correction_reason = _text(correction_reason, "correction_reason", 2000, required=bool(supersedes_id))
    if category in COMMENT_REQUIRED and not comment:
        raise ValidationError({"comment": "Comments are required for this result."})
    prepared = prepare_files(uploads, kinds)

    own_cleanup = created_files is None
    created_files = [] if own_cleanup else created_files
    try:
        with transaction.atomic():
            issue = ClientIssue.objects.select_for_update(of=("self",)).select_related(
                "document_revision__document__folder__project", "client_company").get(pk=issue.pk)
            project = issue.document_revision.document.folder.project
            company_id = project.company_profile_id
            if (prepared or blob_files) and not company_id:
                raise ValidationError({"files": "Files can only be kept for a company project."})
            surveillance = ClientSurveillance.objects.select_for_update().filter(client_issue=issue).first()

            if category is None:
                if issue_requires_category(issue):
                    raise ValidationError({"category": "The result is required for a distribution for review."})
                if not comment and not prepared and not blob_files:
                    raise ValidationError({"comment": "Record a comment or a file when no result was given."})

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
            if send_event is not None and received_at < send_event.created_at:
                raise ValidationError({"received_at": "The return cannot be earlier than the send it answers."})

            in_transmittal = None
            if in_transmittal_id:
                in_transmittal = Transmittal.objects.filter(pk=in_transmittal_id).first()
                if in_transmittal is None or not eligible_in_transmittals(issue, project.pk).filter(pk=in_transmittal.pk).exists():
                    raise ValidationError({"in_transmittal": "Choose a confirmed incoming transmittal of this project and client company."})
                if can_view_transmittal is not None and not can_view_transmittal(in_transmittal):
                    raise PermissionDenied("You cannot view this incoming transmittal.")

            source_correspondence = None
            if source_correspondence_id:
                source_correspondence = eligible_surveillance_correspondences(issue).select_for_update().filter(pk=source_correspondence_id).first()
                if source_correspondence is None:
                    raise ValidationError({"source_correspondence": "Choose an issued IN correspondence of this project.", "code": "unknown_proof"})
                if can_view_correspondence is not None and not can_view_correspondence(source_correspondence):
                    raise PermissionDenied("You cannot view this surveillance correspondence.")
                other_company = source_correspondence.sender_company_id != issue.client_company_id
                other_type = (source_correspondence.correspondence_type or "").lower() != PROOF_TYPE
                if other_company and not confirm_other_company:
                    raise ValidationError({"source_correspondence": "The sender company of this correspondence differs from the client company of the document: confirm explicitly.", "code": "other_company_confirmation_required"})
                if other_type and not confirm_other_type:
                    raise ValidationError({"source_correspondence": "This is not a Surveillance correspondence: confirm explicitly that it is the proof of this return.", "code": "other_type_confirmation_required"})
                extra_event_data = {**(extra_event_data or {}), "proof_correspondence": source_correspondence.pk, "proof_tier": source_correspondence.proof_tier,
                                    "proof_sender_company": source_correspondence.sender_company_id, "proof_client_company": issue.client_company_id,
                                    "proof_other_company_confirmed": bool(other_company and confirm_other_company),
                                    "proof_other_type_confirmed": bool(other_type and confirm_other_type)}
                if not supersedes_id and ClientSurveillanceReturn.objects.filter(client_issue=issue, source_correspondence=source_correspondence, supersedes__isnull=True).exists():
                    raise ValidationError({"source_correspondence": "This correspondence is already associated with this client revision. Correct the existing return instead."})
                source_reference = source_correspondence.display_reference or source_correspondence.reference or str(source_correspondence.pk)

            superseded = None
            if supersedes_id:
                superseded = ClientSurveillanceReturn.objects.select_for_update().filter(pk=supersedes_id, client_issue=issue).first()
                if superseded is None:
                    raise ValidationError({"supersedes": "The corrected return belongs to another client distribution."})
                if superseded.corrections.exists():
                    raise ValidationError({"supersedes": "This return has already been corrected."})

            if superseded is not None and source_correspondence is not None and superseded.source_correspondence_id != source_correspondence.pk:
                if ClientSurveillanceReturn.objects.filter(client_issue=issue, source_correspondence=source_correspondence, supersedes__isnull=True).exists():
                    raise ValidationError({"source_correspondence": "This correspondence already has a return for this revision. Correct that return instead."})

            if superseded is not None and source_correspondence is None:
                source_correspondence = superseded.source_correspondence
                if source_correspondence is not None:
                    source_reference = superseded.source_reference

            record = ClientSurveillanceReturn.objects.create(
                client_issue=issue, send_event=send_event, category=category, received_at=received_at, comment=comment,
                source_reference=source_reference, source_channel=source_channel, in_transmittal=in_transmittal, source_correspondence=source_correspondence,
                supersedes=superseded, correction_reason=correction_reason if superseded else "", recorded_by=actor,
                **_snapshot(project, category),
            )
            for item in prepared:
                blob = _store_blob(company_id, item, created_files)
                ClientSurveillanceReturnFile.objects.create(
                    client_return=record, blob=blob, file_name=item["name"], content_type=item["content_type"],
                    file_size=item["size"], sha256=item["sha256"], kind=item["kind"],
                )
            for item in blob_files:
                ClientSurveillanceReturnFile.objects.create(
                    client_return=record, blob=item["blob"], file_name=item["name"], content_type=item["content_type"],
                    file_size=item["size"], sha256=item["sha256"], kind=item["kind"],
                )
            if surveillance is None and category and issue_requires_category(issue):
                surveillance = ClientSurveillance.objects.create(client_issue=issue, created_by=actor, status=ClientSurveillance.Status.UNDER_REVIEW)
                ClientSurveillanceEvent.objects.create(surveillance=surveillance, actor=actor, status=surveillance.status,
                    event_type=EVENT.OPENED, data={"automatic": True, "reason": "client_return_received", "return": record.pk})
            if surveillance is not None:
                previous_status = surveillance.status
                late = surveillance.status == ClientSurveillance.Status.CLOSED
                if not late and category and surveillance.status in (ClientSurveillance.Status.DRAFT, ClientSurveillance.Status.UNDER_REVIEW):
                    surveillance.status = ClientSurveillance.Status.READY_FOR_CLOSURE
                    surveillance.save(update_fields=["status", "updated_at"])

                event_type = EVENT.RETURN_LATE if late else (EVENT.RETURN_CORRECTED if superseded else EVENT.RETURN_RECORDED)
                ClientSurveillanceEvent.objects.create(
                    surveillance=surveillance, actor=actor, status=surveillance.status, result=surveillance.result,
                    event_type=event_type,
                    data={"return": record.pk, "category": category, "code": record.code_snapshot,
                          "supersedes": superseded.pk if superseded else None,
                          "send_event": send_event.pk if send_event else None, "late": late, "automatic": True, "previous_status": previous_status, **(extra_event_data or {})},
                )
            # LOT 1: the return opens its automated follow-up (a failure is logged and recovered, never raised)
            from project_management.services.client_surveillance_followup import register_return_safely
            register_return_safely(record, actor, superseded)
            # LOT C: a unique, unambiguous categorised return is retained in the same operation (outcome kept on the record)
            if auto_retain:
                from project_management.services.client_surveillance_followup import auto_retain_safely
                record.retention = auto_retain_safely(record, actor)
            return record
    except BaseException:
        if own_cleanup:
            _discard_created_files(created_files)
        raise


def validate_retained_return(surveillance, return_id):
    """The return the closure keeps: explicit, of this ClientIssue, categorised, not replaced by a
    correction, with the comments its category requires."""
    if not return_id:
        raise ValidationError({"retained_return": "Choose the client return retained for the closure."})
    record = ClientSurveillanceReturn.objects.select_for_update().filter(
        pk=return_id, client_issue_id=surveillance.client_issue_id).first()
    if record is None:
        raise ValidationError({"retained_return": "This return does not belong to this client surveillance."})
    if not record.category:
        raise ValidationError({"retained_return": "The retained return must carry a result."})
    if record.corrections.exists():
        raise ValidationError({"retained_return": "This return has been replaced by a correction."})
    if record.category in COMMENT_REQUIRED and not (record.comment or "").strip():
        raise ValidationError({"retained_return": "Comments are required for this result."})
    return record


def retained_return_id(surveillance):
    """The return kept at closure (from the immutable closing event), None before closure / for legacy rows."""
    for event in reversed(list(surveillance.events.all())):
        if event.status == ClientSurveillance.Status.CLOSED and event.event_type == EVENT.STATUS and event.data:
            return event.data.get("retained_return")
    return None


PROOF_TYPE = 'surveillance'


def eligible_surveillance_correspondences(issue):
    """SURVEILLANCE IN LOT A: the source-proof candidates of a client distribution. Project, IN direction and issued
    status stay hard limits; the sender company and the correspondence type are NO LONGER silent exclusions -- they rank
    the candidates (tier 1: a Surveillance IN of the client company; tier 2: a Surveillance IN of another company;
    tier 3: another IN correspondence) and are confirmed explicitly when the proof is chosen (see record_client_return).
    A proof already linked to ANOTHER document stays selectable; only the same return is protected (view/constraint)."""
    from django.db.models import Case, IntegerField, Q, Value, When
    from project_management.models import Correspondence
    is_type = Q(correspondence_type__iexact=PROOF_TYPE)
    return Correspondence.objects.filter(
        project_id=issue.document_revision.document.folder.project_id, direction='IN', status='ISSUED',
    ).annotate(proof_tier=Case(
        When(is_type & Q(sender_company_id=issue.client_company_id), then=Value(1)),
        When(is_type, then=Value(2)),
        default=Value(3), output_field=IntegerField(),
    )).order_by('proof_tier', '-received_date', '-pk')
