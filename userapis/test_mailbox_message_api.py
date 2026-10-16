"""MAIL-1.3D-B: the review API of stored incoming messages. No Graph call is possible (the network
is patched to fail loudly), nothing is sent, no IN correspondence can appear."""

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APIClient

from project_management.models import Correspondence
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import normalize_message
from userapis.models import MailboxConnection, MailboxMessage, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox import _make_company, _make_project, _make_user
from userapis.test_mailbox_read import SENTINEL_BODY, raw_message
from userapis.test_mailbox_sync import ScriptedReader, SyncBase, iso, page

URL = "/api/user/mailbox-messages/"
FIELDS = {
    "id", "mailbox", "folder", "sender_address", "sender_name", "to_addresses", "cc_addresses",
    "subject", "received_at", "has_attachments", "state", "is_linked", "correspondence_id",
    "created_at",
}
FORBIDDEN = (
    "graph_message_id", "internet_message_id", "conversation_id", "body", "bodyPreview",
    "attachments", "cursor", "token", "access_token", "refresh_token", "company_profile",
)


class ReviewApiBase(SyncBase):
    def setUp(self):
        super().setUp()
        for target in ("requests.get", "requests.post"):
            patcher = patch(target, side_effect=AssertionError("the review API must never call out"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = APIClient()
        self.project_b = _make_project(self.company)
        self.stranger = self._company_member("stranger")                       # no mailbox right at all
        self.sender_only = self._company_member("senderonly")
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=self.sender_only, can_send=True, can_read=False)
        self.foreign = _make_user("foreign@other.test", _make_company("Other"))
        # a LINKS-sent message with its sent copy, and the personal reply to it
        self.corr, _, self.copy = self.sent_out(self.individual_mailbox)
        self.reply_row = self.row(self.individual_mailbox, 1, correspondence=self.corr, subject="RE: personal reply")
        # the personal mailbox also holds an unlinked message that must never be visible
        self.loose_personal = self.row(self.individual_mailbox, 2, subject="private")
        # shared review queue
        self.new_row = self.row(
            self.shared_mailbox, 3, subject="Quote request", received=5,
            **{"from": {"emailAddress": {"name": "Client", "address": "client@acme.test"}}},
        )
        self.ignored_row = self.row(self.shared_mailbox, 4, subject="Newsletter", state="ignored", received=4)
        self.imported_row = self.row(self.shared_mailbox, 5, subject="Order", state="imported", received=3)

    def row(self, mailbox, n, *, state="new", correspondence=None, received=1, **over):
        raw = raw_message(
            n, id=f"AAMk-api-{n}", internetMessageId=f"<api-{n}@example.com>", conversationId=f"conv-api-{n}",
            receivedDateTime=iso(self.now - timedelta(hours=received)), **over,
        )
        meta = normalize_message(raw, "inbox")
        row, _ = store_message_metadata(mailbox, meta)
        MailboxMessage.objects.filter(pk=row.pk).update(state=state, correspondence=correspondence)
        row.refresh_from_db()
        return row

    def get(self, user, path="", **params):
        self.client.force_authenticate(user)
        return self.client.get(URL + path, params)

    def post(self, user, path):
        self.client.force_authenticate(user)
        return self.client.post(URL + path)

    def ids(self, response):
        return [item["id"] for item in response.data["results"]]


class ListAndDetailTests(ReviewApiBase):
    def test_the_owner_sees_only_linked_inbox_messages_of_their_personal_mailbox_plus_granted_shared_ones(self):
        response = self.get(self.owner)
        self.assertEqual(response.status_code, 200)
        ids = set(self.ids(response))
        self.assertIn(self.reply_row.pk, ids)
        self.assertNotIn(self.loose_personal.pk, ids)        # unlinked personal message: never
        self.assertNotIn(self.copy.pk, ids)                  # a sent copy is not an incoming message
        self.assertTrue({self.new_row.pk, self.ignored_row.pk, self.imported_row.pk} <= ids)   # can_read on shared

    def test_a_list_item_has_exactly_the_review_fields_and_nothing_sensitive(self):
        response = self.get(self.owner)
        for item in response.data["results"]:
            self.assertEqual(set(item), FIELDS)
        keys = set()
        for item in response.data["results"]:
            keys |= set(item) | set(item["mailbox"])
        for word in FORBIDDEN:
            self.assertNotIn(word, keys)
        self.assertNotIn(SENTINEL_BODY, str(response.data))
        item = next(i for i in response.data["results"] if i["id"] == self.reply_row.pk)
        self.assertEqual((item["is_linked"], item["correspondence_id"]), (True, self.corr.pk))
        self.assertEqual(set(item["mailbox"]), {"id", "email_address", "display_name", "connection_type"})

    def test_detail_returns_the_same_metadata_only(self):
        response = self.get(self.owner, f"{self.new_row.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), FIELDS)
        self.assertEqual(response.data["subject"], "Quote request")
        self.assertFalse(response.data["is_linked"])
        self.assertIsNone(response.data["correspondence_id"])
        self.assertNotIn(SENTINEL_BODY, str(response.data))

    def test_listing_and_detail_make_no_graph_call_and_create_no_correspondence(self):
        before = Correspondence.objects.filter(direction="IN").count()
        self.get(self.owner)
        self.get(self.owner, f"{self.new_row.pk}/")
        self.assertEqual(Correspondence.objects.filter(direction="IN").count(), before)
        # (requests.get / requests.post are patched to raise: reaching here proves no call was made)

    def test_a_sent_copy_and_an_unlinked_personal_message_are_not_found(self):
        for hidden in (self.copy, self.loose_personal):
            self.assertEqual(self.get(self.owner, f"{hidden.pk}/").status_code, 404)

    def test_unauthenticated_requests_are_refused(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertIn(self.client.post(URL + f"{self.new_row.pk}/ignore/").status_code, (401, 403))

    def test_only_get_on_the_collection_and_detail(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(URL, {}).status_code, 405)
        for method in ("put", "patch", "delete"):
            self.assertEqual(getattr(self.client, method)(URL + f"{self.new_row.pk}/", {}).status_code, 405)


class ScopeAndPrivacyTests(ReviewApiBase):
    def test_a_user_without_any_mailbox_right_sees_nothing(self):
        for user in (self.stranger, self.sender_only, self.approver, self.foreign):
            response = self.get(user)
            self.assertEqual((response.status_code, response.data["count"]), (200, 0), user.email)
            self.assertEqual(self.get(user, f"{self.new_row.pk}/").status_code, 404)
            self.assertEqual(self.get(user, f"{self.reply_row.pk}/").status_code, 404)

    def test_asking_for_a_mailbox_one_cannot_read_is_refused(self):
        for user, mailbox in (
            (self.stranger, self.shared_mailbox), (self.sender_only, self.shared_mailbox),
            (self.approver, self.individual_mailbox), (self.delegate, self.individual_mailbox),
            (self.foreign, self.shared_mailbox),
        ):
            self.assertEqual(self.get(user, mailbox=mailbox.pk).status_code, 403, user.email)

    def test_another_persons_personal_mailbox_is_closed_even_to_those_with_correspondence_rights(self):
        # the approver and the delegate hold rights around this very correspondence / mailbox setup
        for user in (self.approver, self.delegate):
            self.assertEqual(self.get(user, f"{self.reply_row.pk}/").status_code, 404, user.email)
            self.assertNotIn(self.reply_row.pk, self.ids(self.get(user)))
        self.assertEqual(self.post(self.approver, f"{self.reply_row.pk}/ignore/").status_code, 404)

    def test_the_owner_loses_access_when_the_mailbox_is_no_longer_active(self):
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        self.assertEqual(self.get(self.owner, f"{self.reply_row.pk}/").status_code, 404)

    def test_a_lost_read_grant_closes_the_shared_mailbox(self):
        self.assertEqual(self.get(self.owner, f"{self.new_row.pk}/").status_code, 200)
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.owner).update(can_read=False)
        self.assertEqual(self.get(self.owner, f"{self.new_row.pk}/").status_code, 404)
        self.assertEqual(self.get(self.owner, mailbox=self.shared_mailbox.pk).status_code, 403)

    def test_projects_scope_the_shared_mailbox_and_never_cross_companies(self):
        MailboxProjectAssignment.objects.create(mailbox=self.shared_mailbox, project=self.project)
        self.assertEqual(set(self.ids(self.get(self.owner, project=self.project.pk))),
                         {self.new_row.pk, self.ignored_row.pk, self.imported_row.pk})
        self.assertEqual(self.get(self.owner, project=self.project_b.pk).data["count"], 0)     # not assigned there
        self.assertEqual(self.get(self.owner, f"{self.new_row.pk}/", project=self.project_b.pk).status_code, 404)
        # the personal mailbox is not assigned to the project: nothing of it in that context
        self.assertNotIn(self.reply_row.pk, self.ids(self.get(self.owner, project=self.project.pk)))
        # a project of another company is refused outright
        foreign_project = _make_project(self.foreign.company)
        self.assertEqual(self.get(self.owner, project=foreign_project.pk).status_code, 403)
        self.assertEqual(self.get(self.foreign, project=self.project.pk).status_code, 403)

    def test_a_row_of_another_company_never_appears(self):
        other_mailbox = MailboxConnection.objects.create(
            provider="microsoft", connection_type="shared", email_address="info@other.test",
            company_profile=self.foreign.company, connected_by=self.foreign, status="active",
        )
        MailboxUserGrant.objects.create(mailbox=other_mailbox, user=self.foreign, can_read=True)
        foreign_row = self.row(other_mailbox, 9)
        self.assertEqual(self.ids(self.get(self.foreign)), [foreign_row.pk])
        self.assertEqual(self.get(self.owner, f"{foreign_row.pk}/").status_code, 404)
        self.assertNotIn(foreign_row.pk, self.ids(self.get(self.owner)))


class FilterSearchAndPaginationTests(ReviewApiBase):
    def test_filters_by_mailbox_and_state(self):
        self.assertEqual(set(self.ids(self.get(self.owner, mailbox=self.shared_mailbox.pk))),
                         {self.new_row.pk, self.ignored_row.pk, self.imported_row.pk})
        self.assertEqual(self.ids(self.get(self.owner, mailbox=self.individual_mailbox.pk)), [self.reply_row.pk])
        for state, expected in (("new", {self.new_row.pk, self.reply_row.pk}), ("ignored", {self.ignored_row.pk}),
                                ("imported", {self.imported_row.pk})):
            self.assertEqual(set(self.ids(self.get(self.owner, state=state))), expected, state)

    def test_invalid_filters_are_rejected(self):
        self.assertEqual(self.get(self.owner, state="deleted").status_code, 400)
        self.assertEqual(self.get(self.owner, mailbox="abc").status_code, 400)
        self.assertEqual(self.get(self.owner, project="x").status_code, 400)

    def test_search_covers_only_the_shown_metadata(self):
        self.assertEqual(self.ids(self.get(self.owner, search="quote")), [self.new_row.pk])
        self.assertEqual(self.ids(self.get(self.owner, search="client@acme")), [self.new_row.pk])
        self.assertEqual(self.get(self.owner, search="Alice").data["count"], 3)     # sender name of the other visible rows
        # nothing outside the metadata is searchable, and hidden rows are not searchable either
        self.assertEqual(self.get(self.owner, search=SENTINEL_BODY).data["count"], 0)
        self.assertEqual(self.get(self.owner, search="private").data["count"], 0)    # unlinked personal row
        self.assertEqual(self.get(self.stranger, search="quote").data["count"], 0)

    def test_newest_first_and_paginated(self):
        for n in range(20, 45):
            self.row(self.shared_mailbox, n, received=n)
        first = self.get(self.owner, page_size=10)
        self.assertEqual(len(first.data["results"]), 10)
        dates = [i["received_at"] for i in first.data["results"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        everything = []
        page_number = 1
        while True:
            response = self.get(self.owner, page_size=10, page=page_number)
            everything += self.ids(response)
            if not response.data["next"]:
                break
            page_number += 1
        self.assertEqual(len(everything), len(set(everything)))            # no overlap between pages
        self.assertEqual(len(everything), first.data["count"])
        self.assertEqual(len(self.get(self.owner, page_size=1000).data["results"]), min(100, first.data["count"]))


class TriageTests(ReviewApiBase):
    def state_of(self, row):
        return MailboxMessage.objects.get(pk=row.pk).state

    def test_ignore_and_restore_are_idempotent_and_keep_the_metadata(self):
        first = self.post(self.owner, f"{self.new_row.pk}/ignore/")
        self.assertEqual((first.status_code, first.data["state"]), (200, "ignored"))
        again = self.post(self.owner, f"{self.new_row.pk}/ignore/")
        self.assertEqual((again.status_code, again.data["state"]), (200, "ignored"))
        kept = MailboxMessage.objects.get(pk=self.new_row.pk)
        self.assertEqual((kept.subject, kept.graph_message_id), (self.new_row.subject, self.new_row.graph_message_id))
        restored = self.post(self.owner, f"{self.new_row.pk}/restore/")
        self.assertEqual((restored.status_code, restored.data["state"]), (200, "new"))
        self.assertEqual(self.post(self.owner, f"{self.new_row.pk}/restore/").data["state"], "new")
        self.assertEqual(self.post(self.owner, f"{self.ignored_row.pk}/restore/").data["state"], "new")

    def test_an_imported_message_can_neither_be_ignored_nor_restored(self):
        for action in ("ignore", "restore"):
            response = self.post(self.owner, f"{self.imported_row.pk}/{action}/")
            self.assertEqual(response.status_code, 409, action)
            self.assertEqual(response.data["detail"].code, "message_imported")
        self.assertEqual(self.state_of(self.imported_row), "imported")

    def test_only_those_who_may_read_the_mailbox_can_triage_it(self):
        for user in (self.stranger, self.sender_only, self.foreign, self.approver):
            self.assertEqual(self.post(user, f"{self.new_row.pk}/ignore/").status_code, 404, user.email)
            self.assertEqual(self.post(user, f"{self.ignored_row.pk}/restore/").status_code, 404, user.email)
        self.assertEqual(self.state_of(self.new_row), "new")
        self.assertEqual(self.state_of(self.ignored_row), "ignored")
        # the delegate of the shared mailbox holds can_read here, so they may
        self.assertEqual(self.post(self.delegate, f"{self.new_row.pk}/ignore/").status_code, 200)

    def test_the_owner_may_triage_a_linked_personal_reply_and_nobody_else(self):
        self.assertEqual(self.post(self.delegate, f"{self.reply_row.pk}/ignore/").status_code, 404)
        self.assertEqual(self.post(self.owner, f"{self.reply_row.pk}/ignore/").status_code, 200)
        self.assertTrue(MailboxMessage.objects.filter(pk=self.reply_row.pk, correspondence=self.corr).exists())

    def test_triage_changes_nothing_else_and_creates_no_correspondence(self):
        before = Correspondence.objects.filter(pk=self.corr.pk).values("status", "response_status").get()
        self.post(self.owner, f"{self.new_row.pk}/ignore/")
        self.post(self.owner, f"{self.reply_row.pk}/ignore/")
        self.post(self.owner, f"{self.reply_row.pk}/restore/")
        self.assertEqual(Correspondence.objects.filter(pk=self.corr.pk).values("status", "response_status").get(), before)
        self.assertEqual(Correspondence.objects.filter(direction="IN").count(), 0)

    def test_an_ignored_message_does_not_come_back_after_a_resynchronisation(self):
        self.post(self.delegate, f"{self.new_row.pk}/ignore/")
        replayed = normalize_message(raw_message(
            3, id="AAMk-api-3", internetMessageId="<api-3@example.com>", conversationId="conv-api-3",
            receivedDateTime=iso(self.now - timedelta(hours=5)),
        ), "inbox")
        self.reader.delta["initial"] = page(replayed, removed=[self.ignored_row.graph_message_id, self.imported_row.graph_message_id], delta_link="D")
        count = MailboxMessage.objects.count()
        result = self.shared()
        self.assertTrue(result.ok)
        self.assertEqual(self.state_of(self.new_row), "ignored")                 # still ignored, not reset
        self.assertEqual(MailboxMessage.objects.count(), count)                  # no duplicate...
        self.assertEqual(self.state_of(self.ignored_row), "ignored")             # ...and no decision erased
        self.assertEqual(self.state_of(self.imported_row), "imported")
