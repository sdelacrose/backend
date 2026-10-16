"""LOT 3D-3D2: POST /correspondence/<id>/send and its read-only history.

Section 14: SEND is its own dedicated documents.correspondence_send permission, not
documents.correspondence_update -- an external action with real business consequences (an
actual email leaves the system) is a different authorization decision than editing a draft
record, even though today the same users likely hold both. Every lifecycle/eligibility guard in
services.correspondence_send still applies underneath regardless of permission level (section 3
"All enforced backend-side"), exactly like the validation workflow's own actor check applies
even to a full_access user (LOT 3D-3D1 section 13) -- holding the permission only means the
request is allowed to ASK; ready_to_send/idempotency/recipient/attachment-ownership decide
whether it actually happens.
"""

import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import FileResponse, Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.reverse import reverse

from project_management.models import CorrespondenceSendProof, CorrespondenceSendProofFile
from project_management.permissions import CorrespondencePermission
from project_management.services.correspondence_send import send_correspondence
from project_management.services.correspondence_send_proof_archive import (
    REDUCED_VERIFICATION,
    Fact,
    _hash_stored,
    match_attachments,
)
from project_management.services.correspondence_send_proof_runner import (
    SendProofRetryRefused,
    archiving_enabled,
    check_send_proof_retry,
)
from project_management.services.correspondence_workflow import can_resend, ready_to_send
from userapis.mailbox_import_files import FileRefused, clean_file_name
from project_management.views.correspondence import _user_reference, correspondence_queryset_for_user


def serialize_send_event(event):
    return {
        "id": event.id,
        "correspondence": event.correspondence_id,
        "sender": _user_reference(event.sender),
        "from_email": event.from_email,
        # BCC is included here exactly as everywhere else send evidence is read: this endpoint
        # is already gated by documents.correspondence_send/_view on THIS Correspondence's own
        # scope (section 16 "Do not expose BCC recipients incorrectly... to unauthorized
        # users") -- there is no broader/public read path for this data.
        "to_recipients": event.to_recipients,
        "cc_recipients": event.cc_recipients,
        "bcc_recipients": event.bcc_recipients,
        "subject": event.subject,
        "body": event.body,
        "attachments_snapshot": event.attachments_snapshot,
        "provider": event.provider,
        # MAIL-1.2A section 8: "Boîte expéditrice" in the audit trail -- null for provider="smtp",
        # the MailboxConnection id/address actually used for a provider="graph" send.
        "mailbox": event.mailbox_id,
        "mailbox_email": event.mailbox.email_address if event.mailbox_id else None,
        "provider_message_id": event.provider_message_id,
        "result": event.result,
        "error_message": event.error_message,
        "sent_at": event.sent_at,
        "created_at": event.created_at,
    }


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


class CorrespondenceSendViewSet(viewsets.GenericViewSet):
    filter_backends = []

    def _correspondence(self, request, correspondence_id, action):
        correspondence = correspondence_queryset_for_user(request.user).filter(
            pk=correspondence_id
        ).first()
        if correspondence is None:
            raise Http404
        if not CorrespondencePermission(action).has_object_permission(
            request, self, correspondence
        ):
            raise PermissionDenied()
        return correspondence

    def _has(self, request, correspondence, action):
        return CorrespondencePermission(action).has_object_permission(request, self, correspondence)

    def _event_with_proof(self, request, correspondence, event, can_send):
        data = serialize_send_event(event)
        data["bcc_hidden"] = not can_send
        if not can_send:
            data["bcc_recipients"] = []
        data["proof"] = serialize_send_proof(
            event, getattr(event, "proof", None), request, correspondence,
            can_send=can_send, can_retry=can_send and archiving_enabled(),
        )
        return data

    def _proof_file(self, correspondence, event_id, file_id):
        proof_file = (
            CorrespondenceSendProofFile.objects.select_related("blob", "proof", "proof__event")
            .filter(pk=file_id, proof__event_id=event_id, proof__event__correspondence=correspondence)
            .first()
        )
        if proof_file is None:
            raise Http404
        return proof_file

    def proof_file_download(self, request, correspondence_id=None, event_id=None, file_id=None, *args, **kwargs):
        # The right to see the correspondence (its project and company included) is checked first;
        # the .eml -- the whole message, Bcc included -- further needs the right to send on it.
        correspondence = self._correspondence(request, correspondence_id, "view")
        proof_file = self._proof_file(correspondence, event_id, file_id)
        event = proof_file.proof.event
        if (
            proof_file.kind == CorrespondenceSendProofFile.Kind.EML
            and not self._has(request, correspondence, "send")
        ):
            _audit("proof_file_download", request.user, correspondence, event, proof_file, "denied")
            raise PermissionDenied()
        if not _blob_is_readable_and_intact(proof_file, correspondence):
            _audit("proof_file_download", request.user, correspondence, event, proof_file, "unavailable")
            return Response(
                {"detail": "The archived file is not available.", "code": "file_unavailable"},
                status=status.HTTP_409_CONFLICT,
            )
        response = FileResponse(
            proof_file.blob.file.open("rb"), as_attachment=True, filename=safe_download_name(proof_file),
            content_type="application/octet-stream",     # never rendered or executed by a browser
        )
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response["Cache-Control"] = "private, no-store"
        _audit("proof_file_download", request.user, correspondence, event, proof_file, "granted")
        return response

    def proof_retry(self, request, correspondence_id=None, event_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "send")
        event = correspondence.send_events.filter(pk=event_id).first()
        proof = CorrespondenceSendProof.objects.filter(event=event).first() if event is not None else None
        if proof is None:
            raise Http404
        try:
            check_send_proof_retry(proof.pk, requested_by=request.user)
        except SendProofRetryRefused as refused:
            _audit("proof_retry", request.user, correspondence, event, None, refused.code)
            http = {"not_authorized": status.HTTP_403_FORBIDDEN}.get(refused.code, status.HTTP_409_CONFLICT)
            return Response({"detail": "The archiving cannot be run again.", "code": refused.code}, status=http)
        # The archiving reads Graph and may take a while: it is queued, never run inside the request.
        # Nothing is run here, and a queue that cannot be reached is reported, not worked around.
        try:
            from project_management.tasks import retry_send_proof_archive_task

            retry_send_proof_archive_task.apply_async(args=[proof.pk, request.user.pk], retry=False)
        except Exception:  # noqa: BLE001
            _audit("proof_retry", request.user, correspondence, event, None, "queue_unavailable")
            return Response(
                {"detail": "The archiving could not be queued.", "code": "queue_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        _audit("proof_retry", request.user, correspondence, event, None, "queued")
        return Response({"queued": True}, status=status.HTTP_202_ACCEPTED)

    def retrieve(self, request, correspondence_id=None, *args, **kwargs):
        # History is read-only Correspondence data: gated by the existing view permission, not
        # the new send one (matches the validation workflow endpoint's own retrieve/act split).
        correspondence = self._correspondence(request, correspondence_id, "view")
        events = correspondence.send_events.select_related("sender", "mailbox", "proof").prefetch_related("proof__files").all()
        # MAIL-1.4-D: Bcc and the .eml are for people who may SEND on this correspondence; the right to
        # see it is not enough. Every field the history had before is still there.
        can_send = self._has(request, correspondence, "send")
        return Response(
            {
                "events": [self._event_with_proof(request, correspondence, e, can_send) for e in events],
                "ready_to_send": ready_to_send(correspondence),
                # CORR-DIST-2 section 6/16: a second, independent eligibility flag -- a Resend
                # action, never conflated with the FIRST-send one above (see can_resend's own
                # docstring for why they can never both be true for the same Correspondence).
                "can_resend": can_resend(correspondence),
            }
        )

    def send(self, request, correspondence_id=None, *args, **kwargs):
        correspondence = self._correspondence(request, correspondence_id, "send")
        data = request.data
        # Section 17: only genuinely email-composer values are ever read from the request --
        # status/sent_at/provider result/parent response/ready_to_send are exclusively backend-
        # owned consequences, never accepted here even if a client sent them.
        try:
            event = send_correspondence(
                correspondence,
                sender=request.user,
                cc=data.get("cc"),
                bcc=data.get("bcc"),
                subject=data.get("subject"),
                body=data.get("body"),
                attachment_ids=data.get("attachment_ids"),
                # MAIL-1.2A section 4: optional -- omitted entirely reproduces the existing
                # SMTP-only behavior exactly (send_correspondence's own default).
                mailbox_id=data.get("mailbox_id"),
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        response_status = (
            status.HTTP_201_CREATED if event.result == "SUCCESS" else status.HTTP_400_BAD_REQUEST
        )
        return Response(serialize_send_event(event), status=response_status)


# --- MAIL-1.4-D: the proof of a send ---------------------------------------------------------------

audit_logger = logging.getLogger("project_management.send_proof_audit")

# What a person is told about a proof that is not complete: a coarse category, never the technical code
# (some of which say too much about security or storage).
_TEMPORARY_CODES = frozenset({
    "temporary", "rate_limited", "token_refresh_failed", "invalid_response", "token_unavailable",
    "token_rejected", "mailbox_inactive", "unexpected_error",
})
_PUBLIC_ISSUES = {
    "copy_not_found": "waiting_for_copy", "copy_gone": "waiting_for_copy",
    "attachment_missing": "attachment_missing", "attachments_truncated": "attachment_missing",
    "attachment_hash_mismatch": "attachment_differs", "attachment_ambiguous": "attachment_differs",
    "unexpected_attachment": "attachment_differs",
    "attachment_not_allowed": "attachment_refused", "file_too_large": "attachment_refused",
}
_RETRYABLE_STATES = (
    CorrespondenceSendProof.State.FAILED, CorrespondenceSendProof.State.PARTIAL,
    CorrespondenceSendProof.State.UNAVAILABLE, CorrespondenceSendProof.State.PENDING,
)


def _audit(action, user, correspondence, event, proof_file=None, outcome=""):
    """One line per download or retry: who, what, which file, the outcome (the date is the log's own).
    Identifiers only -- never a name, a content, a Bcc, a token or a marker."""
    audit_logger.info(
        "send_proof_audit action=%s outcome=%s user=%s correspondence=%s event=%s file=%s",
        action, outcome, getattr(user, "pk", None), correspondence.pk, getattr(event, "pk", None),
        getattr(proof_file, "pk", None),
    )


def proof_verification_level(proof):
    """full: an archived proof whose files were matched by SHA-256 (or only the .eml was expected);
    reduced: an older send matched by name and size only; none: not archived (yet)."""
    if proof.state != CorrespondenceSendProof.State.ARCHIVED:
        return "none"
    return "reduced" if proof.error_code == REDUCED_VERIFICATION else "full"


def proof_issue(proof):
    if proof.state == CorrespondenceSendProof.State.ARCHIVED:
        return None
    if proof.state == CorrespondenceSendProof.State.UNAVAILABLE:
        return "copy_unavailable"
    code = proof.error_code
    if not code:
        return None
    if code in _PUBLIC_ISSUES:
        return _PUBLIC_ISSUES[code]
    return "temporary_problem" if code in _TEMPORARY_CODES else "needs_attention"


def safe_download_name(proof_file):
    """A file name that cannot name a path or hide anything: the stored name made safe, or a neutral
    one when it cannot be (the .eml keeps its extension)."""
    try:
        return clean_file_name(proof_file.file_name, fallback=f"evidence-{proof_file.pk}")
    except FileRefused:
        extension = ".eml" if proof_file.kind == CorrespondenceSendProofFile.Kind.EML else ""
        return f"evidence-{proof_file.pk}{extension}"


def _blob_is_readable_and_intact(proof_file, correspondence):
    """The file exists, belongs to this correspondence's company and still hashes to what was kept --
    read in chunks, so even a large file never sits in memory."""
    blob = proof_file.blob
    company = correspondence.company_profile_id or (
        correspondence.project.company_profile_id if correspondence.project_id else None
    )
    if company is None or blob.company_profile_id != company:
        return False
    if blob.sha256 != proof_file.sha256 or blob.file_size != proof_file.file_size:
        return False
    return _hash_stored(blob) == (proof_file.file_size, proof_file.sha256)


def serialize_send_proof(event, proof, request, correspondence, *, can_send, can_retry):
    """The proof of one send for a reader: its state, what was expected and kept, and the non-sensitive
    metadata of each kept file. Never a path, a Graph id, a marker, a technical code, a Bcc or a body."""
    if proof is None:
        return None
    files = list(proof.files.all())
    expected = list(event.attachments_snapshot or [])
    facts = [
        Fact(f.graph_attachment_id, f.file_name, f.file_size, f.sha256, True)
        for f in files if f.kind == CorrespondenceSendProofFile.Kind.ATTACHMENT
    ]
    _pairs, missing, _unmatched, _problems, _reduced = match_attachments(expected, facts)
    serialized = []
    for f in files:
        # the .eml holds the whole message, Bcc included: seeing the correspondence is not enough
        allowed = can_send if f.kind == CorrespondenceSendProofFile.Kind.EML else True
        serialized.append({
            "id": f.id,
            "kind": f.kind,
            "file_name": safe_download_name(f),
            "content_type": f.content_type,
            "file_size": f.file_size,
            "sha256": f.sha256,
            "archived_at": f.created_at,
            "can_download": allowed,
            "download_url": reverse(
                "correspondence-send-proof-file-download",
                kwargs={"correspondence_id": correspondence.id, "event_id": event.id, "file_id": f.id},
                request=request,
            ) if allowed else None,
        })
    return {
        "state": proof.state,
        "verification_level": proof_verification_level(proof),
        "issue": proof_issue(proof),
        "archived_at": proof.archived_at,
        "attempts": proof.attempts,
        "next_attempt_at": proof.next_attempt_at,
        "files_archived": len(files),
        "expected_attachments": [{"file_name": e.get("file_name"), "file_size": e.get("file_size")} for e in expected],
        "missing_attachments": [{"file_name": e.get("file_name"), "file_size": e.get("file_size")} for e in missing],
        "files": serialized,
        "can_retry": bool(can_retry and proof.state in _RETRYABLE_STATES),
    }
