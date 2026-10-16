# MAIL-1.3B: the read-only Graph client and the metadata storage. Microsoft Graph is always
# simulated (requests.get is patched); every address, subject and id below is fictitious.
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import quote

import requests
from django.test import TestCase
from django.utils import timezone

from models.models import UserOutlookCredentials
from userapis.mailbox_messages import (
    can_read_stored_message,
    get_sync_state,
    messages_visible_to,
    record_sync_error,
    record_sync_success,
    store_message_metadata,
)
from userapis.mailbox_read import (
    ERROR_MESSAGES,
    MailboxGraphReader,
    MailboxReadError,
    normalize_message,
)
from userapis.models import (
    MailboxConnection,
    MailboxMessage,
    MailboxProjectAssignment,
    MailboxSyncState,
    MailboxUserGrant,
)
from userapis.test_mailbox import _make_company, _make_project, _make_user

SENTINEL_BODY = "TOP-SECRET-BODY-TEXT"
BASE = "https://graph.microsoft.com/v1.0"
ME_URL_PREFIX = f"{BASE}/me"


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._bad_json = bad_json
        self.text = ""

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


def raw_message(n=1, **over):
    message = {
        "id": f"AAMk-immutable-{n}",
        "internetMessageId": f"<msg{n}@example.com>",
        "conversationId": f"conv-{n}",
        "receivedDateTime": "2026-10-10T08:30:00Z",
        "subject": f"Fictitious subject {n}",
        "hasAttachments": True,
        "from": {"emailAddress": {"name": "Alice Test", "address": "Alice@Example.com"}},
        "toRecipients": [{"emailAddress": {"address": "owner@example.com"}}],
        "ccRecipients": [{"emailAddress": {"address": "cc@example.com"}}],
        # Things that must NEVER be kept, even if Graph returned them:
        "body": {"contentType": "html", "content": SENTINEL_BODY},
        "bodyPreview": SENTINEL_BODY,
        "attachments": [{"name": "secret.pdf", "contentBytes": SENTINEL_BODY}],
    }
    message.update(over)
    return message


class FakeGraph:
    """Routes the reader's GETs. `me_address` is who the token claims to be."""

    def __init__(self, me_address="owner@example.com"):
        self.me_address = me_address
        self.calls = []
        self.list_payload = {"value": [raw_message(1), raw_message(2)]}
        self.item_payload = raw_message(1)
        self.forced = None  # a FakeResponse (or exception) returned for non-/me requests

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params, "headers": headers or {}})
        if url.startswith(f"{BASE}/me?"):
            return FakeResponse(200, {"mail": self.me_address, "userPrincipalName": self.me_address})
        if self.forced is not None:
            if isinstance(self.forced, Exception):
                raise self.forced
            return self.forced
        if "/mailFolders/" in url:
            return FakeResponse(200, self.list_payload)
        return FakeResponse(200, self.item_payload)

    @property
    def message_calls(self):
        return [c for c in self.calls if not c["url"].startswith(f"{BASE}/me?")]


class MailboxReadBase(TestCase):
    def setUp(self):
        self.company = _make_company("Acme")
        self.owner = _make_user("owner@example.com", self.company)
        self.delegate = _make_user("delegate@example.com", self.company)
        self.reader_user = _make_user("reader@example.com", self.company)
        self.sender_only = _make_user("sendonly@example.com", self.company)
        self.stranger = _make_user("stranger@example.com", self.company)
        self.foreign = _make_user("foreign@example.com", _make_company("Other"))
        self.project = _make_project(self.company)
        self.other_project = _make_project(self.company)
        for user, token in ((self.owner, "OWNER-TOKEN"), (self.delegate, "DELEGATE-TOKEN")):
            self.credentials(user, token)
        self.personal = MailboxConnection.objects.create(
            provider="microsoft", connection_type="individual", email_address="owner@example.com",
            company_profile=self.company, owner_user=self.owner, connected_by=self.owner,
            status="active",
        )
        self.shared = MailboxConnection.objects.create(
            provider="microsoft", connection_type="shared", email_address="info@example.com",
            company_profile=self.company, connected_by=self.delegate, status="active",
        )
        MailboxUserGrant.objects.create(
            mailbox=self.shared, user=self.reader_user, can_read=True, can_send=False
        )
        MailboxUserGrant.objects.create(
            mailbox=self.shared, user=self.sender_only, can_read=False, can_send=True
        )
        self.graph = FakeGraph()
        patcher = patch("userapis.mailbox_read.requests.get", side_effect=self.graph)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No test may ever reach Microsoft's token endpoint: a renewal is always simulated.
        refresher = patch(
            "userapis.mailbox_read.refresh_outlook_access_token", return_value="REFRESHED-TOKEN"
        )
        self.refresh = refresher.start()
        self.addCleanup(refresher.stop)

    def credentials(self, user, token, *, expires_in=3600, refresh="REFRESH"):
        return UserOutlookCredentials.objects.update_or_create(
            user=user,
            defaults={
                "outlook_access_token": token,
                "outlook_refresh_token": refresh,
                "outlook_token_expires": timezone.now() + timedelta(seconds=expires_in),
            },
        )[0]

    def reader(self, mailbox, user, **kw):
        return MailboxGraphReader(mailbox, actor=user, **kw)

    def assertCode(self, code, call):
        with self.assertRaises(MailboxReadError) as ctx:
            call()
        self.assertEqual(ctx.exception.code, code)
        return ctx.exception


class PersonalAndSharedReadTests(MailboxReadBase):
    def test_owner_reads_the_metadata_of_a_personal_mailbox_through_me(self):
        page = self.reader(self.personal, self.owner).list_messages("inbox")
        self.assertEqual(len(page.messages), 2)
        first = page.messages[0]
        self.assertEqual(first.graph_id, "AAMk-immutable-1")
        self.assertEqual(first.internet_message_id, "<msg1@example.com>")
        self.assertEqual(first.conversation_id, "conv-1")
        self.assertEqual(first.sender_address, "alice@example.com")
        self.assertEqual(first.to_addresses, ("owner@example.com",))
        self.assertEqual(first.cc_addresses, ("cc@example.com",))
        self.assertTrue(first.has_attachments)
        self.assertEqual(first.received_at.year, 2026)
        urls = [c["url"] for c in self.graph.message_calls]
        self.assertEqual(urls, [f"{ME_URL_PREFIX}/mailFolders/inbox/messages"])
        self.assertTrue(self.graph.calls[0]["url"].startswith(f"{BASE}/me?"))  # identity probe
        self.assertEqual(self.graph.calls[0]["headers"]["Authorization"], "Bearer OWNER-TOKEN")

    def test_another_user_cannot_read_a_personal_mailbox(self):
        for user in (self.stranger, self.reader_user, self.delegate, self.foreign):
            self.assertCode("not_authorized", lambda u=user: self.reader(self.personal, u))
        self.assertEqual(self.graph.calls, [])  # not a single Graph request was made

    def test_shared_mailbox_is_read_with_can_read_through_users_address_and_the_delegate_token(self):
        self.graph.me_address = "unused@example.com"
        reader = self.reader(self.shared, self.reader_user)
        reader.list_messages("sentitems")
        call = self.graph.calls[-1]
        self.assertEqual(
            call["url"], f"{BASE}/users/{quote('info@example.com', safe='')}/mailFolders/sentitems/messages"
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer DELEGATE-TOKEN")
        # no identity probe for a shared mailbox (it is read by address)
        self.assertEqual(len(self.graph.calls), 1)

    def test_shared_mailbox_without_can_read_is_refused(self):
        for user in (self.sender_only, self.stranger, self.foreign):
            self.assertCode("not_authorized", lambda u=user: self.reader(self.shared, u))
        self.assertEqual(self.graph.calls, [])

    def test_a_revoked_can_read_stops_the_reading(self):
        MailboxUserGrant.objects.filter(mailbox=self.shared, user=self.reader_user).update(can_read=False)
        self.assertCode("not_authorized", lambda: self.reader(self.shared, self.reader_user))

    def test_company_isolation_for_readers_and_visibility(self):
        self.assertCode("not_authorized", lambda: self.reader(self.shared, self.foreign))
        row, _ = store_message_metadata(self.shared, normalize_message(raw_message(1), "inbox"))
        self.assertFalse(messages_visible_to(self.foreign).exists())
        self.assertFalse(can_read_stored_message(self.foreign, row))

    def test_project_assignment_is_respected_in_a_project_context(self):
        MailboxProjectAssignment.objects.create(mailbox=self.shared, project=self.project)
        self.assertCode(
            "not_authorized",
            lambda: self.reader(self.shared, self.reader_user, project=self.other_project),
        )
        self.reader(self.shared, self.reader_user, project=self.project).list_messages()
        row, _ = store_message_metadata(self.shared, normalize_message(raw_message(1), "inbox"))
        self.assertTrue(messages_visible_to(self.reader_user, project=self.project).exists())
        self.assertFalse(messages_visible_to(self.reader_user, project=self.other_project).exists())
        self.assertTrue(can_read_stored_message(self.reader_user, row, project=self.project))
        self.assertFalse(can_read_stored_message(self.reader_user, row, project=self.other_project))

    def test_an_inactive_mailbox_is_refused_and_only_an_authorized_user_learns_why(self):
        MailboxConnection.objects.filter(pk=self.personal.pk).update(status="revoked")
        self.personal.refresh_from_db()
        self.assertCode("mailbox_inactive", lambda: self.reader(self.personal, self.owner))
        self.assertCode("not_authorized", lambda: self.reader(self.personal, self.stranger))

    def test_a_personal_mailbox_whose_token_belongs_to_another_account_is_refused(self):
        self.graph.me_address = "someone.else@example.com"
        self.assertCode(
            "identity_mismatch", lambda: self.reader(self.personal, self.owner).list_messages()
        )
        self.assertEqual(self.graph.message_calls, [])

    def test_get_message_and_reply_identification_keep_only_threading_headers(self):
        self.graph.item_payload = {
            "internetMessageId": "<reply1@example.com>",
            "conversationId": "conv-9",
            "internetMessageHeaders": [
                {"name": "Message-ID", "value": "<reply1@example.com>"},
                {"name": "In-Reply-To", "value": "<sent1@example.com>"},
                {"name": "References", "value": "<a@example.com> <sent1@example.com>"},
                {"name": "Received", "value": "from secret-relay.internal"},
                {"name": "X-Originating-IP", "value": "10.1.2.3"},
            ],
        }
        ident = self.reader(self.personal, self.owner).get_reply_identification("AAMk-1")
        self.assertEqual(ident.in_reply_to, "<sent1@example.com>")
        self.assertEqual(ident.references, ("<a@example.com>", "<sent1@example.com>"))
        self.assertEqual(ident.internet_message_id, "<reply1@example.com>")
        self.assertNotIn("secret-relay", repr(ident))
        self.assertNotIn("10.1.2.3", repr(ident))
        self.graph.item_payload = raw_message(3)
        self.assertEqual(self.reader(self.personal, self.owner).get_message("AAMk-3").graph_id, "AAMk-immutable-3")

    def test_only_get_is_ever_issued_and_never_a_body_or_attachment_requested(self):
        with patch("requests.post") as post, patch("requests.put") as put, \
                patch("requests.patch") as patch_, patch("requests.delete") as delete:
            reader = self.reader(self.personal, self.owner)
            reader.list_messages()
            reader.get_message("AAMk-1")
            reader.get_reply_identification("AAMk-1")
        for verb in (post, put, patch_, delete):
            verb.assert_not_called()
        for call in self.graph.message_calls:
            fields = {f.strip().lower() for f in (call["params"] or {}).get("$select", "").split(",")}
            self.assertFalse(fields & {"body", "bodypreview", "uniquebody", "attachments"})

    def test_only_allowed_folders_and_the_mailboxs_own_next_links_are_accepted(self):
        reader = self.reader(self.personal, self.owner)
        self.assertCode("invalid_request", lambda: reader.list_messages("drafts"))
        self.assertCode("invalid_request", lambda: reader.list_messages("inbox", page_url="https://evil.example/x"))
        self.assertCode(
            "invalid_request",
            lambda: reader.list_messages(
                "inbox", page_url=f"{BASE}/users/info%40example.com/mailFolders/inbox/messages?$skip=10"
            ),
        )
        self.graph.list_payload = {
            "value": [], "@odata.nextLink": f"{ME_URL_PREFIX}/mailFolders/inbox/messages?$skip=50",
        }
        page = reader.list_messages("inbox")
        self.assertEqual(page.next_link, f"{ME_URL_PREFIX}/mailFolders/inbox/messages?$skip=50")
        reader.list_messages("inbox", page_url=page.next_link)
        self.graph.list_payload = {"value": [], "@odata.nextLink": "https://evil.example/steal"}
        self.assertCode("invalid_request", lambda: reader.list_messages("inbox"))


class StorageTests(MailboxReadBase):
    def meta(self, n=1, **over):
        return normalize_message(raw_message(n, **over), "inbox")

    def test_storing_the_same_message_twice_keeps_one_row(self):
        first, created = store_message_metadata(self.personal, self.meta())
        again, created_again = store_message_metadata(self.personal, self.meta())
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(MailboxMessage.objects.count(), 1)
        self.assertEqual(first.company_profile_id, self.company.id)
        self.assertEqual(first.state, MailboxMessage.ProcessingState.NEW)

    def test_the_same_internet_message_under_another_graph_id_is_one_row_and_decisions_survive(self):
        row, _ = store_message_metadata(self.personal, self.meta())
        MailboxMessage.objects.filter(pk=row.pk).update(state="imported")
        other_copy = normalize_message(raw_message(1, id="AAMk-sent-copy"), "sentitems")
        same, created = store_message_metadata(self.personal, other_copy)
        self.assertFalse(created)
        self.assertEqual(same.pk, row.pk)
        same.refresh_from_db()
        self.assertEqual(same.state, "imported")  # a re-read never rewrites a decision
        self.assertEqual(same.folder, "inbox")  # first provenance kept
        self.assertEqual(MailboxMessage.objects.count(), 1)

    def test_missing_internet_message_id_is_handled_without_a_placeholder(self):
        a = normalize_message(raw_message(1, internetMessageId=None), "inbox")
        b = normalize_message(raw_message(2, internetMessageId=None), "inbox")
        self.assertEqual(a.internet_message_id, "")
        row_a, _ = store_message_metadata(self.personal, a)
        row_b, _ = store_message_metadata(self.personal, b)
        self.assertNotEqual(row_a.pk, row_b.pk)
        self.assertEqual(row_a.internet_message_id, "")
        self.assertEqual(store_message_metadata(self.personal, a)[1], False)
        self.assertEqual(MailboxMessage.objects.count(), 2)

    def test_sparse_graph_messages_are_normalized_safely(self):
        meta = normalize_message({"id": "only-id"}, "inbox")
        self.assertEqual((meta.subject, meta.sender_address, meta.to_addresses), ("", "", ()))
        self.assertIsNone(meta.received_at)
        self.assertIsNone(normalize_message({"subject": "no id"}))
        self.assertIsNone(normalize_message("not a dict"))
        row, _ = store_message_metadata(self.personal, meta)
        self.assertEqual(row.to_addresses, [])

    def test_the_same_message_in_two_mailboxes_is_two_rows_with_their_own_rights(self):
        mine, _ = store_message_metadata(self.personal, self.meta())
        shared, _ = store_message_metadata(self.shared, self.meta())
        self.assertNotEqual(mine.pk, shared.pk)
        self.assertEqual(mine.internet_message_id, shared.internet_message_id)
        # MAIL-1.3D-A: a personal mailbox shows only rows linked to a LINKS correspondence
        self.assertEqual(list(messages_visible_to(self.owner)), [])
        self.assertFalse(can_read_stored_message(self.owner, mine))
        self.assertEqual(list(messages_visible_to(self.reader_user)), [shared])
        self.assertFalse(messages_visible_to(self.stranger).exists())
        self.assertFalse(messages_visible_to(self.sender_only).exists())
        self.assertFalse(can_read_stored_message(self.owner, shared))
        self.assertFalse(can_read_stored_message(self.reader_user, mine))
        # a revoked grant hides the shared copy without touching the personal one
        MailboxUserGrant.objects.filter(mailbox=self.shared, user=self.reader_user).update(can_read=False)
        self.assertFalse(messages_visible_to(self.reader_user).exists())
        self.assertEqual(list(messages_visible_to(self.owner)), [])

    def test_visibility_is_exactly_the_mailbox_read_rule(self):
        store_message_metadata(self.personal, self.meta(1))
        store_message_metadata(self.shared, self.meta(2))
        rows = list(MailboxMessage.objects.all())
        for user in (self.owner, self.delegate, self.reader_user, self.sender_only, self.stranger, self.foreign):
            visible = set(messages_visible_to(user).values_list("pk", flat=True))
            allowed = {r.pk for r in rows if can_read_stored_message(user, r)}
            self.assertEqual(visible, allowed, user.email)

    def test_nothing_but_metadata_is_stored(self):
        field_names = {f.name for f in MailboxMessage._meta.get_fields()}
        for forbidden in ("body", "body_preview", "html", "content", "attachments", "token"):
            self.assertNotIn(forbidden, field_names)
        row, _ = store_message_metadata(self.personal, self.meta())
        dumped = " ".join(
            str(getattr(row, f.name)) for f in MailboxMessage._meta.concrete_fields
        )
        self.assertNotIn(SENTINEL_BODY, dumped)
        self.assertNotIn("secret.pdf", dumped)
        self.assertTrue(row.has_attachments)  # the flag only

    def test_a_message_cannot_claim_another_companys_mailbox(self):
        other = _make_company("Elsewhere")
        from django.core.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            MailboxMessage.objects.create(
                mailbox=self.personal, company_profile=other, graph_message_id="x",
            )


class ErrorAndSyncStateTests(MailboxReadBase):
    def read(self):
        return lambda: self.reader(self.shared, self.reader_user).list_messages()

    def test_graph_errors_are_mapped_and_carry_only_generic_text(self):
        cases = (
            (FakeResponse(403, {"error": {"code": "ErrorAccessDenied", "message": "info@example.com denied"}}), "access_denied"),
            (FakeResponse(404, {"error": {"code": "ErrorItemNotFound"}}), "not_found"),
            (FakeResponse(429, {}, headers={"Retry-After": "30"}), "rate_limited"),
            (FakeResponse(503, {}), "temporary"),
            (FakeResponse(400, {}), "request_rejected"),
            (FakeResponse(200, None, bad_json=True), "invalid_response"),
            (FakeResponse(200, {"value": "oops"}), "invalid_response"),
            (FakeResponse(200, ["not", "a", "dict"]), "invalid_response"),
            (requests.ConnectionError("dns for info@example.com failed"), "temporary"),
            (requests.Timeout("slow"), "temporary"),
        )
        for forced, code in cases:
            self.graph.forced = forced
            error = self.assertCode(code, self.read())
            self.assertIn(str(error), ERROR_MESSAGES.values())
            self.assertNotIn("example.com", str(error))
        self.graph.forced = FakeResponse(429, {}, headers={"Retry-After": "30"})
        limited = self.assertCode("rate_limited", self.read())
        self.assertEqual((limited.retryable, limited.retry_after), (True, 30))
        self.graph.forced = FakeResponse(503, {})
        self.assertTrue(self.assertCode("temporary", self.read()).retryable)
        self.graph.forced = FakeResponse(403, {})
        self.assertFalse(self.assertCode("access_denied", self.read()).retryable)

    def test_an_expired_token_is_renewed_once_with_the_mail_scopes(self):
        self.credentials(self.delegate, "STALE", expires_in=-10)
        with patch("userapis.mailbox_read.refresh_outlook_access_token", return_value="NEW-TOKEN") as refresh:
            reader = self.reader(self.shared, self.reader_user)
            reader.list_messages()
            reader.list_messages()
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.graph.calls[0]["headers"]["Authorization"], "Bearer NEW-TOKEN")
        from ecpmpapp.constants import MS_MAIL_SCOPES
        self.assertEqual(refresh.call_args.kwargs["scope"], MS_MAIL_SCOPES)

    def test_a_cached_token_rejected_once_is_renewed_then_retried_but_never_looped(self):
        outcomes = [FakeResponse(401, {}), FakeResponse(200, {"value": []})]
        self.graph.forced = None
        original = self.graph.__call__

        def scripted(url, **kw):
            self.graph.calls.append({"url": url, "params": kw.get("params"), "headers": kw.get("headers") or {}})
            return outcomes.pop(0)

        with patch("userapis.mailbox_read.requests.get", side_effect=scripted), \
                patch("userapis.mailbox_read.refresh_outlook_access_token", return_value="RENEWED") as refresh:
            page = self.reader(self.shared, self.reader_user).list_messages()
        self.assertEqual(page.messages, [])
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(self.graph.calls[-1]["headers"]["Authorization"], "Bearer RENEWED")
        # a token that keeps being rejected ends in token_rejected after ONE renewal
        with patch("userapis.mailbox_read.requests.get", return_value=FakeResponse(401, {})), \
                patch("userapis.mailbox_read.refresh_outlook_access_token", return_value="RENEWED") as refresh:
            self.assertCode("token_rejected", self.read())
        self.assertEqual(refresh.call_count, 1)

    def test_a_failed_renewal_and_a_missing_connection_are_reported_not_hidden(self):
        self.credentials(self.delegate, "STALE", expires_in=-10)
        with patch("userapis.mailbox_read.refresh_outlook_access_token", return_value=None):
            self.assertCode("token_refresh_failed", self.read())
        UserOutlookCredentials.objects.filter(user=self.delegate).delete()
        self.assertCode("token_unavailable", self.read())
        MailboxConnection.objects.filter(pk=self.shared.pk).update(connected_by=None)
        self.shared.refresh_from_db()
        self.assertCode("token_unavailable", self.read())

    def test_errors_never_change_the_mailbox_status(self):
        self.graph.forced = FakeResponse(403, {})
        self.assertCode("access_denied", self.read())
        self.graph.forced = FakeResponse(401, {})
        with patch("userapis.mailbox_read.refresh_outlook_access_token", return_value="x"):
            self.assertCode("token_rejected", self.read())
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.status, "active")

    def test_sync_state_records_a_cursor_and_only_an_error_code(self):
        state = record_sync_success(self.shared, cursor="opaque-delta-token")
        self.assertEqual((state.cursor, state.last_error_code), ("opaque-delta-token", ""))
        self.assertIsNotNone(state.last_synced_at)
        error = MailboxReadError("rate_limited", retry_after=5)
        state = record_sync_error(self.shared, error)
        self.assertEqual(state.last_error_code, "rate_limited")
        self.assertNotIn(ERROR_MESSAGES["rate_limited"], str(state.__dict__))
        self.assertEqual(state.cursor, "opaque-delta-token")  # an error never loses the cursor
        state = record_sync_success(self.shared)
        self.assertEqual((state.last_error_code, state.last_error_at), ("", None))
        self.assertEqual(MailboxSyncState.objects.filter(mailbox=self.shared).count(), 1)
        self.assertEqual(get_sync_state(self.shared).pk, state.pk)
        self.shared.refresh_from_db()
        self.assertEqual(self.shared.status, "active")

    def test_reading_leaves_the_send_engine_and_its_evidence_untouched(self):
        from project_management.models import CorrespondenceSendEvent
        self.reader(self.personal, self.owner).list_messages()
        self.assertEqual(CorrespondenceSendEvent.objects.count(), 0)
        import userapis.mailbox_read as reading
        import userapis.mailbox_send as sending
        self.assertFalse(hasattr(reading, "send_via_graph"))
        self.assertTrue(callable(sending.send_via_graph))
