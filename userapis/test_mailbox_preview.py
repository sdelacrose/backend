"""MAIL-1.3D-C2-B1: previewing one selected Outlook message. Graph is a fake (patched GET); every
write verb is patched to raise; nothing may be stored, downloaded, sent or changed."""

import re
import time
from unittest.mock import patch
from urllib.parse import unquote

from django.core import mail
from rest_framework.test import APIClient

from models.models import UserOutlookCredentials
from project_management.models import Correspondence
from userapis.mailbox_preview import (
    MAX_BODY_BYTES, MAX_FILE_BYTES, InvalidAttachmentReference, attachment_policy,
    build_attachment_reference, read_attachment_reference, truncate_utf8,
)
from userapis.mailbox_read import GraphAttachmentMeta, html_to_text
from userapis.models import MailboxConnection, MailboxMessage, MailboxProjectAssignment, MailboxSyncState, MailboxUserGrant
from userapis.test_mailbox import _make_project
from userapis.test_mailbox_import import BASE, ImportBase, ImportGraph
from userapis.test_mailbox_read import SENTINEL_BODY, FakeResponse
from userapis.test_mailbox_sync import iso

URL = "/api/user/mailbox-preview/"
ATTACHMENT_FIELDS = {"reference", "name", "size", "content_type", "kind", "is_inline", "importable", "reason"}
RAW_ATTACHMENT_ID = "AAMk-RAW-ATTACHMENT-ID"


class PreviewGraph(ImportGraph):
    """ImportGraph plus the attachments listing; records the headers each GET carried."""

    def prepare(self):
        self.attachments = {}            # message graph id -> [raw attachment dicts]
        self.attachment_pages = []       # explicit payloads, consumed in order
        self.attachments_forced = None
        self.headers_log = []
        return self

    def __call__(self, url, params=None, headers=None, timeout=None, **kwargs):
        path = url.split("?")[0]
        if not url.startswith(f"{BASE}/me?"):
            self.headers_log.append({"path": path, "headers": dict(headers or {}), "params": dict(params or {})})
        listing = re.search(r"/messages/([^/]+)/attachments$", path)
        if listing and not url.startswith(f"{BASE}/me?"):
            self.calls.append({"url": url, "params": dict(params or {})})
            if self.attachments_forced is not None:
                return self.attachments_forced
            if self.attachment_pages:
                return FakeResponse(200, self.attachment_pages.pop(0))
            return FakeResponse(200, {"value": self.attachments.get(unquote(listing.group(1)), [])})
        return super().__call__(url, params, headers, timeout, **kwargs)


def attachment(n, kind="file", **over):
    raw = {
        "@odata.type": f"#microsoft.graph.{kind}Attachment", "id": f"{RAW_ATTACHMENT_ID}-{n}",
        "name": f"document-{n}.pdf", "contentType": "application/pdf", "size": 1000, "isInline": False,
        "contentBytes": SENTINEL_BODY,
    }
    raw.update(over)
    return raw


class PreviewBase(ImportBase):
    def setUp(self):
        super().setUp()
        self.graph.__class__ = PreviewGraph          # same patched object, now with the attachments route
        self.graph.prepare()

    def preview(self, user, reference, **body):
        self.client.force_authenticate(user)
        return self.client.post(URL, {"reference": reference, **body}, format="json")

    def message(self, n=1, **over):
        over.setdefault("body", {"contentType": "text", "content": "Hello,\nplease find the minutes."})
        return self.graph.add(n, **over)

    def ok(self, raw=None, user=None, mailbox=None, folder="inbox", **body):
        raw = raw or self.message()
        user = user or self.owner
        return self.preview(user, self.reference(user, raw, mailbox, folder), **body)

    @property
    def writes(self):
        return [c for c in self.graph.calls if "$value" in c["url"]]


class AuthorizationTests(PreviewBase):
    def test_the_owner_previews_a_message_of_their_personal_mailbox(self):
        response = self.ok()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["body_text"], "Hello,\nplease find the minutes.")
        self.assertEqual(response.data["mailbox"]["id"], self.individual_mailbox.pk)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_nobody_else_can_preview_a_personal_mailbox_even_with_their_own_reference(self):
        raw = self.message()
        for user in (self.approver, self.delegate, self.reader_only, self.foreign):
            response = self.preview(user, self.reference(user, raw))
            self.assertEqual((response.status_code, response.data["code"]), (403, "not_authorized"), user.email)
            self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(self.graph.calls, [])

    def test_correspondence_rights_do_not_open_a_mailbox(self):
        self.grant_project(self.approver, ("view", "create", "update"))
        self.assertEqual(self.preview(self.approver, self.reference(self.approver, self.message())).status_code, 403)

    def test_a_reference_issued_to_someone_else_is_useless(self):
        response = self.preview(self.approver, self.reference(self.owner, self.message()))
        self.assertEqual((response.status_code, response.data["code"]), (400, "invalid_reference"))
        self.assertEqual(self.graph.calls, [])

    def test_shared_mailbox_needs_a_valid_read_grant_which_revocation_removes(self):
        raw = self.message()
        ref = self.reference(self.delegate, raw, self.shared_mailbox)
        self.assertEqual(self.preview(self.delegate, ref).status_code, 200)
        self.assertIn("/users/", self.graph.calls[0]["url"])
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.delegate).update(can_read=False)
        response = self.preview(self.delegate, ref)
        self.assertEqual((response.status_code, response.data["code"]), (403, "not_authorized"))
        # a grant that was never a read grant
        sender_only = self._company_member("sendonly")
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=sender_only, can_send=True, can_read=False)
        self.assertEqual(self.preview(sender_only, self.reference(sender_only, raw, self.shared_mailbox)).status_code, 403)

    def test_another_company_never_reaches_the_mailbox(self):
        self.assertEqual(self.preview(self.foreign, self.reference(self.foreign, self.message(), self.shared_mailbox)).status_code, 403)

    def test_the_project_is_optional_and_checked_when_given(self):
        raw = self.message()
        ref = self.reference(self.owner, raw)
        self.assertEqual(self.preview(self.owner, ref).status_code, 200)                           # none
        self.assertEqual(self.preview(self.owner, ref, project=self.project.pk).status_code, 200)  # assigned
        self.assertEqual(self.preview(self.owner, ref, project=self.project_b.pk).status_code, 403)  # not assigned
        foreign_project = _make_project(self.foreign.company)
        self.assertEqual(self.preview(self.owner, ref, project=foreign_project.pk).status_code, 403)
        self.assertEqual(self.preview(self.owner, ref, project=999999).status_code, 403)
        # the preview does not depend on any assignment when no project is named
        MailboxProjectAssignment.objects.filter(mailbox=self.individual_mailbox).delete()
        self.assertEqual(self.preview(self.owner, ref).status_code, 200)

    def test_forged_altered_or_expired_references_are_refused_before_any_read(self):
        token = self.reference(self.owner, self.message())
        for bad in (token[:-4] + "AAAA", "garbage", token.upper()):
            self.assertEqual(self.preview(self.owner, bad).data["code"], "invalid_reference")
        with patch("django.core.signing.time.time", return_value=time.time() + 31 * 60):
            self.assertEqual(self.preview(self.owner, token).data["code"], "invalid_reference")
        self.assertEqual(self.graph.calls, [])

    def test_a_disconnected_or_inactive_mailbox(self):
        raw = self.message()
        ref = self.reference(self.owner, raw)
        UserOutlookCredentials.objects.filter(user=self.owner).delete()
        self.assertEqual(self.preview(self.owner, ref).data["code"], "token_unavailable")
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        self.assertEqual(self.preview(self.owner, ref).data["code"], "mailbox_inactive")

    def test_authentication_and_method(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(URL, {}, format="json").status_code, (401, 403))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(URL).status_code, 405)
        self.assertEqual(self.client.post(URL, {}, format="json").status_code, 400)


class BodyTests(PreviewBase):
    def test_plain_text_is_requested_and_returned_with_its_metadata(self):
        response = self.ok(self.message(subject="Minutes"))
        call = next(h for h in self.graph.headers_log if h["path"].endswith("/messages/AAMk-imp-1"))
        self.assertIn('outlook.body-content-type="text"', call["headers"]["Prefer"])
        self.assertIn('IdType="ImmutableId"', call["headers"]["Prefer"])
        self.assertIn("body", call["params"]["$select"])
        for forbidden in ("uniqueBody", "attachments"):
            self.assertNotIn(forbidden, call["params"]["$select"])
        self.assertEqual(response.data["message"]["subject"], "Minutes")
        self.assertFalse(response.data["body_truncated"])
        self.assertEqual(response.data["body_size"], len("Hello,\nplease find the minutes.".encode()))

    def test_an_html_answer_is_converted_never_returned_or_fetched(self):
        html = ('<html><head><style>p{color:red}</style><script>alert(1)</script></head><body>'
                '<p>Dear team,</p><p>See <b>attached</b><br>regards</p>'
                '<img src="https://tracker.example.com/pixel.gif"><a href="https://evil.example.com/x">link text</a>'
                '</body></html>')
        response = self.ok(self.message(body={"contentType": "html", "content": html}))
        text = response.data["body_text"]
        self.assertIn("Dear team,", text)
        self.assertIn("attached", text)
        self.assertIn("link text", text)
        for leaked in ("<", ">", "alert", "color:red", "tracker.example.com", "evil.example.com"):
            self.assertNotIn(leaked, text)
        self.assertTrue(all(c["url"].startswith(BASE) for c in self.graph.calls))        # nothing else was loaded

    def test_html_helper_is_safe_on_odd_input(self):
        self.assertEqual(html_to_text(""), "")
        self.assertEqual(html_to_text("   "), "")
        self.assertEqual(html_to_text("plain, no tags"), "plain, no tags")
        self.assertNotIn("<", html_to_text("<div><p>unclosed <b>tags"))

    def test_the_utf8_limit_never_splits_a_character(self):
        for char, size in (("a", 1), ("é", 2), ("€", 3), ("😀", 4)):
            text = char * (MAX_BODY_BYTES // size + 5)
            cut, truncated, total = truncate_utf8(text)
            self.assertTrue(truncated)
            self.assertEqual(total, len(text) * size)
            self.assertLessEqual(len(cut.encode("utf-8")), MAX_BODY_BYTES)
            self.assertGreater(len(cut.encode("utf-8")), MAX_BODY_BYTES - size)          # as much as fits
            self.assertEqual(cut, char * (len(cut)))                                      # whole characters only
            cut.encode("utf-8").decode("utf-8")                                           # still valid UTF-8

    def test_the_limit_is_exact_and_truncation_is_flagged_in_the_response(self):
        exact = "x" * MAX_BODY_BYTES
        self.assertEqual(truncate_utf8(exact), (exact, False, MAX_BODY_BYTES))
        self.assertEqual(truncate_utf8(exact + "x")[1:], (True, MAX_BODY_BYTES + 1))
        response = self.ok(self.message(body={"contentType": "text", "content": "é" * 60000}))
        self.assertEqual((response.data["body_truncated"], response.data["body_size"]), (True, 120000))
        self.assertLessEqual(len(response.data["body_text"].encode("utf-8")), MAX_BODY_BYTES)
        empty = self.ok(self.message(2, body={"contentType": "text", "content": ""}))
        self.assertEqual((empty.data["body_text"], empty.data["body_truncated"], empty.data["body_size"]), ("", False, 0))

    def test_line_endings_and_nul_bytes_are_normalised(self):
        response = self.ok(self.message(body={"contentType": "text", "content": "a\r\nb\rc\x00d"}))
        self.assertEqual(response.data["body_text"], "a\nb\ncd")

    def test_a_moved_or_deleted_message_is_gone(self):
        gone, moved = self.message(1), self.message(2)
        self.graph.items[moved["id"]]["parentFolderId"] = "FOLDER-ARCHIVE"
        del self.graph.items[gone["id"]]
        for raw in (gone, moved):
            response = self.preview(self.owner, self.reference(self.owner, raw))
            self.assertEqual((response.status_code, response.data["code"]), (404, "message_gone"))

    def test_graph_failures_have_controlled_answers(self):
        ref = self.reference(self.owner, self.message())
        for status, http, code in ((403, 403, "access_denied"), (429, 429, "rate_limited"), (500, 503, "temporary"), (404, 404, "message_gone")):
            self.graph.forced = FakeResponse(status, {"error": {"message": "secret Bearer TOKEN"}}, headers={"Retry-After": "7"})
            response = self.preview(self.owner, ref)
            self.assertEqual((response.status_code, response.data["code"]), (http, code))
            self.assertEqual(response["Cache-Control"], "no-store")
            text = str(response.data) + str(response.content)
            for secret in ("TOKEN", "Bearer", "graph.microsoft.com"):
                self.assertNotIn(secret, text)
        self.graph.forced = FakeResponse(429, {}, headers={"Retry-After": "7"})
        self.assertEqual(self.preview(self.owner, ref)["Retry-After"], "7")

    def test_a_malformed_graph_answer_is_a_bad_gateway(self):
        for body in ("a string", ["list"], {"contentType": "text", "content": 42}):
            self.graph.items.clear()
            raw = self.graph.add(1, body=body)
            response = self.preview(self.owner, self.reference(self.owner, raw))
            self.assertEqual((response.status_code, response.data["code"]), (502, "invalid_response"), body)
        self.graph.forced = FakeResponse(200, None, bad_json=True)
        self.assertEqual(self.preview(self.owner, self.reference(self.owner, raw)).data["code"], "invalid_response")

    def test_a_failing_attachment_listing_fails_the_whole_preview(self):
        ref = self.reference(self.owner, self.message())
        self.graph.attachments_forced = FakeResponse(429, {}, headers={"Retry-After": "3"})
        response = self.preview(self.owner, ref)
        self.assertEqual((response.status_code, response.data["code"]), (429, "rate_limited"))
        self.assertNotIn("body_text", response.data)                # no half-answer


class AttachmentTests(PreviewBase):
    def listing(self, *raws, **message):
        raw = self.message(**message)
        self.graph.attachments[raw["id"]] = list(raws)
        return self.ok(raw)

    def test_only_the_listed_metadata_is_returned_and_never_content_or_raw_ids(self):
        response = self.listing(attachment(1), attachment(2, "item", name="forwarded.eml"), attachment(3, "reference", name="onedrive"),
                                attachment(4, isInline=True, name="logo.png", contentType="image/png"))
        self.assertEqual(response.status_code, 200)
        items = response.data["attachments"]
        self.assertEqual([i["kind"] for i in items], ["file", "item", "reference", "file"])
        for item in items:
            self.assertEqual(set(item), ATTACHMENT_FIELDS)
        text = str(response.data) + str(response.content)
        for leaked in ("contentBytes", RAW_ATTACHMENT_ID, "AAMk-imp-1", "graph.microsoft.com", "$value", "downloadUrl"):
            self.assertNotIn(leaked, text)
        self.assertEqual(items[0], {**items[0], "name": "document-1.pdf", "size": 1000, "content_type": "application/pdf", "is_inline": False, "importable": True, "reason": ""})
        self.assertNotIn("contentBytes", self.graph.headers_log[-1]["params"].get("$select", ""))

    def test_the_selection_policy(self):
        response = self.listing(
            attachment(1), attachment(2, "item"), attachment(3, "reference"), attachment(4, isInline=True),
            attachment(5, size=MAX_FILE_BYTES), attachment(6, size=MAX_FILE_BYTES + 1), attachment(7, size=0),
            attachment(8, size=None), attachment(9, **{"@odata.type": "#microsoft.graph.somethingElse"}),
        )
        verdict = [(i["importable"], i["reason"]) for i in response.data["attachments"]]
        self.assertEqual(verdict, [
            (True, ""), (False, "item_attachment_not_supported"), (False, "reference_attachment_not_supported"),
            (False, "inline_image"), (True, ""), (False, "file_too_large"), (False, "empty_file"),
            (False, "size_unknown"), (False, "unknown_attachment_type"),
        ])
        self.assertEqual(attachment_policy(GraphAttachmentMeta("x", kind="file", size=5)), (True, ""))

    def test_limits_for_the_future_import_are_announced(self):
        limits = self.listing().data["limits"]
        self.assertEqual(limits, {"max_body_bytes": 102400, "max_file_bytes": 25 * 1024 * 1024,
                                  "max_total_bytes": 100 * 1024 * 1024, "max_files": 20})

    def test_a_flood_of_attachments_is_bounded_and_flagged(self):
        response = self.listing(*[attachment(n) for n in range(1, 131)])
        self.assertEqual((len(response.data["attachments"]), response.data["attachments_truncated"]), (100, True))
        # a chain of pages is followed only a few times
        self.graph.attachment_pages = [
            {"value": [attachment(n)], "@odata.nextLink": f"{BASE}/me/messages/x/attachments?$skiptoken={n}"} for n in range(1, 10)
        ]
        before = len([c for c in self.graph.calls if c["url"].endswith("/attachments") or "/attachments?" in c["url"]])
        response = self.ok(self.message(2))
        pages = len([c for c in self.graph.calls if "/attachments" in c["url"]]) - before
        self.assertLessEqual(pages, 3)
        self.assertTrue(response.data["attachments_truncated"])

    def test_a_foreign_next_link_is_never_followed(self):
        self.graph.attachment_pages = [{"value": [attachment(1)], "@odata.nextLink": "https://evil.example.com/steal"}]
        response = self.ok()
        self.assertEqual(response.status_code, 400)
        self.assertTrue(all(c["url"].startswith(BASE) for c in self.graph.calls))

    def test_no_attachment_is_ever_downloaded(self):
        self.listing(attachment(1), attachment(2))
        self.assertEqual(self.writes, [])
        self.assertTrue(all(not re.search(r"/attachments/[^/?]+", c["url"]) for c in self.graph.calls))


class AttachmentReferenceTests(PreviewBase):
    def references(self, user=None, **kw):
        raw = self.message()
        self.graph.attachments[raw["id"]] = [attachment(1), attachment(2)]
        response = self.ok(raw, user)
        return raw, [a["reference"] for a in response.data["attachments"]]

    def test_each_reference_names_user_mailbox_message_and_attachment(self):
        raw, refs = self.references()
        self.assertEqual(len(set(refs)), 2)
        first = read_attachment_reference(refs[0], user=self.owner, mailbox_id=self.individual_mailbox.pk, message_graph_id=raw["id"])
        self.assertEqual(first, {"mailbox_id": self.individual_mailbox.pk, "message_id": raw["id"],
                                 "attachment_id": f"{RAW_ATTACHMENT_ID}-1", "folder": "inbox"})
        self.assertEqual(read_attachment_reference(refs[1], user=self.owner)["attachment_id"], f"{RAW_ATTACHMENT_ID}-2")

    def test_a_forged_foreign_expired_or_misbound_reference_is_refused(self):
        raw, refs = self.references()
        token = refs[0]
        cases = [
            dict(token=token[:-4] + "AAAA", user=self.owner),
            dict(token="garbage", user=self.owner),
            dict(token="", user=self.owner),
            dict(token=token, user=self.approver),                                       # another user
            dict(token=token, user=self.owner, mailbox_id=self.shared_mailbox.pk),        # another mailbox
            dict(token=token, user=self.owner, message_graph_id="AAMk-other-message"),    # another message
            dict(token=token, user=self.owner, max_age=-1),                               # expired
        ]
        for case in cases:
            token_value = case.pop("token")
            with self.assertRaises(InvalidAttachmentReference):
                read_attachment_reference(token_value, **case)
        # a message reference is not an attachment reference
        with self.assertRaises(InvalidAttachmentReference):
            read_attachment_reference(self.reference(self.owner, raw), user=self.owner)

    def test_a_reference_is_not_an_authorization(self):
        raw, refs = self.references()
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        # still decodable for its user: whoever consumes it must re-authorize the mailbox itself
        self.assertEqual(read_attachment_reference(refs[0], user=self.owner)["mailbox_id"], self.individual_mailbox.pk)
        self.assertEqual(self.preview(self.owner, self.reference(self.owner, raw)).status_code, 409)

    def test_the_reference_builder_is_deterministic_in_content_not_in_value(self):
        mailbox, user = self.individual_mailbox, self.owner
        a = build_attachment_reference(user, mailbox, "m", "a", "inbox")
        self.assertEqual(read_attachment_reference(a, user=user)["attachment_id"], "a")


class NothingIsStoredTests(PreviewBase):
    def test_no_persistence_no_state_change_no_correspondence_no_mail_no_write(self):
        raw = self.message(subject="Stored? no.")
        self.graph.attachments[raw["id"]] = [attachment(1)]
        row = MailboxMessage.objects.create(
            mailbox=self.shared_mailbox, company_profile=self.company, folder="inbox",
            graph_message_id="AAMk-known", internet_message_id="<known@example.com>", state="ignored",
        )
        before = (MailboxMessage.objects.count(), MailboxSyncState.objects.count(), Correspondence.objects.count(),
                  list(MailboxMessage.objects.values_list("pk", "state", "imported_correspondence_id")))
        for _ in range(2):
            self.assertEqual(self.ok(raw).status_code, 200)
            self.assertEqual(self.ok(self.message(2), user=self.delegate, mailbox=self.shared_mailbox).status_code, 200)
        after = (MailboxMessage.objects.count(), MailboxSyncState.objects.count(), Correspondence.objects.count(),
                 list(MailboxMessage.objects.values_list("pk", "state", "imported_correspondence_id")))
        self.assertEqual(before, after)
        self.assertEqual(MailboxMessage.objects.get(pk=row.pk).state, "ignored")
        self.assertEqual(Correspondence.objects.filter(direction__in=("IN", "OUT")).count(), 0)
        self.assertEqual(mail.outbox, [])
        # (requests.post/put/patch/delete are patched to raise: reaching here proves none was used)

    def test_neither_the_body_nor_any_name_reaches_the_logs(self):
        raw = self.message(subject="Confidential subject", body={"contentType": "text", "content": "TOP SECRET BODY LINE"})
        self.graph.attachments[raw["id"]] = [attachment(1, name="salaries.xlsx")]
        with self.assertLogs("userapis", level="DEBUG") as logs:
            self.assertEqual(self.ok(raw).status_code, 200)
        text = " ".join(logs.output)
        for secret in ("TOP SECRET", "Confidential subject", "salaries", "alice@example.com", "AAMk", "REFRESHED-TOKEN"):
            self.assertNotIn(secret, text)

    def test_the_search_and_sync_reads_stay_metadata_only(self):
        from userapis.mailbox_read import MESSAGE_SELECT
        self.assertNotIn("body", MESSAGE_SELECT.lower().replace("hasattachments", ""))
        from userapis.mailbox_read import CONTENT_SELECT
        self.assertIn("body", CONTENT_SELECT)


class ImportStatusTests(PreviewBase):
    def test_a_never_imported_message_reports_nothing(self):
        self.assertEqual(self.ok().data["import_status"], {"imported": False, "correspondence_id": None})

    def test_an_imported_message_shows_its_correspondence_only_to_those_allowed_to_see_it(self):
        raw = self.message()
        done = self.do(self.owner, self.reference(self.owner, raw), external_reference="EXT-1")
        self.assertEqual(done.status_code, 201)
        corr_id = done.data["correspondence"]["id"]
        self.assertEqual(self.ok(raw).data["import_status"], {"imported": True, "correspondence_id": corr_id})
        # the same mail seen from the shared mailbox by a reader with no correspondence right
        twin = self.graph.add(7, internetMessageId=raw["internetMessageId"], body={"contentType": "text", "content": "x"})
        shared = self.preview(self.reader_only, self.reference(self.reader_only, twin, self.shared_mailbox))
        self.assertEqual(shared.data["import_status"], {"imported": True, "correspondence_id": None})
        # ... and a reader who does hold the right sees it
        shared = self.preview(self.delegate, self.reference(self.delegate, twin, self.shared_mailbox))
        self.assertEqual(shared.data["import_status"], {"imported": True, "correspondence_id": corr_id})
        self.assertEqual(MailboxMessage.objects.filter(state="imported").count(), 1)               # still only the real import
