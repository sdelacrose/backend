"""MAIL-1.3D-A: the incoming-sync foundation. No real Graph call, no real mail: the reader is either
a scripted fake (sync logic) or the real reader over a patched `requests.get` (HTTP contract)."""

from datetime import timedelta
from unittest.mock import patch

from types import SimpleNamespace

from django.core.cache import cache
from django.db import connection
from django.utils import timezone

from models.models import UserOutlookCredentials
from project_management.models import Correspondence, CorrespondenceSendEvent
from project_management.test_correspondence_send_graph import GraphSendTestBase
from userapis.mailbox_correlation import build_send_marker
from userapis.mailbox_messages import (
    can_read_stored_message, get_sync_state, messages_visible_to, record_sync_success,
    store_message_metadata,
)
from userapis.mailbox_read import (
    DeltaPage, MailboxGraphReader, MailboxReadError, MessagePage, ReplyIdentification,
    normalize_message,
)
from userapis.mailbox_sync import (
    ADVISORY_LOCK_NAMESPACE, _Cursor, authorize_mailbox_sync, identify_pending_sent_copies,
    sync_personal_replies, sync_shared_inbox,
)
from userapis.models import (
    MailboxConnection, MailboxMessage, MailboxProjectAssignment, MailboxSyncState, MailboxUserGrant,
)
from userapis.test_mailbox import _make_company, _make_user
from userapis.test_mailbox_read import BASE, SENTINEL_BODY, FakeGraph, FakeResponse, raw_message

SENT_ID = "<sent-1@links-local.invalid>"


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class ScriptedReader:
    """Stands in for MailboxGraphReader: answers from scripts and records every call. It has NO
    way to write, and the tests assert which of its methods were used."""

    def __init__(self):
        self.calls = []
        self.inbox = {}          # conversation id -> [list of metas per page]
        self.sent_items = []     # metas answering the Sent Items lookup
        self.idents = {}         # graph id -> ReplyIdentification
        self.delta = {}          # "initial" or a page url -> DeltaPage
        self.errors = {}         # method -> [None | exception, ...] consumed one per call

    def _maybe_fail(self, name):
        queue = self.errors.get(name) or []
        if queue:
            exc = queue.pop(0)
            if exc is not None:
                raise exc

    def list_messages(self, folder="inbox", **kw):
        self.calls.append(("list", folder, kw))
        self._maybe_fail("list")
        if folder == "sentitems":
            return MessagePage(messages=list(self.sent_items))
        pages = self.inbox.get(kw.get("conversation_id"), [[]])
        page_url = kw.get("page_url")
        index = int(page_url.split(":")[1]) if page_url else 0
        return MessagePage(
            messages=list(pages[index]),
            next_link=f"page:{index + 1}" if index + 1 < len(pages) else "",
        )

    def get_reply_identification(self, message_id):
        self.calls.append(("ident", message_id, {}))
        self._maybe_fail("ident")
        return self.idents.get(message_id, ReplyIdentification())

    def delta_messages(self, folder="inbox", *, since=None, page_url=None, page_size=50):
        self.calls.append(("delta", folder, {"since": since, "page_url": page_url}))
        self._maybe_fail("delta")
        return self.delta[page_url or "initial"]

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class OtherSessionLock:
    """Holds a mailbox's sync lock from a second database connection, exactly what another
    process running the same sync would hold."""

    def __init__(self, mailbox):
        self.mailbox = mailbox
        self.conn = connection.copy()

    def try_take(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, self.mailbox.pk])
            return cur.fetchone()[0]

    def release(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s, %s)", [ADVISORY_LOCK_NAMESPACE, self.mailbox.pk])
        self.conn.close()


class SyncBase(GraphSendTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        # the delegate of the shared mailbox may read it (the sync re-authorizes on that right)
        MailboxUserGrant.objects.create(
            mailbox=self.shared_mailbox, user=self.delegate, can_read=True, can_send=False
        )
        self.reader = ScriptedReader()
        self.now = timezone.now()
        self.other_sessions = []
        self.addCleanup(self._close_other_sessions)

    def _close_other_sessions(self):
        for lock in self.other_sessions:
            try:
                lock.conn.close()
            except Exception:
                pass

    def marker(self, link="", since=None):
        """The resume marker the sync saves: `pending:<since>:<next link>`."""
        return "pending:%s:%s" % (iso(since or self.now - timedelta(days=30)), link)

    def backdate_sync(self, mailbox, days):
        MailboxSyncState.objects.filter(mailbox=mailbox).update(last_synced_at=self.now - timedelta(days=days))

    def hold_from_another_process(self, mailbox):
        lock = OtherSessionLock(mailbox)
        self.other_sessions.append(lock)
        self.assertTrue(lock.try_take())
        return lock

    def sent_out(self, mailbox, *, subject=None, copy=True, conversation="conv-1",
                 internet_id=SENT_ID, n=1):
        """An OUT correspondence really sent through Graph (mocked) and, optionally, its recorded
        Sent Items copy."""
        corr = self.approved_out(**({"subject": subject} if subject else {}))
        event, _, _ = self.send_via_graph(corr, mailbox)
        row = self.sent_copy(corr, mailbox, conversation, internet_id, n) if copy else None
        return corr, event, row

    def sent_copy(self, corr, mailbox, conversation="conv-1", internet_id=SENT_ID, n=1, minutes=10):
        meta = normalize_message(raw_message(
            n, id=f"AAMk-sent-{n}-{corr.pk}", internetMessageId=internet_id,
            conversationId=conversation, receivedDateTime=iso(self.now - timedelta(minutes=minutes)),
        ), "sentitems")
        row, _ = store_message_metadata(mailbox, meta)
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=corr)
        row.refresh_from_db()
        return row

    def reply(self, n=7, conversation="conv-1", sender="pascal@example.com", **over):
        raw = raw_message(
            n, id=f"AAMk-reply-{n}", internetMessageId=f"<reply-{n}@example.com>",
            conversationId=conversation, receivedDateTime=iso(self.now - timedelta(minutes=2)),
            **{"from": {"emailAddress": {"name": "P", "address": sender}}}, **over,
        )
        return normalize_message(raw, "inbox")

    def answers_sent(self, meta, *, in_reply_to=SENT_ID, references=(SENT_ID,)):
        self.reader.idents[meta.graph_id] = ReplyIdentification(
            internet_message_id=meta.internet_message_id, conversation_id=meta.conversation_id,
            in_reply_to=in_reply_to, references=tuple(references),
        )

    def personal(self, **kw):
        return sync_personal_replies(
            self.individual_mailbox, actor=self.owner, reader=self.reader, now=self.now, **kw
        )

    def shared(self, **kw):
        return sync_shared_inbox(
            self.shared_mailbox, actor=self.delegate, reader=self.reader, now=self.now, **kw
        )

    def assertNoIncomingCorrespondence(self):
        self.assertEqual(Correspondence.objects.filter(direction="IN").count(), 0)


# --- personal mailboxes ------------------------------------------------------------------------

class PersonalSyncTests(SyncBase):
    def test_a_confirmed_reply_is_stored_linked_and_changes_no_business_status(self):
        corr, _, _ = self.sent_out(self.individual_mailbox)
        before = Correspondence.objects.filter(pk=corr.pk).values("status", "response_status").get()
        reply = self.reply()
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        result = self.personal()
        self.assertTrue(result.ok)
        self.assertEqual((result.stored, result.linked), (1, 1))
        row = MailboxMessage.objects.get(graph_message_id=reply.graph_id)
        self.assertEqual((row.correspondence_id, row.folder, row.state), (corr.pk, "inbox", "new"))
        self.assertEqual(
            Correspondence.objects.filter(pk=corr.pk).values("status", "response_status").get(), before
        )
        self.assertNoIncomingCorrespondence()

    def test_a_correspondence_that_requires_no_reply_is_still_followed(self):
        corr, _, _ = self.sent_out(self.individual_mailbox)
        Correspondence.objects.filter(pk=corr.pk).update(response_status="NOT_REQUIRED")
        reply = self.reply()
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        self.assertEqual(self.personal().linked, 1)

    def test_ambiguous_unrelated_and_own_messages_are_never_stored(self):
        self.sent_out(self.individual_mailbox)
        same_conversation_only = self.reply(n=7)       # conversation alone: ambiguous
        unrelated_headers = self.reply(n=8)            # headers point elsewhere: ambiguous
        own = self.reply(n=9, sender=self.individual_mailbox.email_address)
        self.reader.inbox["conv-1"] = [[same_conversation_only, unrelated_headers, own]]
        self.answers_sent(same_conversation_only, in_reply_to="", references=())
        self.answers_sent(unrelated_headers, in_reply_to="<other@example.com>", references=("<other@example.com>",))
        result = self.personal()
        self.assertTrue(result.ok)
        self.assertEqual((result.stored, result.linked, result.not_matched, result.own_ignored), (0, 0, 2, 1))
        # only the two real candidates had headers fetched: the mailbox's own message never was
        self.assertEqual({c[1] for c in self.reader.of("ident")}, {same_conversation_only.graph_id, unrelated_headers.graph_id})
        self.assertEqual(MailboxMessage.objects.filter(folder="inbox").count(), 0)

    def test_graph_is_asked_server_side_for_one_conversation_inbox_only_and_never_delta(self):
        self.sent_out(self.individual_mailbox)
        self.personal()
        listing = [c for c in self.reader.of("list") if c[1] == "inbox"]
        self.assertEqual(len(listing), 1)
        kw = listing[0][2]
        self.assertEqual(kw["conversation_id"], "conv-1")
        self.assertIsNotNone(kw["received_since"])
        self.assertEqual(kw["received_until"], self.now)
        self.assertLessEqual(kw["top"], 10)
        self.assertEqual({c[1] for c in self.reader.of("list")}, {"inbox"})
        self.assertEqual(self.reader.of("delta"), [])

    def test_only_issued_graph_sent_outs_with_an_identified_copy_are_followed(self):
        self.sent_out(self.individual_mailbox, copy=False)                      # copy not identified
        draft = self.approved_out(subject="Never sent")                         # never sent
        self.sent_copy(draft, self.individual_mailbox, "conv-draft", "<d@x>", n=2)
        other_mailbox_corr, _, _ = self.sent_out(self.shared_mailbox, subject="Other box", conversation="conv-shared", internet_id="<s@x>", n=3)
        result = self.personal()
        self.assertEqual(result.conversations, 0)
        self.assertEqual([c for c in self.reader.of("list") if c[1] == "inbox"], [])

    def test_the_window_is_configurable_and_defaults_to_sixty_days(self):
        corr, _, row = self.sent_out(self.individual_mailbox)
        MailboxMessage.objects.filter(pk=row.pk).update(received_at=self.now - timedelta(days=59))
        self.assertEqual(self.personal().conversations, 1)
        MailboxMessage.objects.filter(pk=row.pk).update(received_at=self.now - timedelta(days=61))
        self.assertEqual(self.personal().conversations, 0)
        self.assertEqual(self.personal(window_days=90).conversations, 1)

    def test_a_second_run_is_idempotent(self):
        self.sent_out(self.individual_mailbox)
        reply = self.reply()
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        self.personal()
        again = self.personal()
        self.assertEqual((again.stored, again.linked, again.already_linked), (0, 0, 1))
        self.assertEqual(MailboxMessage.objects.filter(folder="inbox").count(), 1)

    def test_pagination_is_followed_and_bounded(self):
        self.sent_out(self.individual_mailbox)
        first, second = self.reply(n=7), self.reply(n=8)
        self.reader.inbox["conv-1"] = [[first], [second]]
        self.answers_sent(first)
        self.answers_sent(second)
        result = self.personal()
        self.assertEqual((result.pages, result.linked), (2, 2))
        self.assertEqual(self.reader.of("list")[-1][2]["page_url"], "page:1")

    def test_after_a_failure_nothing_advances_and_the_next_run_resumes(self):
        self.sent_out(self.individual_mailbox)
        reply = self.reply()
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        self.reader.errors["list"] = [MailboxReadError("temporary")]
        failed = self.personal()
        self.assertEqual((failed.ok, failed.error_code), (False, "temporary"))
        state = get_sync_state(self.individual_mailbox)
        self.assertEqual((state.last_error_code, state.last_synced_at), ("temporary", None))
        self.individual_mailbox.refresh_from_db()
        self.assertEqual(self.individual_mailbox.status, "active")
        resumed = self.personal()
        self.assertTrue(resumed.ok)
        self.assertEqual(resumed.linked, 1)
        state.refresh_from_db()
        self.assertEqual((state.last_error_code, state.cursor), ("", ""))
        self.assertIsNotNone(state.last_synced_at)

    def test_rate_limiting_is_reported_with_its_retry_delay(self):
        self.sent_out(self.individual_mailbox)
        self.reader.errors["list"] = [MailboxReadError("rate_limited", retry_after=42)]
        result = self.personal()
        self.assertEqual((result.ok, result.error_code, result.retry_after), (False, "rate_limited", 42))
        self.assertEqual(get_sync_state(self.individual_mailbox).last_error_code, "rate_limited")

    def test_a_token_failure_is_recorded_as_a_code_only(self):
        self.sent_out(self.individual_mailbox)
        self.reader.errors["list"] = [MailboxReadError("token_refresh_failed")]
        result = self.personal()
        self.assertEqual(result.error_code, "token_refresh_failed")
        self.assertEqual(get_sync_state(self.individual_mailbox).last_error_code, "token_refresh_failed")

    def test_a_later_run_reads_only_since_the_previous_run_for_an_already_followed_conversation(self):
        corr, _, row = self.sent_out(self.individual_mailbox)
        MailboxMessage.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(days=3), received_at=self.now - timedelta(days=3)
        )
        record_sync_success(self.individual_mailbox)
        MailboxSyncState.objects.filter(mailbox=self.individual_mailbox).update(
            last_synced_at=self.now - timedelta(days=1)
        )
        self.personal()
        since = [c for c in self.reader.of("list") if c[1] == "inbox"][0][2]["received_since"]
        self.assertEqual(since, self.now - timedelta(days=1) - timedelta(hours=1))

    def test_after_a_45_day_gap_replies_since_the_last_completed_run_are_still_found(self):
        corr, _, row = self.sent_out(self.individual_mailbox)
        MailboxMessage.objects.filter(pk=row.pk).update(
            created_at=self.now - timedelta(days=50), received_at=self.now - timedelta(days=50)
        )
        record_sync_success(self.individual_mailbox)
        MailboxSyncState.objects.filter(mailbox=self.individual_mailbox).update(
            last_synced_at=self.now - timedelta(days=45)
        )
        reply = normalize_message(raw_message(
            7, id="AAMk-reply-gap", internetMessageId="<reply-gap@example.com>", conversationId="conv-1",
            receivedDateTime=iso(self.now - timedelta(days=40)),
            **{"from": {"emailAddress": {"address": "pascal@example.com"}}},
        ), "inbox")
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        result = self.personal()
        since = [c for c in self.reader.of("list") if c[1] == "inbox"][0][2]["received_since"]
        self.assertEqual(since, self.now - timedelta(days=45) - timedelta(hours=1))
        self.assertEqual(result.linked, 1)

    def test_a_sent_copy_is_identified_first_then_its_conversation_is_followed(self):
        corr, event, _ = self.sent_out(self.individual_mailbox, copy=False, conversation="conv-new")
        sent = normalize_message(raw_message(
            3, id="AAMk-sent-new", subject=event.subject, internetMessageId="<sent-new@links-local.invalid>",
            conversationId="conv-new", receivedDateTime=iso(self.now - timedelta(minutes=1)),
            toRecipients=[{"emailAddress": {"address": "jane.doe@links-local.invalid"}}],
        ), "sentitems")
        self.reader.sent_items = [sent]
        self.reader.idents[sent.graph_id] = ReplyIdentification(
            internet_message_id=sent.internet_message_id, conversation_id="conv-new",
            send_marker=build_send_marker(event.pk, corr.pk),
        )
        result = self.personal()
        self.assertEqual((result.sent_copies_identified, result.conversations), (1, 1))
        self.assertEqual(MailboxMessage.objects.get(graph_message_id="AAMk-sent-new").correspondence_id, corr.pk)
        # the copy is now known: a second run does not look it up again
        self.reader.calls.clear()
        self.assertEqual(self.personal().sent_copies_identified, 0)
        self.assertEqual([c for c in self.reader.of("list") if c[1] == "sentitems"], [])

    def test_identification_ignores_failed_sends_other_mailboxes_and_old_sends(self):
        corr, event, _ = self.sent_out(self.individual_mailbox, copy=False)
        self.sent_out(self.shared_mailbox, subject="Elsewhere", copy=False, conversation="c2", internet_id="<s2@x>", n=2)
        CorrespondenceSendEvent.objects.filter(pk=event.pk).update(sent_at=self.now - timedelta(days=61))
        self.reader.sent_items = []
        self.assertEqual(identify_pending_sent_copies(self.individual_mailbox, actor=self.owner, reader=self.reader, now=self.now), 0)
        self.assertEqual(self.reader.of("list"), [])

    def test_a_run_held_by_another_process_does_not_start_and_the_lock_is_released_afterwards(self):
        self.sent_out(self.individual_mailbox)
        other = self.hold_from_another_process(self.individual_mailbox)
        blocked = self.personal()
        self.assertEqual((blocked.ok, blocked.error_code), (False, "sync_in_progress"))
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(get_sync_state(self.individual_mailbox).last_synced_at, None)
        other.release()
        self.assertTrue(self.personal().ok)
        # the finished run released its own lock: another process can take it
        again = OtherSessionLock(self.individual_mailbox)
        self.other_sessions.append(again)
        self.assertTrue(again.try_take())

    def test_the_lock_is_also_released_when_a_run_fails(self):
        self.sent_out(self.individual_mailbox)
        self.reader.errors["list"] = [MailboxReadError("temporary")]
        self.assertFalse(self.personal().ok)
        other = OtherSessionLock(self.individual_mailbox)
        self.other_sessions.append(other)
        self.assertTrue(other.try_take())

    def test_without_postgresql_locks_the_cache_is_the_fallback(self):
        self.sent_out(self.individual_mailbox)
        with patch("userapis.mailbox_sync.connection", SimpleNamespace(vendor="sqlite")):
            cache.add(f"mailbox-sync-lock:{self.individual_mailbox.pk}", "1", 60)
            self.assertEqual(self.personal().error_code, "sync_in_progress")
            cache.clear()
            self.assertTrue(self.personal().ok)
    def test_nothing_reads_a_body_or_attachment_or_writes_to_the_mailbox(self):
        for name in ("send", "delete", "move", "mark_read", "update", "get_body", "attachments"):
            self.assertFalse(hasattr(self.reader, name))
        self.assertTrue({"body", "bodyPreview", "attachments"}.isdisjoint(
            {f.name for f in MailboxMessage._meta.get_fields()}
        ))


class PersonalSyncAuthorizationTests(SyncBase):
    def assertRefused(self, code, *args, kind="personal"):
        function = sync_personal_replies if kind == "personal" else sync_shared_inbox
        with self.assertRaises(MailboxReadError) as ctx:
            function(args[0], actor=args[1], reader=self.reader, now=self.now)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(MailboxMessage.objects.count(), 0)

    def test_only_the_owner_may_sync_a_personal_mailbox(self):
        self.assertRefused("not_authorized", self.individual_mailbox, self.approver)
        self.assertRefused("not_authorized", self.individual_mailbox, self.delegate)

    def test_a_person_of_another_company_is_refused(self):
        stranger = _make_user("elsewhere@other.test", _make_company("Other"))
        self.assertRefused("not_authorized", self.individual_mailbox, stranger)

    def test_an_inactive_or_non_microsoft_mailbox_is_refused(self):
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        self.individual_mailbox.refresh_from_db()
        self.assertRefused("mailbox_inactive", self.individual_mailbox, self.owner)

    def test_the_wrong_kind_of_mailbox_is_refused_for_each_strategy(self):
        self.assertRefused("invalid_request", self.shared_mailbox, self.delegate)
        self.assertRefused("invalid_request", self.individual_mailbox, self.owner, kind="shared")

    def test_only_the_delegate_with_a_read_right_may_sync_a_shared_mailbox(self):
        # a reader who is not the token holder
        self.assertRefused("not_authorized", self.shared_mailbox, self.owner, kind="shared")
        # the delegate loses the read right
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.delegate).update(can_read=False)
        self.assertRefused("not_authorized", self.shared_mailbox, self.delegate, kind="shared")

    def test_authorization_helper_is_explicit(self):
        authorize_mailbox_sync(self.owner, self.individual_mailbox, "individual")
        authorize_mailbox_sync(self.delegate, self.shared_mailbox, "shared")


class PersonalVisibilityTests(SyncBase):
    def test_correspondence_rights_never_open_the_metadata_of_a_linked_personal_message(self):
        corr, _, copy = self.sent_out(self.individual_mailbox)
        reply = self.reply()
        self.reader.inbox["conv-1"] = [[reply]]
        self.answers_sent(reply)
        self.personal()
        stored = MailboxMessage.objects.get(graph_message_id=reply.graph_id)
        self.assertEqual(stored.correspondence_id, corr.pk)
        # level 2 -- the right to see the metadata: the mailbox owner only
        self.assertIn(stored.pk, set(messages_visible_to(self.owner).values_list("pk", flat=True)))
        # people who DO hold correspondence rights on this very correspondence do not get it
        for person in (self.approver, self.delegate):
            self.assertEqual(messages_visible_to(person).count(), 0, person.email)
            self.assertFalse(can_read_stored_message(person, stored), person.email)
        stranger = _make_user("elsewhere@other.test", _make_company("Other"))
        self.assertFalse(can_read_stored_message(stranger, stored))
        self.assertEqual(messages_visible_to(stranger).count(), 0)
        # level 1 -- the existence of a reply is not carried by the correspondence payload
        from project_management.serializers.correspondence import CorrespondenceSerializer
        self.assertFalse([name for name in CorrespondenceSerializer().fields if "mailbox" in name])

    def test_the_owner_loses_the_view_when_the_mailbox_is_no_longer_active(self):
        corr, _, copy = self.sent_out(self.individual_mailbox)
        self.assertTrue(can_read_stored_message(self.owner, copy))
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        copy.mailbox.refresh_from_db()
        self.assertFalse(can_read_stored_message(self.owner, copy))
        self.assertEqual(messages_visible_to(self.owner).count(), 0)

    def test_an_unlinked_row_of_a_personal_mailbox_is_never_visible(self):
        corr, _, copy = self.sent_out(self.individual_mailbox)
        loose = normalize_message(raw_message(5, id="AAMk-loose", internetMessageId="<loose@x>"), "inbox")
        row, _ = store_message_metadata(self.individual_mailbox, loose)
        visible = set(messages_visible_to(self.owner).values_list("pk", flat=True))
        self.assertEqual(visible, {copy.pk})
        self.assertTrue(can_read_stored_message(self.owner, copy))
        self.assertFalse(can_read_stored_message(self.owner, row))
        # nobody else, linked or not
        self.assertEqual(messages_visible_to(self.approver).count(), 0)
        self.assertFalse(can_read_stored_message(self.approver, copy))

    def test_a_shared_mailbox_row_is_visible_to_readers_only_and_per_project(self):
        loose = normalize_message(raw_message(5, id="AAMk-shared", internetMessageId="<sh@x>"), "inbox")
        row, _ = store_message_metadata(self.shared_mailbox, loose)
        self.assertEqual(messages_visible_to(self.owner).count(), 1)             # can_read grant
        self.assertEqual(messages_visible_to(self.approver).count(), 0)          # none
        self.assertEqual(messages_visible_to(self.owner, project=self.project).count(), 0)  # unassigned
        MailboxProjectAssignment.objects.create(mailbox=self.shared_mailbox, project=self.project)
        self.assertEqual(messages_visible_to(self.owner, project=self.project).count(), 1)


# --- shared mailboxes --------------------------------------------------------------------------

def page(*messages, removed=(), next_link="", delta_link=""):
    return DeltaPage(messages=list(messages), removed_ids=list(removed), next_link=next_link, delta_link=delta_link)


class SharedDeltaSyncTests(SyncBase):
    def inbound(self, n, conversation=None, days_ago=1, **kw):
        return normalize_message(raw_message(
            n, id=f"AAMk-in-{n}", internetMessageId=f"<in-{n}@example.com>",
            conversationId=conversation or f"conv-in-{n}",
            receivedDateTime=iso(self.now - timedelta(days=days_ago)), **kw,
        ), "inbox")

    def test_the_first_run_is_bounded_stores_new_rows_and_saves_the_cursor_at_the_end(self):
        a, b = self.inbound(1), self.inbound(2)
        self.reader.delta["initial"] = page(a, b, delta_link="DELTA-1")
        result = self.shared()
        self.assertTrue(result.ok)
        self.assertEqual((result.stored, result.cursor_saved), (2, True))
        call = self.reader.of("delta")[0][2]
        self.assertEqual((call["since"], call["page_url"]), (self.now - timedelta(days=30), None))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")
        self.assertEqual(set(MailboxMessage.objects.values_list("state", flat=True)), {"new"})
        self.assertNoIncomingCorrespondence()

    def test_the_next_run_resumes_from_the_saved_cursor_without_a_new_first_read(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.reader.calls.clear()
        self.reader.delta["DELTA-1"] = page(self.inbound(2), delta_link="DELTA-2")
        result = self.shared()
        call = self.reader.of("delta")[0][2]
        self.assertEqual((call["since"], call["page_url"]), (None, "DELTA-1"))
        self.assertEqual((result.stored, get_sync_state(self.shared_mailbox).cursor), (1, "DELTA-2"))

    def test_pages_are_followed_and_only_the_final_delta_link_is_saved(self):
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(2), next_link="NEXT-2")
        self.reader.delta["NEXT-2"] = page(self.inbound(3), delta_link="DELTA-9")
        result = self.shared()
        self.assertEqual((result.pages, result.stored), (3, 3))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-9")

    def test_an_interrupted_run_resumes_at_the_next_unread_page_without_duplicates(self):
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(2), delta_link="DELTA-1")
        self.reader.errors["delta"] = [None, MailboxReadError("temporary")]
        failed = self.shared()
        self.assertEqual((failed.ok, failed.error_code, failed.cursor_saved), (False, "temporary", False))
        state = get_sync_state(self.shared_mailbox)
        # only a resume marker, never a final position
        self.assertEqual((state.cursor, state.last_error_code, state.last_synced_at), (self.marker("NEXT-1"), "temporary", None))
        self.assertEqual(MailboxMessage.objects.count(), 1)                    # first page kept
        self.reader.calls.clear()
        done = self.shared()
        self.assertTrue(done.ok)
        # the first page is NOT read again: the run starts at the marker
        self.assertEqual([(c[2]["since"], c[2]["page_url"]) for c in self.reader.of("delta")], [(None, "NEXT-1")])
        self.assertEqual(MailboxMessage.objects.count(), 2)                    # no duplicate
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    def test_a_crash_in_the_middle_of_a_page_replays_that_page_only_and_idempotently(self):
        corr, _, _ = self.sent_out(self.shared_mailbox, conversation="conv-1")
        reply = self.inbound(5, conversation="conv-1")
        self.answers_sent(reply)
        self.reader.errors["ident"] = [MailboxReadError("temporary")]
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(2), reply, delta_link="DELTA-1")
        self.assertEqual(self.shared().error_code, "temporary")
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker("NEXT-1"))
        stored_before = MailboxMessage.objects.filter(folder="inbox").count()   # page 1 + page 2 up to the crash
        self.assertGreaterEqual(stored_before, 2)
        self.assertTrue(self.shared().ok)
        self.assertEqual(MailboxMessage.objects.filter(folder="inbox").count(), 3)
        self.assertEqual(MailboxMessage.objects.get(graph_message_id=reply.graph_id).correspondence_id, corr.pk)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    def test_a_backlog_beyond_the_page_limit_is_read_over_several_runs_without_rereading(self):
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(2), next_link="NEXT-2")
        self.reader.delta["NEXT-2"] = page(self.inbound(3), delta_link="DELTA-3")
        first = self.shared(max_pages=1)
        # not an error and not finished: a resume marker, no final position, no completion time
        self.assertEqual((first.ok, first.incomplete, first.cursor_saved, first.error_code), (True, True, False, ""))
        state = get_sync_state(self.shared_mailbox)
        self.assertEqual((state.cursor, state.last_synced_at), (self.marker("NEXT-1"), None))
        second = self.shared(max_pages=1)
        self.assertEqual((second.ok, second.incomplete), (True, True))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker("NEXT-2"))
        third = self.shared(max_pages=1)
        self.assertEqual((third.ok, third.incomplete, third.cursor_saved), (True, False, True))
        state = get_sync_state(self.shared_mailbox)
        self.assertEqual(state.cursor, "DELTA-3")
        self.assertIsNotNone(state.last_synced_at)
        # every page was requested exactly once, in order
        self.assertEqual(
            [(c[2]["since"] is not None, c[2]["page_url"]) for c in self.reader.of("delta")],
            [(True, None), (False, "NEXT-1"), (False, "NEXT-2")],
        )
        self.assertEqual(MailboxMessage.objects.count(), 3)

    def test_a_resume_link_that_expired_falls_back_to_one_bounded_read(self):
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.shared(max_pages=1)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker("NEXT-1"))
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-FRESH")
        result = self.shared()
        self.assertTrue(result.ok and result.cursor_reset)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-FRESH")
        self.assertEqual(MailboxMessage.objects.count(), 1)

    def test_a_completed_position_followed_by_an_interrupted_round_resumes_from_the_marker(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.reader.calls.clear()
        self.reader.delta["DELTA-1"] = page(self.inbound(2), next_link="NEXT-A")
        self.reader.delta["NEXT-A"] = page(self.inbound(3), delta_link="DELTA-2")
        self.reader.errors["delta"] = [None, MailboxReadError("rate_limited", retry_after=5)]
        self.assertEqual(self.shared().error_code, "rate_limited")
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor,
            self.marker("NEXT-A", since=get_sync_state(self.shared_mailbox).last_synced_at - timedelta(hours=1)))
        self.assertTrue(self.shared().ok)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-2")
        self.assertEqual(MailboxMessage.objects.count(), 3)
    def test_an_expired_cursor_is_dropped_and_the_period_since_the_last_completed_round_is_read_once(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 10)
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        self.reader.delta["initial"] = page(self.inbound(1), self.inbound(2), delta_link="DELTA-2")
        result = self.shared()
        self.assertTrue(result.ok)
        self.assertTrue(result.cursor_reset)
        calls = [(c[2]["since"], c[2]["page_url"]) for c in self.reader.of("delta")]
        self.assertEqual(calls, [(None, "DELTA-1"), ((self.now - timedelta(days=10) - timedelta(hours=1)).replace(microsecond=0), None)])
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-2")
        self.assertEqual(MailboxMessage.objects.count(), 2)
    def test_a_second_expiry_is_reported_and_keeps_the_recovery_bound(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")] * 2
        result = self.shared()
        self.assertEqual((result.ok, result.error_code), (False, "sync_state_expired"))
        # no final position, and the 45-day bound is still remembered for the next attempt
        self.assertEqual(
            get_sync_state(self.shared_mailbox).cursor,
            self.marker(since=self.now - timedelta(days=45) - timedelta(hours=1)),
        )
    def test_rate_limiting_keeps_the_cursor_and_reports_the_delay(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.reader.errors["delta"] = [MailboxReadError("rate_limited", retry_after=30)]
        result = self.shared()
        self.assertEqual((result.ok, result.error_code, result.retry_after), (False, "rate_limited", 30))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    def test_a_last_page_without_a_delta_link_is_an_error_not_a_cursor(self):
        self.reader.delta["initial"] = page(self.inbound(1))
        result = self.shared()
        self.assertEqual((result.ok, result.error_code), (False, "invalid_response"))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "")

    def test_messages_older_than_the_bound_are_ignored_on_the_first_read(self):
        old = normalize_message(raw_message(
            4, id="AAMk-old", internetMessageId="<old@x>", receivedDateTime=iso(self.now - timedelta(days=40)),
        ), "inbox")
        self.reader.delta["initial"] = page(old, self.inbound(1), delta_link="D")
        self.assertEqual(self.shared().stored, 1)

    def test_removed_items_delete_only_untouched_review_rows(self):
        rows = {}
        for n, state, linked in ((1, "new", False), (2, "new", True), (3, "imported", False), (4, "ignored", False)):
            meta = self.inbound(n)
            row, _ = store_message_metadata(self.shared_mailbox, meta)
            update = {"state": state}
            if linked:
                corr, _, _ = self.sent_out(self.shared_mailbox, subject=f"s{n}", conversation=f"c{n}", internet_id=f"<s{n}@x>", n=10 + n)
                update["correspondence"] = corr
            MailboxMessage.objects.filter(pk=row.pk).update(**update)
            rows[n] = meta.graph_id
        self.reader.delta["initial"] = page(removed=list(rows.values()), delta_link="D")
        result = self.shared()
        self.assertEqual(result.removed, 1)
        remaining = set(MailboxMessage.objects.filter(folder="inbox").values_list("graph_message_id", flat=True))
        self.assertEqual(remaining, {rows[2], rows[3], rows[4]})

    def test_a_reply_to_a_linked_sent_message_is_linked_and_other_messages_never_get_headers_read(self):
        corr, _, _ = self.sent_out(self.shared_mailbox, conversation="conv-1")
        reply = self.inbound(5, conversation="conv-1")
        unrelated = self.inbound(6)
        self.answers_sent(reply)
        self.reader.delta["initial"] = page(reply, unrelated, delta_link="D")
        result = self.shared()
        self.assertEqual((result.stored, result.linked), (2, 1))
        self.assertEqual(MailboxMessage.objects.get(graph_message_id=reply.graph_id).correspondence_id, corr.pk)
        self.assertIsNone(MailboxMessage.objects.get(graph_message_id=unrelated.graph_id).correspondence_id)
        self.assertEqual([c[1] for c in self.reader.of("ident")], [reply.graph_id])
        self.assertNoIncomingCorrespondence()
        self.assertEqual(Correspondence.objects.get(pk=corr.pk).status, "ISSUED")

    def test_a_message_sent_from_the_shared_address_itself_is_not_stored(self):
        own = normalize_message(raw_message(
            7, id="AAMk-own", internetMessageId="<own@x>", receivedDateTime=iso(self.now - timedelta(hours=1)),
            **{"from": {"emailAddress": {"address": self.shared_mailbox.email_address}}},
        ), "inbox")
        self.reader.delta["initial"] = page(own, delta_link="D")
        result = self.shared()
        self.assertEqual((result.stored, result.own_ignored), (0, 1))

    def test_a_run_held_by_another_process_does_not_start_and_touches_nothing(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        other = self.hold_from_another_process(self.shared_mailbox)
        blocked = self.shared()
        self.assertEqual((blocked.ok, blocked.error_code), (False, "sync_in_progress"))
        self.assertEqual(self.reader.calls, [])
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "")
        other.release()
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "")
        self.assertTrue(self.shared().ok)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    def test_a_run_never_overwrites_a_position_another_run_moved(self):
        class MovedUnderneath(ScriptedReader):
            def delta_messages(inner, *args, **kwargs):
                MailboxSyncState.objects.filter(mailbox=self.shared_mailbox).update(cursor="DELTA-OTHER-RUN")
                return super().delta_messages(*args, **kwargs)

        self.reader = MovedUnderneath()
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-MINE")
        result = self.shared()
        self.assertEqual((result.ok, result.error_code, result.cursor_saved), (False, "cursor_conflict", False))
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-OTHER-RUN")
    # --- MAIL-1.3D-A3: recovery never depends on the 30-day window alone ---------------------------

    def test_a_valid_cursor_after_45_days_is_simply_resumed(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        self.reader.calls.clear()
        self.reader.delta["DELTA-1"] = page(self.inbound(2), delta_link="DELTA-2")
        self.assertTrue(self.shared().ok)
        self.assertEqual([(c[2]["since"], c[2]["page_url"]) for c in self.reader.of("delta")], [(None, "DELTA-1")])

    def test_an_expired_final_cursor_after_45_days_recovers_the_whole_unsynchronised_period(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        in_the_gap = self.inbound(2, days_ago=40)       # older than 30 days, newer than the last sync
        already_synced = self.inbound(3, days_ago=50)   # before the last completed round
        recent = self.inbound(4, days_ago=1)
        self.reader.delta["initial"] = page(in_the_gap, already_synced, recent, delta_link="DELTA-2")
        result = self.shared()
        self.assertTrue(result.ok and result.cursor_reset)
        read = [c[2]["since"] for c in self.reader.of("delta") if c[2]["since"] is not None]
        self.assertEqual(read, [(self.now - timedelta(days=45) - timedelta(hours=1)).replace(microsecond=0)])   # NOT now - 30 days
        stored = set(MailboxMessage.objects.values_list("graph_message_id", flat=True))
        self.assertIn(in_the_gap.graph_id, stored)
        self.assertIn(recent.graph_id, stored)
        self.assertNotIn(already_synced.graph_id, stored)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-2")

    def test_an_expired_resume_marker_after_45_days_recovers_from_the_last_completed_round(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        self.reader.delta["DELTA-1"] = page(self.inbound(2), next_link="NEXT-A")
        self.reader.errors["delta"] = [None, MailboxReadError("temporary")]
        self.assertFalse(self.shared().ok)
        bound = (self.now - timedelta(days=45) - timedelta(hours=1)).replace(microsecond=0)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker("NEXT-A", since=bound))
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        gap = self.inbound(5, days_ago=42)
        self.reader.delta["initial"] = page(gap, delta_link="DELTA-9")
        result = self.shared()
        self.assertTrue(result.ok and result.cursor_reset)
        self.assertEqual([c[2]["since"] for c in self.reader.of("delta") if c[2]["since"]], [bound])
        self.assertIn(gap.graph_id, set(MailboxMessage.objects.values_list("graph_message_id", flat=True)))

    def test_a_first_read_interrupted_for_45_days_keeps_its_original_bound(self):
        self.reader.delta["initial"] = page(self.inbound(1), next_link="NEXT-1")
        self.reader.errors["delta"] = [None, MailboxReadError("temporary")]
        self.reader.delta["NEXT-1"] = page(delta_link="X")
        original = self.now - timedelta(days=30)
        self.assertFalse(self.shared().ok)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker("NEXT-1", since=original))
        # 45 days later the resume link has expired
        self.now = self.now + timedelta(days=45)
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        in_original_scope = self.inbound(7, days_ago=60)    # 15 days after the first read started
        self.reader.delta["initial"] = page(in_original_scope, delta_link="DELTA-1")
        self.assertTrue(self.shared().ok)
        self.assertEqual(iso(self.reader.of("delta")[-1][2]["since"]), iso(original))
        self.assertIn(in_original_scope.graph_id, set(MailboxMessage.objects.values_list("graph_message_id", flat=True)))

    def test_a_reset_followed_by_a_crash_before_any_page_keeps_the_bound(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        bound = (self.now - timedelta(days=45) - timedelta(hours=1)).replace(microsecond=0)
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired"), MailboxReadError("temporary")]
        self.assertEqual(self.shared().error_code, "temporary")
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, self.marker(since=bound))
        self.reader.calls.clear()
        self.reader.delta["initial"] = page(self.inbound(2, days_ago=44), delta_link="DELTA-2")
        self.assertTrue(self.shared().ok)
        self.assertEqual([c[2]["since"] for c in self.reader.of("delta")], [bound])
        self.assertEqual(MailboxMessage.objects.filter(graph_message_id=self.inbound(2).graph_id).count(), 1)

    def test_a_marker_without_a_bound_still_resumes_and_falls_back_to_the_default_window(self):
        MailboxSyncState.objects.update_or_create(mailbox=self.shared_mailbox, defaults={"cursor": "pending:NEXT-1"})
        self.reader.delta["NEXT-1"] = page(self.inbound(1), delta_link="DELTA-1")
        self.assertTrue(self.shared().ok)
        self.assertEqual([(c[2]["since"], c[2]["page_url"]) for c in self.reader.of("delta")], [(None, "NEXT-1")])
        MailboxSyncState.objects.filter(mailbox=self.shared_mailbox).update(cursor="pending:NEXT-1")
        self.reader.calls.clear()
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        self.reader.delta["initial"] = page(delta_link="DELTA-2")
        self.assertTrue(self.shared().ok)
        # no completed round is recorded here beyond the one above: its end bounds the re-read
        self.assertEqual(len([c for c in self.reader.of("delta") if c[2]["since"]]), 1)

    # --- a saved position never runs ahead of the stored metadata ---------------------------------------

    def test_a_marker_is_written_only_after_every_message_of_its_page_is_stored(self):
        self.reader.delta["initial"] = page(self.inbound(1), self.inbound(2), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(3), self.inbound(4), delta_link="DELTA-1")
        seen = []
        original = _Cursor.save

        def spy(cursor, new, **kw):
            seen.append((new, MailboxMessage.objects.count()))
            return original(cursor, new, **kw)

        with patch.object(_Cursor, "save", spy):
            self.assertTrue(self.shared().ok)
        self.assertEqual(seen, [(self.marker("NEXT-1"), 2), ("DELTA-1", 4)])

    def test_a_crash_in_the_middle_of_a_page_leaves_the_position_behind_and_loses_nothing(self):
        self.reader.delta["initial"] = page(self.inbound(1), self.inbound(2), self.inbound(3), delta_link="DELTA-1")
        import userapis.mailbox_sync as sync_module
        real_store = sync_module.store_message_metadata
        calls = {"n": 0}

        def flaky(mailbox, meta):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("process killed")
            return real_store(mailbox, meta)

        with patch.object(sync_module, "store_message_metadata", flaky):
            with self.assertRaises(RuntimeError):
                self.shared()
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "")           # not advanced
        self.assertEqual(MailboxMessage.objects.count(), 1)                         # what was stored stays
        # the lock died with the failed run: a new one starts and finishes
        self.assertTrue(self.shared().ok)
        self.assertEqual(MailboxMessage.objects.count(), 3)                         # no loss, no duplicate
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    def test_a_crash_while_saving_the_marker_replays_the_page_without_duplicates(self):
        self.reader.delta["initial"] = page(self.inbound(1), self.inbound(2), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(self.inbound(3), delta_link="DELTA-1")
        original = _Cursor.save
        state = {"first": True}

        def dying(cursor, new, **kw):
            if state["first"]:
                state["first"] = False
                raise RuntimeError("process killed")
            return original(cursor, new, **kw)

        with patch.object(_Cursor, "save", dying):
            with self.assertRaises(RuntimeError):
                self.shared()
        self.assertEqual((get_sync_state(self.shared_mailbox).cursor, MailboxMessage.objects.count()), ("", 2))
        self.assertTrue(self.shared().ok)
        self.assertEqual(MailboxMessage.objects.count(), 3)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-1")

    # --- concurrency while recovering ---------------------------------------------------------------------

    def test_a_dead_process_releases_its_lock_and_another_recovers(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        self.backdate_sync(self.shared_mailbox, 45)
        dead = self.hold_from_another_process(self.shared_mailbox)
        self.reader.calls.clear()
        self.assertEqual(self.shared().error_code, "sync_in_progress")             # a live holder blocks
        self.assertEqual(self.reader.calls, [])
        dead.conn.close()                                                           # the process dies
        self.reader.errors["delta"] = [MailboxReadError("sync_state_expired")]
        self.reader.delta["initial"] = page(self.inbound(2, days_ago=40), delta_link="DELTA-2")
        self.assertTrue(self.shared().ok)
        self.assertEqual(get_sync_state(self.shared_mailbox).cursor, "DELTA-2")

    def test_a_recovery_cannot_overwrite_a_position_another_run_moved(self):
        self.reader.delta["initial"] = page(self.inbound(1), delta_link="DELTA-1")
        self.shared()
        mailbox = self.shared_mailbox

        class MovedThenExpired(ScriptedReader):
            def delta_messages(inner, *args, **kwargs):
                MailboxSyncState.objects.filter(mailbox=mailbox).update(cursor="DELTA-OTHER-RUN")
                raise MailboxReadError("sync_state_expired")

        self.reader = MovedThenExpired()
        result = self.shared()
        self.assertEqual((result.ok, result.error_code), (False, "cursor_conflict"))
        self.assertEqual(get_sync_state(mailbox).cursor, "DELTA-OTHER-RUN")

    # --- removals ---------------------------------------------------------------------------------------------

    def test_a_removal_on_a_resumed_page_is_applied_once_and_replays_harmlessly(self):
        keep, gone = self.inbound(1), self.inbound(2)
        for meta in (keep, gone):
            store_message_metadata(self.shared_mailbox, meta)
        self.reader.delta["initial"] = page(self.inbound(3), next_link="NEXT-1")
        self.reader.delta["NEXT-1"] = page(removed=[gone.graph_id, "never-stored"], delta_link="DELTA-1")
        self.reader.errors["delta"] = [None, MailboxReadError("temporary")]
        self.assertFalse(self.shared().ok)
        self.assertTrue(self.shared().ok)
        ids = set(MailboxMessage.objects.values_list("graph_message_id", flat=True))
        self.assertEqual(ids, {keep.graph_id, self.inbound(3).graph_id})
        # the same removal delivered again (a replayed page) changes nothing
        MailboxSyncState.objects.filter(mailbox=self.shared_mailbox).update(cursor="DELTA-1")
        self.reader.delta["DELTA-1"] = page(removed=[gone.graph_id], delta_link="DELTA-2")
        self.assertTrue(self.shared().ok)
        self.assertEqual(MailboxMessage.objects.count(), 2)



# --- the real reader's delta contract (HTTP level, patched requests) ---------------------------

class DeltaReaderHttpTests(SyncBase):
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph(me_address="owner@links-local.invalid")
        for target, kwargs in (
            ("userapis.mailbox_read.requests.get", {"side_effect": self.graph}),
            ("userapis.mailbox_read.refresh_outlook_access_token", {"return_value": "RENEWED"}),
        ):
            patcher = patch(target, **kwargs)
            self.mocks = getattr(self, "mocks", {})
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    def shared_reader(self):
        return MailboxGraphReader(self.shared_mailbox, actor=self.delegate)

    def test_the_first_request_is_a_bounded_metadata_only_delta_read(self):
        self.graph.list_payload = {"value": [raw_message(1)], "@odata.deltaLink": f"{BASE}/users/shared%40links-local.invalid/mailFolders/inbox/messages/delta?$deltatoken=T"}
        got = self.shared_reader().delta_messages(since=self.now - timedelta(days=30))
        call = self.graph.message_calls[0]
        self.assertTrue(call["url"].endswith("/mailFolders/inbox/messages/delta"))
        self.assertTrue(call["params"]["$filter"].startswith("receivedDateTime ge "))
        self.assertNotIn("body", call["params"]["$select"].lower().replace("hasattachments", ""))
        self.assertIn("odata.maxpagesize=", call["headers"]["Prefer"])
        self.assertIn("ImmutableId", call["headers"]["Prefer"])
        self.assertTrue(got.delta_link)
        # whatever else Graph returned is dropped
        self.assertNotIn(SENTINEL_BODY, repr(got))

    def test_a_first_request_without_a_bound_is_refused(self):
        with self.assertRaises(MailboxReadError) as ctx:
            self.shared_reader().delta_messages()
        self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.graph.message_calls, [])

    def test_removed_items_come_back_as_ids_only(self):
        self.graph.list_payload = {"value": [
            {"id": "AAMk-gone", "@removed": {"reason": "deleted"}}, raw_message(2),
        ]}
        got = self.shared_reader().delta_messages(since=self.now - timedelta(days=1))
        self.assertEqual((got.removed_ids, [m.graph_id for m in got.messages]), (["AAMk-gone"], ["AAMk-immutable-2"]))

    def test_a_link_outside_the_mailbox_context_or_not_a_delta_url_is_never_followed(self):
        reader = self.shared_reader()
        for url in (
            "https://evil.example.com/v1.0/users/shared%40links-local.invalid/mailFolders/inbox/messages/delta",
            f"{BASE}/users/someone-else%40example.com/mailFolders/inbox/messages/delta?x=1",
            f"{BASE}/users/shared%40links-local.invalid/messages",
        ):
            with self.assertRaises(MailboxReadError) as ctx:
                reader.delta_messages(page_url=url)
            self.assertEqual(ctx.exception.code, "invalid_request")
        self.assertEqual(self.graph.message_calls, [])

    def test_graphs_own_spelling_of_a_user_link_is_accepted(self):
        self.graph.forced = FakeResponse(200, {"value": [], "@odata.deltaLink": f"{BASE}/users('shared@links-local.invalid')/mailFolders('inbox')/messages/delta?$deltatoken=N"})
        got = self.shared_reader().delta_messages(
            page_url=f"{BASE}/users('shared@links-local.invalid')/mailFolders('inbox')/messages/delta?$deltatoken=T"
        )
        self.assertTrue(got.delta_link)

    def test_http_410_is_an_expired_cursor_and_429_keeps_its_delay(self):
        reader = self.shared_reader()
        link = f"{BASE}/users/shared%40links-local.invalid/mailFolders/inbox/messages/delta?$deltatoken=T"
        self.graph.forced = FakeResponse(410, {})
        with self.assertRaises(MailboxReadError) as ctx:
            reader.delta_messages(page_url=link)
        self.assertEqual((ctx.exception.code, ctx.exception.retryable), ("sync_state_expired", False))
        self.graph.forced = FakeResponse(429, {}, headers={"Retry-After": "17"})
        with self.assertRaises(MailboxReadError) as ctx:
            reader.delta_messages(page_url=link)
        self.assertEqual((ctx.exception.code, ctx.exception.retry_after), ("rate_limited", 17))

    def test_a_full_personal_run_renews_the_token_reads_only_and_stores_no_body(self):
        corr, _, _ = self.sent_out(self.individual_mailbox)
        # the stored token has expired by now: it is renewed once (simulated), the identity is
        # checked, then GETs only
        UserOutlookCredentials.objects.filter(user=self.owner).update(
            outlook_token_expires=timezone.now() - timedelta(minutes=5)
        )
        reply_raw = raw_message(
            7, id="AAMk-reply-7", internetMessageId="<reply-7@example.com>", conversationId="conv-1",
            receivedDateTime=iso(self.now - timedelta(minutes=2)),
        )
        self.graph.list_payload = {"value": [reply_raw]}
        self.graph.item_payload = {
            "internetMessageId": "<reply-7@example.com>", "conversationId": "conv-1",
            "internetMessageHeaders": [
                {"name": "In-Reply-To", "value": SENT_ID}, {"name": "References", "value": f"<root@x> {SENT_ID}"},
                {"name": "X-Secret-Header", "value": SENTINEL_BODY},
            ],
        }
        with patch("userapis.mailbox_read.requests.post", side_effect=AssertionError("no write")) as post:
            result = sync_personal_replies(self.individual_mailbox, actor=self.owner, now=self.now)
        self.assertTrue(result.ok, result.error_code)
        self.assertEqual(result.linked, 1)
        self.mocks["userapis.mailbox_read.refresh_outlook_access_token"].assert_called_once()
        post.assert_not_called()
        row = MailboxMessage.objects.get(graph_message_id="AAMk-reply-7")
        self.assertEqual(row.correspondence_id, corr.pk)
        stored = " ".join(str(getattr(row, f.name)) for f in MailboxMessage._meta.fields)
        self.assertNotIn(SENTINEL_BODY, stored)
        # every inbox listing was for one exact conversation
        listings = [c for c in self.graph.message_calls if c["url"].endswith("/mailFolders/inbox/messages")]
        self.assertEqual(len(listings), 1)
        self.assertIn("conversationId eq 'conv-1'", listings[0]["params"]["$filter"])
