"""MAIL-1.3D-A: the technical foundation of the incoming synchronisation of mailbox METADATA.

Two strategies, deliberately different, both built only on what already exists (the read-only
MailboxGraphReader, MailboxMessage / MailboxSyncState, `correlate_reply`, `identify_sent_message`):

PERSONAL mailbox -- `sync_personal_replies`
  Never a global read. Only the conversations of OUT correspondences that LINKS sent through Graph
  and whose Sent Items copy has been identified are looked at, one Inbox query per conversation,
  filtered BY GRAPH on the exact conversationId and a time window. Every other message of the
  mailbox is never listed. A found message is stored ONLY when `correlate_reply` answers
  MATCH_CONFIRMED; ambiguous, unrelated and own (outgoing) messages are neither stored nor kept.

SHARED mailbox -- `sync_shared_inbox`
  An incremental Graph delta read of the Inbox (metadata only), the first run bounded to the last
  `MS_SHARED_SYNC_BACKFILL_DAYS` days (30). Every message becomes a MailboxMessage in state `new`
  (the future review queue); one whose conversation is a LINKS-sent one is correlated and linked
  when MATCH_CONFIRMED. The FINAL delta link is saved only after a run read every page. While a
  round is in progress, the only thing saved after each fully processed page is a RESUME marker
  (`pending:<since>:<next page link>`): a crash, an error or the page limit therefore resumes at
  the next unread page instead of re-reading the same pages, and the marker is never mistaken for
  a completed position. A marker is only written once every message of its page is committed, so
  a saved position never runs ahead of the stored metadata; a replay is harmless (idempotent).

  Recovery never depends on the 30-day window alone. `<since>` is the lower bound of the period
  this mailbox has NOT yet been synchronised for: the end of the last COMPLETED round
  (`MailboxSyncState.last_synced_at`, minus an overlap) when there is one, otherwise the start of
  the first bounded read (now - 30 days, the only place that window applies). When an expired
  delta link or resume link (HTTP 410) must be dropped, the read restarts from that bound -- even
  if it is months old -- and the bound is kept in the marker, so it survives another crash.

Not here, on purpose: no import to a LINKS IN correspondence, no status change, no endpoint, no
schedule, no webhook, no notification, no message body or attachment, no change of the read/unread
state of any Outlook message. Nothing calls these functions automatically.

One run per mailbox at a time, across processes: a PostgreSQL session advisory lock (the
database is the one shared resource every process already uses; Django's default per-process
cache would not be). Even so, every cursor write is a compare-and-set on the value the run started
from, so a run can never overwrite a position another run moved.

Visibility, three distinct questions (see mailbox_messages.py): (1) that a reply EXISTS for a
correspondence is a fact about the correspondence and is not exposed here; (2) the right to see a
stored message's METADATA is the right to read its mailbox, nothing more -- correspondence rights
never open it; (3) the right to IMPORT a message is a later lot and will require (2) plus the
correspondence create right. This module only stores; it grants nothing.

Every entry point re-authorizes the acting user -- who must be the mailbox's token holder (the
owner of a personal mailbox, the delegate of a shared one) AND allowed to read it -- before any
Graph request. Results and logs carry counts and error CODES only: never an address, subject,
token, cursor or Graph URL.
"""

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from userapis.mailbox_correlation import (
    MATCH_CONFIRMED, OUTGOING_FOLDERS, correlate_reply, identify_sent_message,
)
from userapis.mailbox_messages import (
    get_sync_state, record_sync_error, record_sync_success, store_message_metadata,
)
from userapis.mailbox_read import (
    MailboxGraphReader, MailboxReadError, _token_holder, authorize_mailbox_read,
)
from userapis.models import MailboxConnection, MailboxMessage, MailboxSyncState

logger = logging.getLogger(__name__)

DEFAULT_REPLY_WINDOW_DAYS = 60
DEFAULT_SHARED_BACKFILL_DAYS = 30
# A reply search re-reads a little before the last successful run, so a message that arrived while
# that run was in progress is never missed (storing is idempotent, a re-read costs nothing).
OVERLAP = timedelta(hours=1)
LOCK_SECONDS = 15 * 60
# Namespace of this module's PostgreSQL advisory locks (first key; the mailbox id is the second).
ADVISORY_LOCK_NAMESPACE = 13031
# An unfinished delta round: `pending:<since>:<next page link>` (see _marker). Anything else stored
# in the cursor is the final delta link of a completed round (or empty).
PENDING_PREFIX = "pending:"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ISO_LENGTH = 20
MAX_CONVERSATIONS = 100
MAX_PAGES_PER_CONVERSATION = 3
MAX_DELTA_PAGES = 100
MAX_SENT_COPY_LOOKUPS = 20


def _days(name, default):
    value = getattr(settings, name, default)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@dataclass
class SyncResult:
    """What a run did: counts and codes only."""
    kind: str
    mailbox_id: int
    ok: bool = True
    error_code: str = ""
    retry_after: object = None
    sent_copies_identified: int = 0
    conversations: int = 0
    pages: int = 0
    examined: int = 0
    own_ignored: int = 0
    already_linked: int = 0
    not_matched: int = 0
    stored: int = 0
    linked: int = 0
    removed: int = 0
    cursor_reset: bool = False
    cursor_saved: bool = False
    incomplete: bool = False


# --- authorization and exclusion --------------------------------------------------------------

def authorize_mailbox_sync(actor, mailbox, kind):
    """Explicit re-authorization before ANY sync operation. `kind` is "individual" or "shared".
    The actor must be allowed to READ the mailbox (same rule as every read) and be the very person
    whose Microsoft token reads it: the owner of a personal mailbox, the delegate of a shared one.
    Raises MailboxReadError otherwise, before anything is looked up or requested."""
    authorize_mailbox_read(actor, mailbox)
    if mailbox.connection_type != kind:
        raise MailboxReadError("invalid_request")
    holder = _token_holder(mailbox)
    if holder is None or holder.pk != actor.pk:
        raise MailboxReadError("not_authorized")


@contextmanager
def _single_run(mailbox):
    """One sync of a mailbox at a time, ACROSS PROCESSES. On PostgreSQL (every configured
    environment) this is a session advisory lock: taken without waiting, released when the run
    ends -- or by the database itself if the process dies or loses its connection, so a crash
    never leaves a stuck lock. Elsewhere (no such lock) it falls back to the cache, which is only
    as shared as the configured cache; the cursor compare-and-set below remains the guarantee."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, int(mailbox.pk)]
            )
            acquired = bool(cursor.fetchone()[0])
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(%s, %s)",
                            [ADVISORY_LOCK_NAMESPACE, int(mailbox.pk)],
                        )
                except Exception:  # the session is gone: the database already released it
                    logger.warning("Mailbox sync lock release skipped mailbox_id=%s", mailbox.pk)
        return
    key = f"mailbox-sync-lock:{mailbox.pk}"
    acquired = cache.add(key, "1", LOCK_SECONDS)
    try:
        yield acquired
    finally:
        if acquired:
            cache.delete(key)


def _iso(moment):
    return moment.astimezone(dt_timezone.utc).strftime(ISO_FORMAT)


def _marker(since, link=""):
    """`pending:<since>:<next link>`; an empty link means "start the bounded read at `since`"."""
    return f"{PENDING_PREFIX}{_iso(since)}:{link}"


def _parse_marker(value):
    """(since, link) of a resume marker. A marker written without a bound (older format) gives
    since=None and the whole remainder as the link."""
    rest = value[len(PENDING_PREFIX):]
    try:
        since = datetime.strptime(rest[:ISO_LENGTH], ISO_FORMAT).replace(tzinfo=dt_timezone.utc)
        if rest[ISO_LENGTH:ISO_LENGTH + 1] != ":":
            raise ValueError
    except ValueError:
        return None, rest
    return since, rest[ISO_LENGTH + 1:]


def _recovery_since(last_synced_at, default_cutoff):
    """Where an unsynchronised period starts: the end of the last completed round (with an
    overlap), or -- never synchronised -- the bounded first read."""
    return last_synced_at - OVERLAP if last_synced_at else default_cutoff


def _plan_round(saved, last_synced_at, default_cutoff):
    """From the saved cursor, what to read next: (link, since, first_page_is_bounded_read).
    `link` None means a fresh bounded read from `since`."""
    if not saved:
        return None, _recovery_since(last_synced_at, default_cutoff), True
    if saved.startswith(PENDING_PREFIX):
        since, link = _parse_marker(saved)
        since = since or _recovery_since(last_synced_at, default_cutoff)
        return (link or None), since, not link
    return saved, _recovery_since(last_synced_at, default_cutoff), False


class CursorConflict(Exception):
    """Another run moved the saved position since this run started: this run stops, untouched."""


class _Cursor:
    """The saved cursor value as THIS run last wrote or read it: every write is a compare-and-set
    against it."""

    def __init__(self, mailbox, value):
        self.mailbox = mailbox
        self.value = value

    def save(self, new, *, complete=False):
        with transaction.atomic():
            state = MailboxSyncState.objects.select_for_update().get(mailbox=self.mailbox)
            if state.cursor != self.value:
                raise CursorConflict()
            state.cursor = new
            if complete:
                state.last_synced_at = timezone.now()
                state.last_error_code = ""
                state.last_error_at = None
            state.save()
        self.value = new


# --- sent copies -------------------------------------------------------------------------------

def identify_pending_sent_copies(mailbox, *, actor, reader, now=None, window_days=None, limit=None):
    """Find, with the existing `identify_sent_message`, the Sent Items copy of each recent
    SUCCESSFUL Graph send from this mailbox that has none recorded yet. Prepared for an automatic
    call after a confirmed send (nothing in the send path calls it, so the send and its
    double-send protections are untouched). A copy that is not in Sent Items yet is simply tried
    again by the next run, until the window is over. Returns how many copies were recorded."""
    from project_management.models import Correspondence, CorrespondenceSendEvent

    now = now or timezone.now()
    window = timedelta(days=window_days or _days("MS_REPLY_SYNC_WINDOW_DAYS", DEFAULT_REPLY_WINDOW_DAYS))
    # MAIL-1.4-A: "identified" is a fact about ONE send event (its proof points to its copy), not
    # about the correspondence: a resend of an already identified correspondence has a new copy to
    # find. Without a linked copy, the FIRST send of a correspondence keeps its former meaning (a
    # copy already recorded for the correspondence is that send's); every later send of it has a
    # copy of its own still to find.
    recorded_copy = MailboxMessage.objects.filter(
        mailbox=mailbox, correspondence=OuterRef("correspondence_id"), folder__in=OUTGOING_FOLDERS,
    )
    earlier_send = CorrespondenceSendEvent.objects.filter(
        correspondence=OuterRef("correspondence_id"), provider="graph",
        result=CorrespondenceSendEvent.Result.SUCCESS,
    ).filter(Q(sent_at__lt=OuterRef("sent_at")) | Q(sent_at=OuterRef("sent_at"), pk__lt=OuterRef("pk")))
    events = (
        CorrespondenceSendEvent.objects.filter(
            mailbox=mailbox, provider="graph", result=CorrespondenceSendEvent.Result.SUCCESS,
            sent_at__gte=now - window, correspondence__direction=Correspondence.Direction.OUT,
        )
        .annotate(_has_recorded_copy=Exists(recorded_copy), _has_earlier_send=Exists(earlier_send))
        .exclude(proof__mailbox_message__isnull=False)
        .exclude(_has_recorded_copy=True, _has_earlier_send=False)
        .select_related("correspondence")
        .order_by("-sent_at")[: (limit or MAX_SENT_COPY_LOOKUPS)]
    )
    identified = 0
    for event in events:
        if identify_sent_message(event, actor=actor, reader=reader) is not None:
            identified += 1
    return identified


def _sent_copies(mailbox):
    """The recorded Sent Items copies of LINKS-sent, issued OUT correspondences of this mailbox."""
    from project_management.models import Correspondence, CorrespondenceSendEvent

    sent_through_graph = CorrespondenceSendEvent.objects.filter(
        correspondence=OuterRef("correspondence_id"), mailbox=mailbox, provider="graph",
        result=CorrespondenceSendEvent.Result.SUCCESS,
    )
    return MailboxMessage.objects.filter(
        mailbox=mailbox, company_profile_id=mailbox.company_profile_id,
        folder__in=OUTGOING_FOLDERS, correspondence__isnull=False,
        correspondence__direction=Correspondence.Direction.OUT,
        correspondence__status=Correspondence.Status.ISSUED,
    ).exclude(conversation_id="").filter(Exists(sent_through_graph))


# --- shared handling of one found message -----------------------------------------------------

def _link_if_confirmed(mailbox, row, meta, reader, actor, result):
    """Existing correlation, nothing parallel: fetch the whitelisted threading headers of ONE
    message and ask `correlate_reply`. Only MATCH_CONFIRMED links the row."""
    ident = reader.get_reply_identification(meta.graph_id)
    correlation = correlate_reply(
        mailbox, actor=actor, in_reply_to=ident.in_reply_to, references=ident.references,
        conversation_id=meta.conversation_id or ident.conversation_id,
        message_internet_id=meta.internet_message_id or ident.internet_message_id,
        message_folder="inbox",
    )
    if correlation.result != MATCH_CONFIRMED:
        result.not_matched += 1
        return None
    with transaction.atomic():
        if row is None:
            row, created = store_message_metadata(mailbox, meta)
            result.stored += int(created)
        row = MailboxMessage.objects.select_for_update().get(pk=row.pk)
        if not row.internet_message_id and ident.internet_message_id:
            row.internet_message_id = ident.internet_message_id
            row.save(update_fields=["internet_message_id", "updated_at"])
        if row.correspondence_id is None:
            row.correspondence_id = correlation.correspondence_id
            row.save(update_fields=["correspondence", "updated_at"])
            result.linked += 1
    return row


def _known_row(mailbox, meta):
    query = Q(graph_message_id=meta.graph_id)
    if meta.internet_message_id:
        query |= Q(internet_message_id=meta.internet_message_id)
    return MailboxMessage.objects.filter(mailbox=mailbox).filter(query).first()


def _fail(mailbox, result, exc):
    """Record a failed run: the error CODE only, the mailbox status untouched, no cursor moved."""
    record_sync_error(mailbox, exc)
    result.ok = False
    result.error_code = str(getattr(exc, "code", "") or "error")[:40]
    result.retry_after = getattr(exc, "retry_after", None)
    logger.warning("Mailbox sync failed mailbox_id=%s code=%s", mailbox.pk, result.error_code)
    return result


# --- personal mailbox -------------------------------------------------------------------------

def sync_personal_replies(
    mailbox, *, actor, reader=None, now=None, window_days=None, max_conversations=None,
):
    """Look for replies to what LINKS sent from a PERSONAL mailbox -- and only for that (see the
    module docstring). Returns a SyncResult; authorization failures raise MailboxReadError before
    anything happens, every other Graph failure is recorded in MailboxSyncState and returned."""
    authorize_mailbox_sync(actor, mailbox, MailboxConnection.ConnectionType.INDIVIDUAL)
    result = SyncResult("personal", mailbox.pk)
    with _single_run(mailbox) as acquired:
        if not acquired:
            result.ok, result.error_code = False, "sync_in_progress"
            return result
        now = now or timezone.now()
        window = timedelta(days=window_days or _days("MS_REPLY_SYNC_WINDOW_DAYS", DEFAULT_REPLY_WINDOW_DAYS))
        try:
            reader = reader or MailboxGraphReader(mailbox, actor=actor)
            result.sent_copies_identified = identify_pending_sent_copies(
                mailbox, actor=actor, reader=reader, now=now, window_days=window.days,
            )
            last_run = get_sync_state(mailbox).last_synced_at
            conversations = {}
            for copy in _sent_copies(mailbox).filter(received_at__gte=now - window):
                conversations.setdefault(copy.conversation_id, []).append(copy)
            ordered = sorted(
                conversations.items(), key=lambda item: min(c.received_at for c in item[1]),
                reverse=True,
            )[: (max_conversations or MAX_CONVERSATIONS)]
            result.conversations = len(ordered)
            own_address = mailbox.email_address.strip().lower()
            for conversation_id, copies in ordered:
                earliest = min(c.received_at for c in copies)
                if last_run is None or any(c.created_at >= last_run - OVERLAP for c in copies):
                    # A copy identified since the last run: its replies were never looked for.
                    since = earliest - timedelta(minutes=1)
                else:
                    since = max(earliest - timedelta(minutes=1), last_run - OVERLAP)
                page_url = None
                for _ in range(MAX_PAGES_PER_CONVERSATION):
                    page = reader.list_messages(
                        "inbox", top=10, received_since=since, received_until=now,
                        conversation_id=conversation_id, page_url=page_url,
                    )
                    result.pages += 1
                    for meta in page.messages:
                        result.examined += 1
                        if meta.sender_address == own_address:
                            # The mailbox's own message (its CC copy, its own answer): never a reply.
                            result.own_ignored += 1
                            continue
                        known = _known_row(mailbox, meta)
                        if known is not None and known.correspondence_id:
                            result.already_linked += 1
                            continue
                        _link_if_confirmed(mailbox, known, meta, reader, actor, result)
                    if not page.next_link:
                        break
                    page_url = page.next_link
            record_sync_success(mailbox)
        except MailboxReadError as exc:
            return _fail(mailbox, result, exc)
    return result


# --- shared mailbox ---------------------------------------------------------------------------

def sync_shared_inbox(
    mailbox, *, actor, reader=None, now=None, backfill_days=None, max_pages=None,
):
    """Incremental metadata read of a SHARED mailbox's Inbox (see the module docstring)."""
    authorize_mailbox_sync(actor, mailbox, MailboxConnection.ConnectionType.SHARED)
    result = SyncResult("shared", mailbox.pk)
    with _single_run(mailbox) as acquired:
        if not acquired:
            result.ok, result.error_code = False, "sync_in_progress"
            return result
        now = now or timezone.now()
        cutoff = now - timedelta(days=backfill_days or _days("MS_SHARED_SYNC_BACKFILL_DAYS", DEFAULT_SHARED_BACKFILL_DAYS))
        try:
            reader = reader or MailboxGraphReader(mailbox, actor=actor)
            result.sent_copies_identified = identify_pending_sent_copies(
                mailbox, actor=actor, reader=reader, now=now,
            )
            sent_conversations = set(_sent_copies(mailbox).values_list("conversation_id", flat=True))
            state = get_sync_state(mailbox)
            last_synced_at = state.last_synced_at
            cursor = _Cursor(mailbox, state.cursor)
            while True:
                try:
                    finished = _drain_delta(
                        mailbox, reader, actor, cursor, last_synced_at, cutoff,
                        sent_conversations, result, max_pages or MAX_DELTA_PAGES,
                    )
                    break
                except MailboxReadError as exc:
                    if (
                        cursor.value and not result.cursor_reset
                        and exc.code in ("sync_state_expired", "invalid_request")
                    ):
                        # The saved position (or resume link) is no longer usable. Read again
                        # from the start of the period NOT yet synchronised -- never merely the
                        # last 30 days -- and keep that bound in the saved marker. Once per run;
                        # what was stored stays (idempotent).
                        _, since, _ = _plan_round(cursor.value, last_synced_at, cutoff)
                        cursor.save(_marker(since))
                        result.cursor_reset = True
                        continue
                    raise
            if not finished:
                # Page limit: not an error. The resume marker is saved, the next run continues
                # from the next unread page; the final position is NOT recorded.
                result.incomplete = True
        except CursorConflict:
            result.ok, result.error_code = False, "cursor_conflict"
            return result
        except MailboxReadError as exc:
            return _fail(mailbox, result, exc)
    return result


def _drain_delta(
    mailbox, reader, actor, cursor, last_synced_at, default_cutoff, sent_conversations, result,
    max_pages,
):
    """Read the pages of one delta round, saving a resume marker after each fully processed page
    and the final delta link only after the last. Returns True when the round is complete, False
    when the page limit stopped it (the marker is saved, so a later run resumes there)."""
    link, since, initial = _plan_round(cursor.value, last_synced_at, default_cutoff)
    if link is None:
        page = reader.delta_messages(since=since)
    else:
        page = reader.delta_messages(page_url=link)
    own_address = mailbox.email_address.strip().lower()
    pages = 0
    while True:
        pages += 1
        result.pages += 1
        for meta in page.messages:
            if initial and meta.received_at is not None and meta.received_at < since:
                continue  # belt and braces: the first read is bounded even if Graph ignored it
            result.examined += 1
            if meta.sender_address == own_address:
                result.own_ignored += 1
                continue
            row, created = store_message_metadata(mailbox, meta)
            result.stored += int(created)
            if row.correspondence_id is None and meta.conversation_id in sent_conversations:
                _link_if_confirmed(mailbox, row, meta, reader, actor, result)
        for graph_id in page.removed_ids:
            # Deleted or moved out of the Inbox: only an untouched review-queue row goes away;
            # anything linked, imported or ignored is kept as evidence of a decision.
            deleted, _ = MailboxMessage.objects.filter(
                mailbox=mailbox, graph_message_id=graph_id, folder="inbox",
                state=MailboxMessage.ProcessingState.NEW, correspondence__isnull=True,
            ).delete()
            result.removed += deleted
        if page.delta_link:
            cursor.save(page.delta_link, complete=True)
            result.cursor_saved = True
            return True
        if not page.next_link:
            raise MailboxReadError("invalid_response")
        # Everything on this page is stored: remember where to resume, never the final position.
        cursor.save(_marker(since, page.next_link))
        if pages >= max_pages:
            return False
        page = reader.delta_messages(page_url=page.next_link)
