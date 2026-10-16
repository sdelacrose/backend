"""MAIL-1.4-C: running the archiving of send proofs on its own -- when, how often, as whom -- around the
MAIL-1.4-B service (`archive_send_proof`), which stays the only thing that reads a mailbox and keeps files.

Nothing here can send an e-mail, change a send event or the correspondence, or read a mailbox other
than the one a LINKS send went through, and only to find that send's own copy (HMAC marker). Nothing
runs unless it is switched on:

    MAIL_SEND_PROOF_ARCHIVING_ENABLED = False          (default: every entry point does nothing)
    MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE = <datetime>    (only sends made at or after this moment are
                                                       ever archived automatically; unset: nothing is)

so switching it on later archives what is sent from then on and never the older history, which stays
reachable only through the explicit manual retry.

THE SCHEDULE. A run that does not finish is retried by the periodic sweep, not by Celery retries:
after the n-th run the next one is due 1 min, 5 min, 15 min, 1 h, 6 h, then 24 h later (RETRY_DELAYS);
after the seventh run there is none. What is retried follows what failed:
  * copy not in Sent Items yet / gone      -> retried; exhausted: UNAVAILABLE
  * temporary Graph error, rate limit, token to renew, mailbox disconnected, unexpected/storage
    error                                  -> retried; exhausted: stays FAILED, explicitly
  * partial because something is missing or the list was cut -> retried; exhausted: stays PARTIAL
  * anything decided on purpose (not authorized, access denied, marker mismatch, corrupt blob,
    invalid or too large .eml, a refused/ambiguous/different attachment) -> NEVER retried by itself;
    only the manual retry, after someone changed something.
"""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import F, Q
from django.utils import timezone

from project_management.models import CorrespondenceSendEvent, CorrespondenceSendProof
from project_management.services.correspondence_send_proof_archive import (
    ArchiveResult,
    State,
    _try_lock,
    _unlock,
    archive_send_proof,
)
from userapis.mailbox_read import MailboxReadError, authorize_mailbox_read
from userapis.models import MailboxConnection, MailboxUserGrant

logger = logging.getLogger(__name__)

RETRY_DELAYS = (
    timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15),
    timedelta(hours=1), timedelta(hours=6), timedelta(hours=24),
)
# What a retry can change (technical or waiting conditions) ...
AUTO_RETRY_FAILURES = frozenset({
    "temporary", "rate_limited", "token_refresh_failed", "invalid_response",
    "token_unavailable", "token_rejected", "mailbox_inactive", "unexpected_error",
})
AUTO_RETRY_PARTIAL = frozenset({"attachment_missing", "attachments_truncated"})
# ... and the decisions only a person can reverse (never retried by the schedule).
PERMANENT_FAILURES = frozenset({
    "not_authorized", "access_denied", "identity_mismatch", "unsupported_provider", "marker_mismatch",
    "blob_corrupt", "invalid_eml", "eml_too_large", "request_rejected", "invalid_request",
})
SWEEP_LOCK_NAMESPACE = 13042
DEFAULT_SWEEP_BATCH = 20
DEFAULT_SWEEP_SECONDS = 240


class SendProofRetryRefused(Exception):
    """A manual retry that was not allowed; `code` says why (no detail about the mailbox or the mail)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- switches ----------------------------------------------------------------------------------

def archiving_enabled():
    """The master switch: off, nothing archives -- neither the schedule nor a manual retry."""
    return bool(getattr(settings, "MAIL_SEND_PROOF_ARCHIVING_ENABLED", False))


def auto_archive_since():
    """The moment from which sends are archived automatically, or None when it is not switched on."""
    if not archiving_enabled():
        return None
    return getattr(settings, "MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE", None)


def auto_archiving_applies(proof, now=None):
    since = auto_archive_since()
    sent = proof.event.sent_at or proof.event.created_at
    return since is not None and sent is not None and sent >= since


# --- schedule ----------------------------------------------------------------------------------

def retry_delay(attempts):
    """How long to wait after the `attempts`-th run, or None when the retries are used up."""
    index = int(attempts) - 1
    if 0 <= index < len(RETRY_DELAYS):
        return RETRY_DELAYS[index]
    return None


def _apply_schedule(proof, now):
    """Decide, from the state a run left, whether and when it is tried again. Writes only the columns
    of the schedule and, once retries are used up, UNAVAILABLE."""
    state, code, next_at = proof.state, proof.error_code, None
    delay = retry_delay(proof.attempts)
    if state == State.PENDING:
        if delay is None:
            state = State.UNAVAILABLE
        else:
            next_at = now + delay
    elif state == State.FAILED and code in AUTO_RETRY_FAILURES and delay is not None:
        next_at = now + delay
    elif state == State.PARTIAL and code in AUTO_RETRY_PARTIAL and delay is not None:
        next_at = now + delay
    proof.state, proof.next_attempt_at = state, next_at
    proof.save(update_fields=["state", "next_attempt_at", "updated_at"])


def _failure(proof, code, now):
    """A run that could not even start (no one allowed to read, mailbox not active): recorded the
    same way a service failure is, and scheduled by the same rules."""
    proof.attempts = min(proof.attempts + 1, 32767)
    proof.state, proof.error_code = State.FAILED, code
    proof.save(update_fields=["attempts", "state", "error_code", "updated_at"])
    _apply_schedule(proof, now)


# --- who reads --------------------------------------------------------------------------------

def resolve_archive_actor(event):
    """A user who is really allowed to READ the mailbox this send went through, or None. Never a
    privileged account: only people the mailbox's own rules already allow, in this order -- the
    person who sent (or the owner of a personal mailbox), the delegate whose Microsoft account the
    mailbox is read with, then anyone holding an explicit read grant. Re-checked with the same
    `authorize_mailbox_read` every read uses."""
    mailbox = event.mailbox
    if mailbox is None:
        return None
    if mailbox.connection_type == MailboxConnection.ConnectionType.INDIVIDUAL:
        candidates = [mailbox.owner_user]
    else:
        candidates = [event.sender, mailbox.connected_by]
        candidates += [
            grant.user for grant in
            MailboxUserGrant.objects.filter(mailbox=mailbox, can_read=True).select_related("user").order_by("id")
        ]
    seen = set()
    for user in candidates:
        if user is None or user.pk in seen or not getattr(user, "is_active", True):
            continue
        seen.add(user.pk)
        try:
            authorize_mailbox_read(user, mailbox)
        except MailboxReadError:
            continue
        return user
    return None


# --- one run -----------------------------------------------------------------------------------

def run_archive(proof_id, *, now=None, actor=None, reader=None):
    """One archiving attempt of a proof, with its schedule applied. Holds the proof's lock around
    both, so two runs never overlap. Returns the service's ArchiveResult."""
    now = now or timezone.now()
    proof = CorrespondenceSendProof.objects.select_related(
        "event", "event__mailbox", "event__sender", "event__correspondence",
    ).filter(pk=proof_id).first()
    if proof is None:
        return ArchiveResult(proof_id, "not_applicable", error_code="no_such_proof")
    event = proof.event
    if (
        event.result != CorrespondenceSendEvent.Result.SUCCESS
        or event.provider != "graph" or event.mailbox_id is None
    ):
        return ArchiveResult(proof_id, "not_applicable", state=proof.state, error_code="not_a_graph_send")
    if proof.state == State.ARCHIVED:
        return archive_send_proof(proof_id, actor=actor, reader=reader)     # reports already_archived
    if not _try_lock(proof.pk):
        return ArchiveResult(proof_id, "busy", state=proof.state)
    try:
        proof.refresh_from_db()
        if proof.state == State.ARCHIVED:
            return archive_send_proof(proof_id, actor=actor, reader=reader)
        if event.mailbox.status != MailboxConnection.Status.ACTIVE and reader is None:
            _failure(proof, "mailbox_inactive", now)
            return ArchiveResult(proof.pk, "failed", state=proof.state, error_code=proof.error_code)
        if reader is None and actor is None:
            actor = resolve_archive_actor(event)
            if actor is None:
                _failure(proof, "not_authorized", now)
                return ArchiveResult(proof.pk, "failed", state=proof.state, error_code=proof.error_code)
        result = archive_send_proof(proof_id, actor=actor or resolve_archive_actor(event), reader=reader)
        if result.outcome in ("busy", "already_archived", "not_applicable"):
            return result
        proof.refresh_from_db()
        if proof.state != State.ARCHIVED:
            _apply_schedule(proof, now)
            result.state, result.error_code = proof.state, proof.error_code
        return result
    finally:
        _unlock(proof.pk)


# --- manual retry ------------------------------------------------------------------------------

def _may_send(user, correspondence):
    """The same right that sending the correspondence needed (documents.correspondence_send)."""
    from project_management.permissions import CorrespondencePermission

    permission = CorrespondencePermission("send")
    scope, context = permission._scope_and_context(correspondence.project)
    return permission._check_action_or_full_access(user, "correspondence_send", scope, context)


def check_send_proof_retry(proof_id, *, requested_by):
    """Everything that must hold before a manual retry -- the person's company and right to send, the
    proof not archived yet, a Microsoft 365 send, the master switch on -- and nothing else done.
    Returns the proof, or raises SendProofRetryRefused. The request that queues a retry and the task
    that runs it both go through this."""
    proof = CorrespondenceSendProof.objects.select_related(
        "event", "event__mailbox", "event__correspondence", "event__correspondence__project",
    ).filter(pk=proof_id).first()
    if proof is None or requested_by is None:
        raise SendProofRetryRefused("not_authorized")
    event = proof.event
    correspondence = event.correspondence
    company_id = getattr(requested_by, "company_id", None)
    if (
        company_id is None or event.mailbox is None or event.mailbox.company_profile_id != company_id
        or not _may_send(requested_by, correspondence)
    ):
        raise SendProofRetryRefused("not_authorized")          # nothing revealed about the proof
    if proof.state == State.ARCHIVED:
        raise SendProofRetryRefused("already_archived")
    if event.provider != "graph" or event.result != CorrespondenceSendEvent.Result.SUCCESS:
        raise SendProofRetryRefused("not_applicable")
    if not archiving_enabled():                  # a retry never bypasses the switch
        raise SendProofRetryRefused("archiving_disabled")
    return proof


def retry_send_proof_archive(proof_id, *, requested_by, now=None, reader=None):
    """Run the archiving of a proof again at someone's request (a FAILED, PARTIAL, UNAVAILABLE or
    still-PENDING proof). The person must belong to the mailbox's company and hold the right to send
    on that correspondence; the mailbox is read by that person when they may read it, else by someone
    who really may. Idempotent: it resumes where the proof stopped. Raises SendProofRetryRefused."""
    proof = check_send_proof_retry(proof_id, requested_by=requested_by)
    event = proof.event
    actor = None
    if reader is None:
        try:
            authorize_mailbox_read(requested_by, event.mailbox)
            actor = requested_by
        except MailboxReadError:
            actor = None            # run_archive looks for someone the mailbox's own rules allow
    return run_archive(proof.pk, now=now, actor=actor, reader=reader)


# --- the periodic sweep ------------------------------------------------------------------------

@dataclass
class SweepResult:
    enabled: bool = True
    skipped: str = ""
    examined: int = 0
    archived: int = 0
    partial: int = 0
    pending: int = 0
    failed: int = 0
    busy: int = 0
    unavailable: int = 0
    proof_ids: list = field(default_factory=list)


def due_proofs(now=None):
    """The proofs the schedule says to run now: never tried, or whose `next_attempt_at` has come --
    only successful Microsoft 365 sends LINKS made itself, and only those from the activation date on."""
    now = now or timezone.now()
    since = auto_archive_since()
    if since is None:
        return CorrespondenceSendProof.objects.none()
    return (
        CorrespondenceSendProof.objects.filter(
            event__provider="graph", event__result=CorrespondenceSendEvent.Result.SUCCESS,
            event__mailbox__isnull=False, event__sent_at__gte=since,
        )
        .filter(
            Q(state=State.PENDING, next_attempt_at__isnull=True)
            | Q(state__in=(State.PENDING, State.PARTIAL, State.FAILED), next_attempt_at__lte=now)
        )
        .order_by(F("next_attempt_at").asc(nulls_first=True), "id")
    )


def _sweep_lock():
    if connection.vendor != "postgresql":
        return True
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [SWEEP_LOCK_NAMESPACE, 0])
        return bool(cursor.fetchone()[0])


def _sweep_unlock():
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [SWEEP_LOCK_NAMESPACE, 0])


def sweep_send_proofs(*, now=None, limit=None, max_seconds=None, reader=None):
    """Run the proofs that are due, a bounded batch at a time. It looks only at proofs LINKS itself
    created for its own sends -- it never lists or searches a mailbox to discover messages -- and
    never overlaps another sweep."""
    result = SweepResult()
    if auto_archive_since() is None:
        result.enabled, result.skipped = False, "disabled"
        return result
    now = now or timezone.now()
    limit = limit or int(getattr(settings, "MAIL_SEND_PROOF_SWEEP_BATCH", DEFAULT_SWEEP_BATCH))
    budget = max_seconds or int(getattr(settings, "MAIL_SEND_PROOF_SWEEP_SECONDS", DEFAULT_SWEEP_SECONDS))
    if not _sweep_lock():
        result.skipped = "sweep_in_progress"
        return result
    try:
        started = timezone.now()
        for proof_id in list(due_proofs(now).values_list("pk", flat=True)[:limit]):
            if (timezone.now() - started).total_seconds() > budget:
                break
            run = run_archive(proof_id, now=now, reader=reader)
            result.examined += 1
            result.proof_ids.append(proof_id)
            if run.outcome == "busy":
                result.busy += 1
            elif run.state == State.ARCHIVED:
                result.archived += 1
            elif run.state == State.PARTIAL:
                result.partial += 1
            elif run.state == State.UNAVAILABLE:
                result.unavailable += 1
            elif run.state == State.FAILED:
                result.failed += 1
            else:
                result.pending += 1
        return result
    finally:
        _sweep_unlock()


# --- after a send ------------------------------------------------------------------------------

def enqueue_send_proof_archive(proof_id):
    """Ask the worker for the FIRST archiving attempt of a proof. Called only once the send's
    transaction has committed. A broker that cannot be reached is only logged: the proof stays
    PENDING and the sweep picks it up."""
    try:
        if auto_archive_since() is None:
            return False
        from project_management.tasks import archive_send_proof_task

        archive_send_proof_task.apply_async(args=[proof_id], retry=False)
        return True
    except Exception:  # noqa: BLE001 -- never an error for the request that made the send
        logger.warning("Could not queue the archiving of send proof %s; the sweep will pick it up", proof_id)
        return False
