"""LOT 3D-3D2: the SEND step for an approved OUT Correspondence.

Architecture (section 2/18):

    send_correspondence()  (this module -- the ONE Correspondence-lifecycle boundary)
        |
        v
    _deliver_via_provider()  (this module -- the ONLY place that touches an email provider)
        |
        v
    Django EmailMultiAlternatives -> settings.EMAIL_BACKEND (SMTP/SES today)

Nothing outside this module ever imports django.core.mail directly for a Correspondence send;
a future Microsoft Graph adapter replaces _deliver_via_provider alone (see the LOT 3D-3D2
report's Microsoft 365 section) without touching send_correspondence's lifecycle sequencing,
locking, evidence, or idempotency guard.

Section 9's transaction-boundary rule is the shape of every function here: the external
network call in _deliver_via_provider is NEVER made inside a database transaction. Two SHORT
transactions bracket it instead -- one to atomically claim the "in-flight" slot (the
IntegrityError from CorrespondenceSendEvent's own UniqueConstraint IS the idempotency guard,
section 15), one to record the outcome and, only on success, apply the Correspondence-side
consequences (ISSUED, apply_response_link).
"""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.utils import timezone

from project_management.models import Correspondence, CorrespondenceAttachment, CorrespondenceSendEvent
from project_management.services.correspondence_response import apply_response_link
from project_management.services.correspondence_workflow import can_resend, ready_to_send

Result = CorrespondenceSendEvent.Result

# Deliberately simple (RFC 5322 is far more permissive than any real mailbox needs) -- rejects
# obvious garbage without becoming a second email-validation framework.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendBlocked(ValidationError):
    """Raised for anything that prevents even ATTEMPTING a send (eligibility/security) -- never
    raised for a provider failure, which is instead recorded as a FAILED CorrespondenceSendEvent
    and reported back as data, not an exception (section 8/9)."""


def _valid_email(value):
    return bool(value) and bool(_EMAIL_RE.match(value.strip()))


def _clean_email_list(values, *, field):
    """CC/BCC: optional, but every entry given must be a real-looking address (section 16)."""
    cleaned = []
    for value in values or []:
        value = (value or "").strip()
        if not value:
            continue
        if not _valid_email(value):
            raise SendBlocked({field: f'"{value}" is not a valid email address.'})
        cleaned.append(value)
    return cleaned


def resolve_recipient(correspondence):
    """Section 4: the canonical TO address is recipient_contact.email -- never a guessed
    company-generic address. Returns {"email", "name"} or None when no usable address exists."""
    contact = correspondence.recipient_contact
    if contact is None or not _valid_email(contact.email):
        return None
    name = f"{contact.name} {contact.surname}".strip() or contact.email
    return {"email": contact.email, "name": name}


def _resolve_attachments(correspondence, attachment_ids):
    """Section 6/16: send exactly what was asked for, and only this Correspondence's own
    attachments -- an id belonging to another Correspondence is silently excluded, never
    guessed-at or cross-matched (never a KeyError/500, never a leak of another record's file)."""
    owned = CorrespondenceAttachment.objects.filter(correspondence=correspondence)
    if attachment_ids is None:
        # No explicit selection: send all of this Correspondence's existing attachments --
        # the simplest coherent default (section 6), never a silent partial send.
        return list(owned)
    wanted = {int(a) for a in attachment_ids}
    selected = list(owned.filter(pk__in=wanted))
    if len(selected) != len(wanted):
        raise SendBlocked(
            {"attachments": "One or more selected attachments do not belong to this correspondence."}
        )
    return selected


def _snapshot_attachments(attachments):
    return [
        {"id": a.id, "file_name": a.file_name, "file_size": a.file_size} for a in attachments
    ]


def validate_send_request(correspondence, *, cc=None, bcc=None, attachment_ids=None):
    """Section 3/16: every eligibility and security guard, all server-side, all BEFORE any
    CorrespondenceSendEvent row (let alone a provider call) is created. Returns
    (recipient, attachments) on success; raises SendBlocked otherwise.

    CORR-DIST-2 section 6: a Correspondence is eligible to attempt a send either as a FIRST
    send (ready_to_send -- DRAFT, approved, has subject/recipient) OR as a RESEND (can_resend --
    already has at least one prior SUCCESS, nothing currently in flight). Exactly one of the two
    can ever be true for a given Correspondence (a first success flips status to ISSUED, which
    alone already makes ready_to_send permanently False), so this is never an ambiguous OR."""
    if correspondence.direction != Correspondence.Direction.OUT:
        raise SendBlocked({"direction": "Only an outgoing correspondence can be sent."})
    if not ready_to_send(correspondence) and not can_resend(correspondence):
        raise SendBlocked(
            {"ready_to_send": "This correspondence is not ready to send: it must be approved and in draft for a first send, or already successfully sent at least once to be resent."}
        )
    recipient = resolve_recipient(correspondence)
    if recipient is None:
        raise SendBlocked(
            {"recipient": "The recipient contact has no usable email address. Set one on the Network contact before sending."}
        )
    cc_clean = _clean_email_list(cc, field="cc")
    bcc_clean = _clean_email_list(bcc, field="bcc")
    attachments = _resolve_attachments(correspondence, attachment_ids)
    return recipient, cc_clean, bcc_clean, attachments


def _deliver_via_provider(*, from_email, to, cc, bcc, subject, body, attachments):
    """The ONE function that touches an email provider (section 2/18) -- V1 is Django's
    configured EmailBackend (SMTP/SES in every real environment; the console backend in local
    dev, section 20/22 -- never a real send during local validation or automated tests, since
    Django's test runner itself swaps in the locmem backend automatically). Returns the
    provider message id when the backend supplies one (SMTP does not, section 2)."""
    message = EmailMultiAlternatives(
        subject=subject, body=body, from_email=from_email, to=[to], cc=cc or None, bcc=bcc or None,
    )
    for attachment in attachments:
        with attachment.file.open("rb") as handle:
            message.attach(attachment.file_name, handle.read())
    message.send(fail_silently=False)
    return ""  # SMTP gives no message id; kept as its own return so a Graph adapter can supply one.


@transaction.atomic
def _claim_send_slot(correspondence, *, sender, from_email, recipient, cc, bcc, subject, body, attachments):
    """Section 15: the ONLY database-transactional part before the provider call. Creating this
    row is the idempotency guard itself -- CorrespondenceSendEvent's own UniqueConstraint turns
    a concurrent duplicate attempt into an IntegrityError here, before any email is sent."""
    locked = Correspondence.objects.select_for_update().get(pk=correspondence.pk)
    # Re-checked INSIDE the lock: a second request that passed validate_send_request() a moment
    # earlier (before this lock was acquired) must not still slip through. CORR-DIST-2 section
    # 8/9: a prior SUCCESS no longer blocks this -- only a currently IN_PROGRESS attempt does
    # (resend is approved; concurrency protection is the only thing this guard is for now).
    if locked.send_events.filter(result=Result.IN_PROGRESS).exists():
        raise SendBlocked({"status": "A send is already in progress."})
    try:
        return CorrespondenceSendEvent.objects.create(
            correspondence=locked,
            sender=sender,
            from_email=from_email,
            to_recipients=[recipient],
            cc_recipients=[{"email": e} for e in cc],
            bcc_recipients=[{"email": e} for e in bcc],
            subject=subject,
            body=body,
            attachments_snapshot=_snapshot_attachments(attachments),
            result=Result.IN_PROGRESS,
        )
    except IntegrityError:
        raise SendBlocked({"status": "A send is already in progress or already completed."})


@transaction.atomic
def _finalize_success(event, *, provider_message_id):
    """Section 9 steps 1-4 (evidence, ISSUED, sent_at, parent update) and step 6/7 (no longer
    sendable, content stays locked) -- one short transaction, only entered AFTER the provider
    already confirmed success. Never touches Correspondence.issue_date (section 10).

    CORR-DIST-2 section 10/11: idempotent on status -- the FIRST success flips DRAFT->ISSUED;
    every later successful RESEND finds status already ISSUED and leaves it untouched (no
    redundant write/history row). apply_response_link is called unconditionally either way: it
    is already its own no-op once the parent is no longer in a PENDING response state, so a
    resend can never re-trigger or re-advance a response that a prior send already resolved."""
    event.result = Result.SUCCESS
    event.provider_message_id = provider_message_id or ""
    event.sent_at = timezone.now()
    event.save(update_fields=["result", "provider_message_id", "sent_at", "updated_at"])

    correspondence = event.correspondence
    if correspondence.status != Correspondence.Status.ISSUED:
        correspondence.status = Correspondence.Status.ISSUED
        correspondence.save(update_fields=["status", "updated_at"])
    # The exact existing rule (LOT 3B/3D-3C0C), never re-derived here: an ISSUED OUT child
    # answers its IN parent. A standalone OUT's own response_status (e.g. AWAITING_REPLY) was
    # already set at creation (initial_response_status) and needs no send-time change --
    # apply_response_link is a no-op whenever there is no parent to update.
    apply_response_link(correspondence)
    return event


@transaction.atomic
def _finalize_failure(event, *, error_message):
    """Section 8: the Correspondence itself is never touched on failure -- still DRAFT, parent
    still whatever it already was. The FAILED row is retained as its own permanent evidence
    (never deleted/overwritten), and is exempt from the "one in-flight" constraint so a retry
    can immediately create a fresh CorrespondenceSendEvent."""
    event.result = Result.FAILED
    event.error_message = error_message
    event.save(update_fields=["result", "error_message", "updated_at"])
    return event


def send_correspondence(correspondence, *, sender, cc=None, bcc=None, subject=None, body=None, attachment_ids=None):
    """The one Correspondence-lifecycle entry point for LOT 3D-3D2 (section 2). Subject/body
    default to the Correspondence's own subject/message but are NEVER written back to it
    (section 5) -- only the CorrespondenceSendEvent records what was actually sent."""
    recipient, cc_clean, bcc_clean, attachments = validate_send_request(
        correspondence, cc=cc, bcc=bcc, attachment_ids=attachment_ids,
    )
    from_email = settings.DEFAULT_FROM_EMAIL
    final_subject = subject if subject is not None else (correspondence.subject or "")
    final_body = body if body is not None else (correspondence.message or "")

    event = _claim_send_slot(
        correspondence, sender=sender, from_email=from_email, recipient=recipient,
        cc=cc_clean, bcc=bcc_clean, subject=final_subject, body=final_body, attachments=attachments,
    )
    try:
        provider_message_id = _deliver_via_provider(
            from_email=from_email, to=recipient["email"], cc=cc_clean, bcc=bcc_clean,
            subject=final_subject, body=final_body, attachments=attachments,
        )
    except Exception as error:  # noqa: BLE001 -- any provider failure is recorded, never masked
        return _finalize_failure(event, error_message=str(error))
    return _finalize_success(event, provider_message_id=provider_message_id)
