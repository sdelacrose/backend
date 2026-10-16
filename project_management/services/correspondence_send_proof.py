"""MAIL-1.4-A: preparing the proof of an OUT send (see models/correspondence_send_proof.py).

Only the idempotent creation of the PENDING proof of a confirmed Microsoft 365 send lives here: no
Graph call, no download, no stored file, no task, and no change to the correspondence. Keeping the
files is a later lot."""

import logging
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction

from project_management.models import CorrespondenceSendEvent, CorrespondenceSendProof

logger = logging.getLogger(__name__)


def ensure_send_proof(event):
    """The proof of `event`, created PENDING when it does not exist yet; None when this send has no
    proof to keep (not a successful Graph send: an SMTP send has no Sent Items copy, a failed or
    in-progress one was not confirmed). Safe to call any number of times, from any number of
    processes: a second call returns the same row."""
    if (
        event.result != CorrespondenceSendEvent.Result.SUCCESS
        or event.provider != "graph"
        or event.mailbox_id is None
    ):
        return None
    existing = CorrespondenceSendProof.objects.filter(event=event).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            return CorrespondenceSendProof.objects.create(event=event)
    except IntegrityError:                       # another process created it first
        return CorrespondenceSendProof.objects.filter(event=event).first()


def ensure_send_proof_safely(event):
    """`ensure_send_proof` for the send path: whatever goes wrong here is logged and swallowed, so a
    send that Microsoft already accepted is never turned into a failure by its proof."""
    try:
        # A savepoint of its own: even a database error here (a table not migrated yet, say) only
        # rolls back this step, never the transaction that records the successful send.
        with transaction.atomic():
            return ensure_send_proof(event)
    except Exception:  # noqa: BLE001 -- the send itself must stand regardless
        logger.warning("Could not prepare the send proof of event %s", event.pk, exc_info=True)
        return None


@dataclass
class MissingProofsReport:
    """What `ensure_missing_send_proofs` found, by event id only (never a subject or an address)."""
    created: list = field(default_factory=list)          # proofs created by this call (or, in a dry run, that would be)
    dry_run: bool = True
    # sends whose Sent Items copy cannot be tied to them by what is already known: the correspondence
    # was sent several times and only a single copy is recorded for it, so no recorded copy can be
    # assumed to be THIS send's. Only the HMAC marker found in Sent Items can settle it.
    uncertain_copy: list = field(default_factory=list)
    # sends of a correspondence with no Sent Items copy recorded at all
    no_recorded_copy: list = field(default_factory=list)
    examined: int = 0


def ensure_missing_send_proofs(*, limit, mailbox=None, dry_run=True):
    """Create the missing PENDING proof of every confirmed Microsoft 365 send recorded before MAIL-1.4-A,
    at most `limit` of them (idempotent: a send that has one is left alone, a second call creates
    nothing). DRY RUN BY DEFAULT: with `dry_run=True` nothing is written and the report lists what
    WOULD be created. Reads only LINKS rows: no Graph call, no download, no change to any send event,
    and no hash is ever guessed from a file that exists today. A proof created here is PENDING and is
    never archived by itself: the automatic archiving only covers sends made after its activation date
    (see correspondence_send_proof_runner). Nothing runs this by itself. Returns a MissingProofsReport."""
    from django.db.models import Count

    from userapis.mailbox_correlation import OUTGOING_FOLDERS
    from userapis.models import MailboxMessage

    events = CorrespondenceSendEvent.objects.filter(
        result=CorrespondenceSendEvent.Result.SUCCESS, provider="graph", mailbox__isnull=False,
        proof__isnull=True,
    ).order_by("id")
    if mailbox is not None:
        events = events.filter(mailbox=mailbox)
    events = events[: int(limit)]
    report = MissingProofsReport(dry_run=bool(dry_run))
    sends_by_correspondence = {
        row["correspondence"]: row["n"]
        for row in CorrespondenceSendEvent.objects.filter(
            result=CorrespondenceSendEvent.Result.SUCCESS, provider="graph",
        ).values("correspondence").annotate(n=Count("id"))
    }
    for event in events:
        report.examined += 1
        if not dry_run:
            ensure_send_proof(event)
        report.created.append(event.pk)
        copies = MailboxMessage.objects.filter(
            mailbox_id=event.mailbox_id, correspondence_id=event.correspondence_id,
            folder__in=OUTGOING_FOLDERS,
        ).count()
        if copies == 0:
            report.no_recorded_copy.append(event.pk)
        elif sends_by_correspondence.get(event.correspondence_id, 0) > copies:
            report.uncertain_copy.append(event.pk)
    return report


def schedule_send_proof_archive(proof):
    """Queue the first archiving attempt of `proof` ONLY once the transaction that records the send has
    committed (never before: an archiving must never run for a send that was rolled back), and without
    waiting for it. Does nothing when the proof does not exist or the automatic archiving is off."""
    if proof is None:
        return
    from project_management.services.correspondence_send_proof_runner import enqueue_send_proof_archive

    transaction.on_commit(lambda: enqueue_send_proof_archive(proof.pk))


def schedule_send_proof_archive_safely(proof):
    """`schedule_send_proof_archive` for the send path: it can never fail the send it follows."""
    try:
        schedule_send_proof_archive(proof)
    except Exception:  # noqa: BLE001
        logger.warning("Could not schedule the archiving of a send proof", exc_info=True)
