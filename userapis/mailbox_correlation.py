"""MAIL-1.3C: recognising, with certainty, which LINKS correspondence a mail message belongs to.

Two parts, both built on what already exists (the Graph send path, the read client and the
MailboxMessage metadata table) -- no new workflow, no new table:

1. SENT SIDE. Microsoft Graph's sendMail answers 202 with no message id, so LINKS cannot learn the
   Internet id of what it sent from the send itself. The existing send adds ONE custom header,
   `x-links-send`, holding an unforgeable marker (event id + HMAC with the server secret). Later,
   the copy saved in Sent Items is found by that marker (`identify_sent_message`) and its
   identifiers (Graph id, internetMessageId, conversationId) are kept as a MailboxMessage row
   linked to the OUT correspondence.

2. REPLY SIDE. `correlate_reply` decides whether an incoming message answers one of those sent
   copies, with three outcomes:
     MATCH_CONFIRMED  -- unambiguous proof (the reply's In-Reply-To / References contain the
                         Internet id of exactly one LINKS-sent message, or a unique conversation
                         hit corroborated by the LINKS reference);
     MATCH_AMBIGUOUS  -- evidence exists but is insufficient or points to several correspondences
                         (e.g. a conversation hit alone, a reference alone);
     NO_MATCH         -- nothing links it to a LINKS send.
   A similar subject is never evidence. A conversationId is only a hint, never proof alone.

Nothing here creates a correspondence, schedules anything or reads on its own: the caller names the
acting user (re-authorized for READ on the mailbox) and supplies the message's identifiers.
Candidates are limited to the SAME mailbox, the SAME company and -- for a project correspondence --
a mailbox still assigned to that project; another company's correspondences are never candidates.
"""

import hashlib
import hmac
from datetime import timedelta
from dataclasses import dataclass, field

from django.conf import settings

from userapis.mailbox_access import is_mailbox_assigned_to_project
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import MailboxGraphReader, authorize_mailbox_read
from userapis.models import MailboxMessage

SEND_MARKER_HEADER = "x-links-send"

MATCH_CONFIRMED = "MATCH_CONFIRMED"
MATCH_AMBIGUOUS = "MATCH_AMBIGUOUS"
NO_MATCH = "NO_MATCH"
# The message is one this mailbox SENT (Sent Items): the correlation is for INCOMING messages only,
# so an outgoing message is never matched, whatever its headers or conversation say.
NOT_APPLICABLE_OUTGOING_REPLY = "NOT_APPLICABLE_OUTGOING_REPLY"
# Folders that hold messages the mailbox sent rather than received.
OUTGOING_FOLDERS = ("sentitems",)


# --- sent side: the marker ---------------------------------------------------------------------

def _signature(event_id, correspondence_id):
    key = settings.SECRET_KEY.encode("utf-8")
    message = f"links-send:{int(event_id)}:{int(correspondence_id)}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()[:20]


def build_send_marker(event_id, correspondence_id):
    """`<event id>.<hmac>`: tells which send it is, and cannot be forged without the server
    secret. It reveals nothing about the correspondence (no reference, no subject)."""
    return f"{int(event_id)}.{_signature(event_id, correspondence_id)}"


def verify_send_marker(marker, event):
    """True only when `marker` is exactly the marker LINKS built for this send event."""
    if not marker or event is None:
        return False
    return hmac.compare_digest(
        str(marker), build_send_marker(event.pk, event.correspondence_id)
    )


def record_sent_copy(event, meta, ident):
    """Keep the identifiers of the Sent Items copy of a SUCCESSFUL Graph send, linked to its OUT
    correspondence. `meta` is the GraphMessageMeta of that copy and `ident` its
    ReplyIdentification (headers). Refused unless the marker proves it is THIS send. Idempotent;
    never overwrites a link to a different correspondence. Returns the MailboxMessage or None."""
    if event.result != "SUCCESS" or event.provider != "graph" or event.mailbox_id is None:
        return None
    if not verify_send_marker(ident.send_marker, event):
        return None
    row, _ = store_message_metadata(event.mailbox, meta)
    if not row.internet_message_id and ident.internet_message_id:
        row.internet_message_id = ident.internet_message_id
        row.save(update_fields=["internet_message_id", "updated_at"])
    if row.correspondence_id is None:
        row.correspondence_id = event.correspondence_id
        row.save(update_fields=["correspondence", "updated_at"])
    elif row.correspondence_id != event.correspondence_id:
        return None
    _link_proof(event, row)
    return row


def _link_proof(event, row):
    """MAIL-1.4-A: tie the Sent Items copy to the proof of THIS send event (the marker already
    proved it is this event's copy, never merely a similar subject or recipient). Creates the
    PENDING proof when needed; never overwrites a copy already linked, and never fails the
    identification itself."""
    from project_management.services.correspondence_send_proof import ensure_send_proof_safely

    proof = ensure_send_proof_safely(event)
    if proof is not None and proof.mailbox_message_id is None:
        proof.mailbox_message = row
        proof.save(update_fields=["mailbox_message", "updated_at"])


def identify_sent_message(event, *, actor, reader=None, limit=25):
    """Find the Sent Items copy of a successful Graph send (read-only) and record its ids.
    Candidates are narrowed by the exact subject and recipient before any header is fetched, and
    CONFIRMED only by the marker. Returns the MailboxMessage, or None if it is not there (yet) --
    never an error the send could suffer from, and never a guess. Nothing calls this
    automatically in this lot."""
    if event.result != "SUCCESS" or event.provider != "graph" or event.mailbox_id is None:
        return None
    # MAIL-1.4-A: this send's copy may already be known (the proof of THIS event points to it):
    # nothing is asked of Graph again. Another send of the same correspondence has its own proof.
    from project_management.models import CorrespondenceSendProof

    authorize_mailbox_read(actor, event.mailbox)
    known = (
        CorrespondenceSendProof.objects.filter(event=event, mailbox_message__isnull=False)
        .select_related("mailbox_message").first()
    )
    if known is not None:
        return known.mailbox_message
    reader = reader or MailboxGraphReader(event.mailbox, actor=actor)
    recipients = {
        str((r or {}).get("email") or "").strip().lower() for r in (event.to_recipients or [])
    }
    # A short window around this one send (the copy's date can precede the recorded sent_at) and
    # the exact subject, both applied by Graph itself: nothing else in Sent Items is listed.
    sent_moment = event.sent_at or event.created_at
    page = reader.list_messages(
        "sentitems",
        top=min(limit, 10),
        received_since=event.created_at - timedelta(minutes=1),
        received_until=sent_moment + timedelta(minutes=5),
        subject=event.subject,
    )
    for meta in page.messages[:limit]:
        if meta.subject != event.subject:
            continue
        if recipients and not recipients & set(meta.to_addresses):
            continue
        ident = reader.get_reply_identification(meta.graph_id)
        row = record_sent_copy(event, meta, ident)
        if row is not None:
            return row
    return None


# --- reply side ------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReplyCorrelation:
    result: str
    correspondence_id: int = None
    candidate_ids: tuple = ()
    evidence: tuple = field(default_factory=tuple)


def _company_of(correspondence):
    if correspondence.company_profile_id:
        return correspondence.company_profile_id
    if correspondence.project_id:
        return correspondence.project.company_profile_id
    return None


def _in_scope(correspondence, mailbox):
    """A sent copy only counts for a correspondence of the mailbox's OWN company that is an OUT
    and -- when it belongs to a project -- whose project the mailbox is still assigned to."""
    if correspondence.direction != "OUT":
        return False
    if _company_of(correspondence) != mailbox.company_profile_id:
        return False
    if correspondence.project_id and not is_mailbox_assigned_to_project(
        mailbox, correspondence.project
    ):
        return False
    return True


def _ids(values):
    return {str(v).strip() for v in values if v and str(v).strip()}


def correlate_reply(
    mailbox, *, actor, in_reply_to="", references=(), conversation_id="",
    links_reference="", message_internet_id="", message_folder="",
):
    """Does an incoming message of `mailbox` answer a correspondence LINKS sent from it? See the
    module docstring for the three outcomes. `actor` must be allowed to READ the mailbox
    (otherwise MailboxReadError, before anything is looked up).

    `message_folder` is the folder the message was read from. A message found in Sent Items is the
    mailbox's own OUTGOING message, never an incoming reply: it returns
    NOT_APPLICABLE_OUTGOING_REPLY without looking at any evidence. Callers should always pass it."""
    authorize_mailbox_read(actor, mailbox)
    if str(message_folder or "").strip().lower() in OUTGOING_FOLDERS:
        return ReplyCorrelation(NOT_APPLICABLE_OUTGOING_REPLY)

    sent = MailboxMessage.objects.filter(
        mailbox=mailbox,
        company_profile_id=mailbox.company_profile_id,
        # Only the copies of messages LINKS SENT are candidates: a stored reply that has itself
        # been linked to a correspondence (folder inbox) is never taken for a sent message.
        folder__in=OUTGOING_FOLDERS,
        correspondence__isnull=False,
        correspondence__direction="OUT",
    ).select_related("correspondence", "correspondence__project")

    def scoped(rows):
        return {r.correspondence_id: r.correspondence for r in rows if _in_scope(r.correspondence, mailbox)}

    own_id = str(message_internet_id or "").strip()

    # 1. Strong proof: the reply names the Internet id of a message LINKS sent.
    strong_ids = _ids([in_reply_to, *references]) - {own_id}
    if strong_ids:
        strong_rows = list(sent.filter(internet_message_id__in=strong_ids))
        strong = scoped(strong_rows)
        if len(strong) == 1:
            (correspondence_id,) = strong
            matched_ids = {r.internet_message_id for r in strong_rows}
            how = "in_reply_to" if str(in_reply_to or "").strip() in matched_ids else "references"
            return ReplyCorrelation(MATCH_CONFIRMED, correspondence_id, (correspondence_id,), (how,))
        if len(strong) > 1:
            return ReplyCorrelation(MATCH_AMBIGUOUS, None, tuple(sorted(strong)), ("references",))

    # 2. Weak evidence only: a conversation hint and/or an explicit LINKS reference.
    by_conversation = {}
    if conversation_id:
        by_conversation = scoped(sent.filter(conversation_id=str(conversation_id)))
    by_reference = {}
    reference = str(links_reference or "").strip()
    if reference:
        from project_management.models import Correspondence

        for candidate in Correspondence.objects.filter(
            reference__iexact=reference, direction="OUT"
        ).select_related("project"):
            if _in_scope(candidate, mailbox):
                by_reference[candidate.pk] = candidate

    if len(by_conversation) == 1 and set(by_conversation) == set(by_reference):
        (correspondence_id,) = by_conversation
        return ReplyCorrelation(
            MATCH_CONFIRMED, correspondence_id, (correspondence_id,),
            ("conversation", "links_reference"),
        )
    candidates = sorted(set(by_conversation) | set(by_reference))
    if candidates:
        evidence = tuple(
            name for name, found in (("conversation", by_conversation), ("links_reference", by_reference)) if found
        )
        return ReplyCorrelation(MATCH_AMBIGUOUS, None, tuple(candidates), evidence)
    return ReplyCorrelation(NO_MATCH)
