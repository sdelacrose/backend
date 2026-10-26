"""MAIL-1.3D-C1: the user's explicit mailbox search. Microsoft Graph is a fake (patched
`requests.get`); `requests.post` raises -- no real call and no mail can leave."""

from datetime import timedelta
from unittest.mock import patch

from rest_framework.test import APIClient

from models.models import UserOutlookCredentials
from project_management.models import Correspondence
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import normalize_message
from userapis.mailbox_search import (
    InvalidSearchReference, build_search_reference, read_search_reference,
)
from userapis.models import (
    MailboxConnection, MailboxMessage, MailboxProjectAssignment, MailboxSyncState, MailboxUserGrant,
)
from userapis.test_mailbox import _make_company, _make_project, _make_user
from userapis.test_mailbox_read import SENTINEL_BODY, FakeGraph, FakeResponse, raw_message
from userapis.test_mailbox_sync import SyncBase, iso

URL = "/api/user/mailbox-search/"
RESULT_FIELDS = {
    "reference", "sender_address", "sender_name", "to_addresses", "cc_addresses", "subject",
    "received_at", "has_attachments",
}
FORBIDDEN = (
    "id", "graph_message_id", "internet_message_id", "internetMessageId", "conversation_id",
    "conversationId", "body", "bodyPreview", "attachments", "token", "cursor",
)


class SearchBase(SyncBase):
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph(me_address=self.individual_mailbox.email_address)
        for target, kwargs in (
            ("userapis.mailbox_read.requests.get", {"side_effect": self.graph}),
            ("userapis.mailbox_read.refresh_outlook_access_token", {"return_value": "REFRESHED-TOKEN"}),
            ("requests.post", {"side_effect": AssertionError("a search never sends anything")}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.graph.list_payload = {"value": [raw_message(1), raw_message(2)]}
        self.client = APIClient()
        self.stranger = self._company_member("stranger")
        self.sender_only = self._company_member("senderonly")
        MailboxUserGrant.objects.create(mailbox=self.shared_mailbox, user=self.sender_only, can_send=True, can_read=False)
        self.foreign = _make_user("foreign@other.test", _make_company("Other"))
        self.project_b = _make_project(self.company)

    def search(self, user, **body):
        body.setdefault("mailbox", self.individual_mailbox.pk)
        self.client.force_authenticate(user)
        return self.client.post(URL, body, format="json")

    @property
    def listing(self):
        return [c for c in self.graph.message_calls if "/mailFolders/" in c["url"]]

    def day(self, days_ago):
        return (self.now - timedelta(days=days_ago)).date().isoformat()


class AuthorizationTests(SearchBase):
    def test_the_owner_searches_their_personal_mailbox(self):
        response = self.search(self.owner)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertEqual(response.data["mailbox"]["id"], self.individual_mailbox.pk)
        self.assertTrue(self.listing[0]["url"].endswith("/me/mailFolders/inbox/messages"))

    def test_nobody_else_can_search_a_personal_mailbox(self):
        for user in (self.approver, self.delegate, self.stranger, self.sender_only, self.foreign):
            response = self.search(user)
            self.assertEqual((response.status_code, response.data["code"]), (403, "not_authorized"), user.email)
        self.assertEqual(self.graph.message_calls, [])        # refused before any Graph request

    def test_correspondence_rights_never_substitute_for_mailbox_rights(self):
        # the approver validated the correspondence sent from this very mailbox
        self.assertEqual(self.search(self.approver).status_code, 403)

    def test_an_unknown_mailbox_looks_exactly_like_an_unauthorized_one(self):
        unknown = self.search(self.owner, mailbox=999999)
        denied = self.search(self.approver)
        self.assertEqual((unknown.status_code, unknown.data), (denied.status_code, denied.data))

    def test_shared_search_follows_the_read_grant(self):
        shared = {"mailbox": self.shared_mailbox.pk}
        self.assertEqual(self.search(self.owner, **shared).status_code, 200)       # can_read
        self.assertEqual(self.search(self.delegate, **shared).status_code, 200)    # can_read (SyncBase)
        for user in (self.sender_only, self.stranger, self.approver, self.foreign):
            self.assertEqual(self.search(user, **shared).status_code, 403, user.email)
        self.assertTrue(self.listing[0]["url"].endswith("/mailFolders/inbox/messages"))
        self.assertIn("/users/", self.listing[0]["url"])
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.owner).update(can_read=False)
        self.assertEqual(self.search(self.owner, **shared).status_code, 403)

    def test_project_context_requires_the_assignment_and_the_same_company(self):
        shared = {"mailbox": self.shared_mailbox.pk}
        self.assertEqual(self.search(self.owner, project=self.project.pk, **shared).status_code, 403)
        MailboxProjectAssignment.objects.create(mailbox=self.shared_mailbox, project=self.project)
        self.assertEqual(self.search(self.owner, project=self.project.pk, **shared).status_code, 200)
        self.assertEqual(self.search(self.owner, project=self.project_b.pk, **shared).status_code, 403)
        foreign_project = _make_project(self.foreign.company)
        self.assertEqual(self.search(self.owner, project=foreign_project.pk, **shared).status_code, 403)

    def test_another_companys_mailbox_is_never_reachable(self):
        other = MailboxConnection.objects.create(
            provider="microsoft", connection_type="shared", email_address="info@other.test",
            company_profile=self.foreign.company, connected_by=self.foreign, status="active",
        )
        MailboxUserGrant.objects.create(mailbox=other, user=self.foreign, can_read=True)
        self.assertEqual(self.search(self.owner, mailbox=other.pk).status_code, 403)
        self.assertEqual(self.graph.message_calls, [])

    def test_authentication_and_method(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.post(URL, {"mailbox": 1}, format="json").status_code, (401, 403))
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get(URL).status_code, 405)


class CriteriaTests(SearchBase):
    def test_default_is_the_inbox_over_the_last_thirty_days(self):
        self.search(self.owner)
        call = self.listing[0]
        flt = call["params"]["$filter"]
        self.assertTrue(flt.startswith("receivedDateTime ge "))           # Graph needs the ordered property first
        self.assertIn(" and receivedDateTime le ", flt)
        self.assertEqual(call["params"]["$orderby"], "receivedDateTime desc")
        self.assertNotIn("from/emailAddress", flt)
        self.assertNotIn("contains(", flt)

    def test_sender_subject_and_dates_become_server_side_filters(self):
        self.search(
            self.owner, sender="Client@Acme.Test", subject="Quote o'neil",
            date_from=self.day(10), date_to=self.day(5), folder="sentitems",
        )
        call = self.listing[0]
        self.assertTrue(call["url"].endswith("/mailFolders/sentitems/messages"))
        flt = call["params"]["$filter"]
        start = f"receivedDateTime ge {self.day(10)}T00:00:00Z"
        end = f"receivedDateTime le {self.day(5)}T23:59:59Z"
        self.assertTrue(flt.startswith(f"{start} and {end}"), flt)
        self.assertTrue(flt.endswith("and from/emailAddress/address eq 'client@acme.test' and contains(subject,'Quote o''neil')"), flt)

    def test_a_date_time_is_taken_as_given(self):
        self.search(self.owner, date_from=f"{self.day(3)}T08:30:00Z", date_to=f"{self.day(2)}T09:00:00+00:00")
        flt = self.listing[0]["params"]["$filter"]
        self.assertTrue(flt.startswith(f"receivedDateTime ge {self.day(3)}T08:30:00Z and receivedDateTime le {self.day(2)}T09:00:00Z"))

    def test_invalid_or_too_wide_criteria_are_refused_before_any_graph_request(self):
        for bad in (
            {"folder": "drafts"}, {"folder": "deleteditems"}, {"sender": "not-an-address"},
            {"subject": "ab"}, {"subject": "x" * 101}, {"date_from": "yesterday"}, {"date_to": "31/12/2026"},
            {"date_from": self.day(1), "date_to": self.day(5)},                 # start after end
            {"date_from": self.day(400), "date_to": self.day(1)},               # wider than a year
            {"page": 0}, {"page": 5}, {"page_size": 0}, {"page_size": 26}, {"page": "x"}, {"mailbox": "abc"},
        ):
            response = self.search(self.owner, **bad)
            self.assertEqual(response.status_code, 400, bad)
        self.assertEqual(self.graph.message_calls, [])

    def test_the_search_is_limited_to_the_chosen_mailbox_only(self):
        self.search(self.owner)
        self.assertEqual(len(self.listing), 1)
        self.assertNotIn("shared", self.listing[0]["url"])


class PaginationTests(SearchBase):
    def test_a_page_asks_for_one_extra_row_to_know_whether_another_exists(self):
        self.graph.list_payload = {"value": [raw_message(n) for n in (1, 2, 3)]}
        first = self.search(self.owner, page_size=2)
        self.assertEqual((len(first.data["results"]), first.data["has_next"]), (2, True))
        self.assertEqual((self.listing[0]["params"]["$top"], self.listing[0].get("params").get("$skip")), (3, None))
        self.graph.list_payload = {"value": [raw_message(3)]}
        second = self.search(self.owner, page_size=2, page=2)
        self.assertEqual((len(second.data["results"]), second.data["has_next"]), (1, False))
        self.assertEqual(self.listing[-1]["params"]["$skip"], 2)

    def test_the_number_of_pages_and_results_is_strictly_bounded(self):
        self.graph.list_payload = {"value": [raw_message(n) for n in range(1, 30)]}
        response = self.search(self.owner, page_size=25, page=4)
        self.assertEqual(len(response.data["results"]), 25)
        self.assertFalse(response.data["has_next"])                           # never advertises a fifth page
        self.assertEqual((response.data["max_pages"], self.listing[-1]["params"]["$top"], self.listing[-1]["params"]["$skip"]), (4, 26, 75))
        self.assertEqual(len(self.listing), 1)                                # one Graph request per search

    def test_no_raw_graph_next_link_is_ever_returned(self):
        self.graph.list_payload = {"value": [raw_message(1)], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?$skiptoken=SECRET"}
        response = self.search(self.owner)
        self.assertNotIn("SECRET", str(response.data))
        self.assertNotIn("nextLink", str(response.data))


class PrivacyAndPersistenceTests(SearchBase):
    def test_results_hold_only_selection_metadata(self):
        response = self.search(self.owner)
        for item in response.data["results"]:
            self.assertEqual(set(item), RESULT_FIELDS)
        self.assertEqual(set(response.data["mailbox"]), {"id", "email_address", "display_name", "connection_type"})
        keys = set(response.data) | set(response.data["criteria"])
        for item in response.data["results"]:
            keys |= set(item)
        for word in FORBIDDEN:
            self.assertNotIn(word, keys)
        self.assertNotIn(SENTINEL_BODY, str(response.data))
        select = self.listing[0]["params"]["$select"].lower().replace("hasattachments", "")
        for word in ("body", "attachments", "preview"):
            self.assertNotIn(word, select)
        # the opaque reference is not a raw Graph id
        self.assertTrue(all("AAMk" not in item["reference"] for item in response.data["results"]))

    def test_nothing_is_persisted_and_no_decision_changes(self):
        known = normalize_message(raw_message(1), "inbox")
        row, _ = store_message_metadata(self.shared_mailbox, known)
        MailboxMessage.objects.filter(pk=row.pk).update(state="ignored")
        counts = (MailboxMessage.objects.count(), MailboxSyncState.objects.count(),
                  Correspondence.objects.count(), Correspondence.objects.filter(direction="IN").count())
        status = MailboxConnection.objects.get(pk=self.individual_mailbox.pk).status
        corr_state = list(Correspondence.objects.order_by("pk").values_list("pk", "status", "response_status"))
        for _ in range(2):
            self.assertEqual(self.search(self.owner).status_code, 200)
            self.assertEqual(self.search(self.owner, mailbox=self.shared_mailbox.pk).status_code, 200)
        self.assertEqual(counts, (MailboxMessage.objects.count(), MailboxSyncState.objects.count(),
                                  Correspondence.objects.count(), Correspondence.objects.filter(direction="IN").count()))
        self.assertEqual(MailboxMessage.objects.get(pk=row.pk).state, "ignored")
        self.assertEqual(MailboxConnection.objects.get(pk=self.individual_mailbox.pk).status, status)
        self.assertEqual(corr_state, list(Correspondence.objects.order_by("pk").values_list("pk", "status", "response_status")))

    def test_a_search_never_reads_another_folder_or_the_whole_mailbox(self):
        self.search(self.owner)
        self.assertEqual({c["url"].split("/mailFolders/")[1].split("/")[0] for c in self.listing}, {"inbox"})


class ErrorTests(SearchBase):
    def assertCode(self, response, status, code):
        self.assertEqual((response.status_code, response.data["code"]), (status, code), response.data)
        text = str(response.data) + str(response.content)
        for secret in ("TOKEN", "REFRESHED", "Bearer", "graph.microsoft.com"):
            self.assertNotIn(secret, text)

    def test_graph_failures_have_stable_codes_and_no_sensitive_detail(self):
        for graph_status, http_status, code in (
            (429, 429, "rate_limited"), (500, 503, "temporary"), (503, 503, "temporary"),
            (401, 409, "token_rejected"), (403, 403, "access_denied"), (404, 404, "not_found"),
            (400, 400, "request_rejected"),
        ):
            self.graph.forced = FakeResponse(graph_status, {"error": {"message": "secret subject Bearer TOKEN"}}, headers={"Retry-After": "17"})
            self.assertCode(self.search(self.owner), http_status, code)

    def test_a_rate_limit_carries_graphs_retry_delay(self):
        self.graph.forced = FakeResponse(429, {}, headers={"Retry-After": "17"})
        self.assertEqual(self.search(self.owner)["Retry-After"], "17")

    def test_an_unreadable_graph_answer_is_a_bad_gateway(self):
        self.graph.forced = FakeResponse(200, None, bad_json=True)
        self.assertCode(self.search(self.owner), 502, "invalid_response")

    def test_a_mailbox_that_is_not_connected_or_whose_connection_is_gone(self):
        UserOutlookCredentials.objects.filter(user=self.owner).delete()
        self.assertCode(self.search(self.owner), 409, "token_unavailable")
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="pending")
        self.assertCode(self.search(self.owner), 409, "mailbox_inactive")
        # only someone who would be allowed learns the state: others get the generic refusal
        self.assertCode(self.search(self.approver), 403, "not_authorized")

    def test_a_renewal_failure_is_reported_without_detail(self):
        with patch("userapis.mailbox_read.refresh_outlook_access_token", return_value=None):
            self.assertCode(self.search(self.owner), 409, "token_refresh_failed")

    def test_a_mismatching_microsoft_account_is_refused(self):
        self.graph.me_address = "someone-else@example.com"
        self.assertCode(self.search(self.owner), 409, "identity_mismatch")
        self.assertEqual(self.listing, [])

    def test_a_failed_search_changes_nothing(self):
        self.graph.forced = FakeResponse(500, {})
        self.search(self.owner)
        self.assertEqual((MailboxSyncState.objects.count(), MailboxMessage.objects.count()), (0, 0))
        self.assertEqual(MailboxConnection.objects.get(pk=self.individual_mailbox.pk).status, "active")


class ReferenceTests(SearchBase):
    def issued(self):
        response = self.search(self.owner)
        return response.data["results"][0]["reference"]

    def test_a_reference_names_the_mailbox_and_item_for_the_user_it_was_issued_to(self):
        payload = read_search_reference(self.issued(), user=self.owner)
        self.assertEqual(payload["mailbox_id"], self.individual_mailbox.pk)
        self.assertEqual(payload["graph_id"], "AAMk-immutable-1")
        self.assertEqual(payload["folder"], "inbox")

    def test_a_forged_altered_foreign_or_expired_reference_is_refused(self):
        token = self.issued()
        forged = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")
        for candidate, user, age in (
            (forged, self.owner, 60), ("not-a-token", self.owner, 60), ("", self.owner, 60),
            (token, self.approver, 60),                                           # issued to someone else
            (token, self.owner, -1),                                              # expired
        ):
            with self.assertRaises(InvalidSearchReference):
                read_search_reference(candidate, user=user, max_age=age)

    def test_a_reference_is_not_an_authorization(self):
        token = self.issued()
        # still decodable for its user after their right is gone: consuming it must re-authorize
        MailboxConnection.objects.filter(pk=self.individual_mailbox.pk).update(status="revoked")
        self.assertEqual(read_search_reference(token, user=self.owner)["mailbox_id"], self.individual_mailbox.pk)
        self.assertEqual(self.search(self.owner).status_code, 409)               # the mailbox itself is closed
        # a bare Graph id presented without a reference proves nothing
        with self.assertRaises(InvalidSearchReference):
            read_search_reference("AAMk-immutable-1", user=self.owner)
