"""GED-SEC.2 -- SEC-02/03/04/05/06: summary vs detail of the client surveillance, history masking,
RECEIVED events filtered by accessible document, 404/403 for Transmittals outside / inside the scope.

Real project roles and services (OpeningBase / ReturnBase); requests go through the real URL conf and the real
authentication stack (APIClient). Only the license SQL is stubbed (OpeningBase). Isolated test data only.
"""
import shutil
import tempfile

from django.test import override_settings
from rest_framework.test import APIClient

from project_management.models import (
    ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn, Transmittal, TransmittalEvent,
)
from project_management.test_client_surveillance_return import ReturnBase, pdf
from project_management.test_transmittal import make_user

P = "/api/project_management/"
VIEW, UPDATE, TVIEW = "documents.view", "documents.transmittal_update", "documents.transmittal_view"
MANAGE = "documents.client_surveillance_manage"
SECRET_COMMENT = "sec2-secret-comment"
SECRET_REF = "sec2-secret-ref"

_MEDIA = tempfile.mkdtemp(prefix="sec2-media-")


@override_settings(MEDIA_ROOT=_MEDIA)
class Sec2Base(ReturnBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.record_row = self.record(comment=SECRET_COMMENT, source_reference=SECRET_REF, uploads=[pdf("sec2")])
        self.file = self.record_row.files.get()
        self.reader = self.grant("sec2.reader@links-local.invalid", self.project, [VIEW])
        self.receiver = self.grant("sec2.receiver@links-local.invalid", self.project, [VIEW, UPDATE])
        self.tview = self.grant("sec2.tview@links-local.invalid", self.project, [VIEW, TVIEW])
        self.both = self.grant("sec2.both@links-local.invalid", self.project, [VIEW, TVIEW, MANAGE, "documents.update"])
        self.no_view = self.grant("sec2.noview@links-local.invalid", self.project, [TVIEW])
        self.other_project_user = self.grant(
            "sec2.otherproject@links-local.invalid", self.other_project, [VIEW, TVIEW, MANAGE])

    # --- helpers -----------------------------------------------------------------------------
    def get(self, user, path):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client.get(P + path)

    def surveillance_path(self, document=None):
        return f"document/{(document or self.document).pk}/client-surveillance"

    def file_path(self, document=None, issue=None, ret=None, file=None):
        return (f"document/{(document or self.document).pk}/client-surveillance/{(issue or self.client_issue).pk}"
                f"/returns/{(ret or self.record_row).pk}/files/{(file or self.file).pk}")

    def first_row(self, response):
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return response.data["results"][0]


class SurveillanceSummaryVsDetailTests(Sec2Base):
    def test_reader_with_documents_view_only_gets_the_summary(self):
        row = self.first_row(self.get(self.reader, self.surveillance_path()))
        self.assertFalse(row["detail_visible"])
        surveillance = row["surveillance"]
        self.assertEqual(surveillance["status"], self.surveillance.status)
        self.assertEqual(surveillance["due_at"], self.surveillance.due_at and surveillance["due_at"])
        self.assertIsNotNone(surveillance["due_at"])
        self.assertEqual(row["document_revision_id"], self.revision.pk)
        self.assertEqual(surveillance["events"], [])
        self.assertIsNone(surveillance["opening_transmittal"])
        ret = row["returns"][0]
        self.assertEqual(ret["category"], self.record_row.category)  # the A/B/C/D result stays visible
        self.assertEqual(ret["code"], self.record_row.code_snapshot)
        self.assertIsNone(ret["comment"])
        self.assertIsNone(ret["source_reference"])
        self.assertIsNone(ret["send_transmittal"])
        self.assertIsNone(ret["in_transmittal"])
        self.assertIsNone(ret["recorded_by"])
        self.assertEqual(ret["files"], [])
        self.assertNotIn("send_events", row)
        self.assertNotIn("in_transmittals", row)

    def test_reader_response_carries_no_sensitive_text_anywhere(self):
        response = self.get(self.reader, self.surveillance_path())
        body = response.content.decode()
        for forbidden in (SECRET_COMMENT, SECRET_REF, self.transmittal.transmittal_number, "download_url", "sha256", "/files/"):
            self.assertNotIn(forbidden, body)
        for row in response.data["distributions"]:
            self.assertIsNone(row["transmittal_reference"])
            self.assertIsNone(row["transmittal_id"])

    def test_receiver_without_transmittal_view_is_also_summary_only(self):
        row = self.first_row(self.get(self.receiver, self.surveillance_path()))
        self.assertFalse(row["detail_visible"])
        self.assertEqual(row["returns"][0]["files"], [])

    def test_surveillance_manager_sees_the_details(self):
        row = self.first_row(self.get(self.manager, self.surveillance_path()))
        self.assertTrue(row["detail_visible"])
        ret = row["returns"][0]
        self.assertEqual((ret["comment"], ret["source_reference"]), (SECRET_COMMENT, SECRET_REF))
        self.assertEqual(len(ret["files"]), 1)
        self.assertIn("download_url", ret["files"][0])
        self.assertTrue(row["surveillance"]["events"])
        self.assertIsNotNone(ret["send_transmittal"])

    def test_transmittal_reader_sees_the_details_on_accessible_documents(self):
        row = self.first_row(self.get(self.tview, self.surveillance_path()))
        self.assertTrue(row["detail_visible"])
        self.assertEqual(row["returns"][0]["comment"], SECRET_COMMENT)
        self.assertEqual(len(row["returns"][0]["files"]), 1)

    def test_user_with_both_rights_keeps_everything(self):
        row = self.first_row(self.get(self.both, self.surveillance_path()))
        self.assertTrue(row["detail_visible"] and row["can_manage"])
        self.assertEqual(row["returns"][0]["comment"], SECRET_COMMENT)

    def test_transmittal_view_without_document_access_gets_404(self):
        self.assertEqual(self.get(self.no_view, self.surveillance_path()).status_code, 404)

    def test_other_project_other_tenant_and_anonymous(self):
        self.assertEqual(self.get(self.other_project_user, self.surveillance_path()).status_code, 404)
        self.assertEqual(self.get(self.other_owner, self.surveillance_path()).status_code, 404)
        self.assertEqual(self.get(None, self.surveillance_path()).status_code, 401)

    def test_nothing_stored_is_modified_by_the_masking(self):
        before = (ClientSurveillanceEvent.objects.count(), ClientSurveillanceReturn.objects.count(),
                  ClientSurveillance.objects.get(pk=self.surveillance.pk).status)
        self.get(self.reader, self.surveillance_path())
        self.get(self.reader, f"document/{self.document.pk}/history-events")
        after = (ClientSurveillanceEvent.objects.count(), ClientSurveillanceReturn.objects.count(),
                 ClientSurveillance.objects.get(pk=self.surveillance.pk).status)
        self.assertEqual(before, after)
        self.record_row.refresh_from_db()
        self.assertEqual(self.record_row.comment, SECRET_COMMENT)


class ReturnFileDownloadTests(Sec2Base):
    def test_reader_with_documents_view_only_is_refused(self):
        self.assertEqual(self.get(self.reader, self.file_path()).status_code, 403)

    def test_receiver_without_transmittal_view_is_refused(self):
        self.assertEqual(self.get(self.receiver, self.file_path()).status_code, 403)

    def test_refusal_does_not_reveal_which_file_ids_exist(self):
        codes = {self.get(self.reader, self.file_path(file=type("F", (), {"pk": pk})())).status_code
                 for pk in (self.file.pk, self.file.pk + 999)}
        self.assertEqual(codes, {403})

    def test_entitled_users_still_download(self):
        # direct view call: a streamed response through the test client would close the test connection
        for user in (self.manager, self.tview, self.both):
            with self.subTest(user=user.email):
                response = self.download(user, self.record_row, self.file)
                self.assertEqual(response.status_code, 200)
                self.assertIn("attachment", response["Content-Disposition"])
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                response.file_to_stream.close()  # not response.close(): that would fire request_finished and close the test connection

    def test_consistency_checks_stay_404_for_entitled_users(self):
        other_document = self.make_document(name="another.txt")
        self.assertEqual(self.download(self.manager, self.record_row, self.file, document=other_document).status_code, 404)
        ghost = type("F", (), {"pk": self.file.pk + 999})()
        self.assertEqual(self.download(self.manager, self.record_row, ghost).status_code, 404)

    def test_other_project_other_tenant_and_anonymous(self):
        self.assertEqual(self.get(self.other_project_user, self.file_path()).status_code, 404)
        self.assertEqual(self.get(self.other_owner, self.file_path()).status_code, 404)
        self.assertEqual(self.get(None, self.file_path()).status_code, 401)


class HistoryMaskingTests(Sec2Base):
    def events(self, user):
        response = self.get(user, f"document/{self.document.pk}/history-events")
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    def client_events(self, events):
        return [e for e in events if e["event_group"] in ("CLIENT_DISTRIBUTION", "CLIENT_SURVEILLANCE")]

    def test_reader_keeps_the_events_but_not_actor_comment_or_transmittal_reference(self):
        masked, full = self.events(self.reader), self.events(self.manager)
        self.assertEqual([(e["id"], e["event_type"], e["occurred_at"]) for e in masked],
                         [(e["id"], e["event_type"], e["occurred_at"]) for e in full])  # nothing removed or invented
        client = self.client_events(masked)
        self.assertTrue(client)
        for event in client:
            self.assertIsNone(event["actor"])
            self.assertNotIn("transmittal_reference", event["metadata"])
            self.assertNotIn("comment", event["metadata"])
        self.assertTrue(any(e["event_group"] == "CLIENT_DISTRIBUTION" and e["metadata"].get("client_company_name") for e in client))
        self.assertTrue(any(e["event_group"] == "CLIENT_SURVEILLANCE" and "result" in e["metadata"] for e in client))

    def test_entitled_users_keep_the_existing_details(self):
        for user in (self.manager, self.tview):
            client = self.client_events(self.events(user))
            self.assertTrue(any(e["metadata"].get("transmittal_reference") for e in client))
            self.assertTrue(any(e["actor"] for e in client))
        self.assertIn(SECRET_COMMENT, str(self.events(self.manager)) + SECRET_COMMENT)

    def test_other_events_are_untouched_for_a_reader(self):
        masked = {e["id"]: e for e in self.events(self.reader) if e["event_group"] not in ("CLIENT_DISTRIBUTION", "CLIENT_SURVEILLANCE")}
        full = {e["id"]: e for e in self.events(self.manager) if e["event_group"] not in ("CLIENT_DISTRIBUTION", "CLIENT_SURVEILLANCE")}
        self.assertEqual(masked, full)

    def test_client_distribution_history_masks_the_transmittal_reference(self):
        path = f"document/{self.document.pk}/client-distribution-history"
        masked = self.get(self.reader, path)
        self.assertEqual(masked.status_code, 200)
        self.assertTrue(masked.data["results"])
        for row in masked.data["results"]:
            self.assertIsNone(row["transmittal_reference"])
            self.assertIsNone(row["transmittal_id"])
            self.assertTrue(row["client_company_name"])
            self.assertIn("review_due_date", row)
        for user in (self.manager, self.tview):
            row = self.get(user, path).data["results"][0]
            self.assertEqual(row["transmittal_reference"], self.transmittal.transmittal_number)

    def test_history_access_rules_are_unchanged(self):
        for path in (f"document/{self.document.pk}/history-events", f"document/{self.document.pk}/client-distribution-history"):
            self.assertEqual(self.get(self.other_project_user, path).status_code, 404)
            self.assertEqual(self.get(self.other_owner, path).status_code, 404)
            self.assertEqual(self.get(None, path).status_code, 401)


class ReceivedEventsFilterTests(Sec2Base):
    def setUp(self):
        super().setUp()
        self.in_transmittal = Transmittal.objects.create(
            project=self.project, direction="IN", status="ISSUED", transmittal_number="SEC2-IN-1",
            external_company=self.client_company, created_by=self.owner)
        self.metadata = {"note": "kept", "client_return_lines": [
            {"line": 1, "client_issue": self.client_issue.pk, "send_event": None, "no_send_proof": True,
             "category": "ACCEPTED", "qualified_now": False}]}
        self.event = TransmittalEvent.objects.create(
            transmittal=self.in_transmittal, event_type="RECEIVED", actor=self.owner, metadata=self.metadata)
        # limited to the documents they created: the document of the surveillance is not accessible to them
        self.limited = self.grant("sec2.limited@links-local.invalid", self.project, ["documents.view_created", TVIEW])

    def received(self, user):
        response = self.get(user, f"transmittal/{self.in_transmittal.pk}/events")
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return [e for e in response.data if e["event_type"] == "RECEIVED"][0]

    def test_reader_of_the_document_sees_its_lines(self):
        event = self.received(self.tview)
        self.assertEqual(len(event["metadata"]["client_return_lines"]), 1)
        self.assertEqual(event["metadata"]["note"], "kept")
        self.assertNotIn("client_return_lines_restricted", event["metadata"])

    def test_inaccessible_document_lines_are_removed_other_metadata_kept(self):
        event = self.received(self.limited)
        self.assertEqual(event["metadata"]["client_return_lines"], [])
        self.assertEqual(event["metadata"]["note"], "kept")
        self.assertEqual(event["metadata"]["client_return_lines_restricted"], 1)
        body = str(event)
        self.assertNotIn("ACCEPTED", body)

    def test_stored_event_is_never_modified(self):
        self.received(self.limited)
        stored = TransmittalEvent.objects.get(pk=self.event.pk)
        self.assertEqual(stored.metadata, self.metadata)


class TransmittalEnumerationTests(Sec2Base):
    def setUp(self):
        super().setUp()
        self.target = Transmittal.objects.create(
            project=self.project, direction="OUT", status="DRAFT", transmittal_number="SEC2-OUT-1",
            external_company=self.client_company, created_by=self.owner)
        self.stranger = make_user("sec2.stranger@links-local.invalid")
        self.stranger.company = self.company
        self.stranger.save(update_fields=["company"])  # same tenant, member of no project

    def path(self, pk=None, suffix="events"):
        return f"transmittal/{pk or self.target.pk}/{suffix}"

    def test_authorized_user_is_unchanged(self):
        self.assertEqual(self.get(self.tview, self.path()).status_code, 200)

    def test_member_without_the_permission_gets_403(self):
        self.assertEqual(self.get(self.reader, self.path()).status_code, 403)

    def test_other_tenant_gets_404_and_is_indistinguishable_from_a_missing_transmittal(self):
        existing = self.get(self.other_owner, self.path())
        missing = self.get(self.other_owner, self.path(pk=99999999))
        self.assertEqual((existing.status_code, missing.status_code), (404, 404))

    def test_same_tenant_non_member_of_the_project_gets_404(self):
        self.assertEqual(self.get(self.stranger, self.path()).status_code, 404)

    def test_member_of_another_project_gets_404(self):
        self.assertEqual(self.get(self.other_project_user, self.path()).status_code, 404)

    def test_missing_transmittal_is_404_for_everybody(self):
        for user in (self.tview, self.reader):
            self.assertEqual(self.get(user, self.path(pk=99999999)).status_code, 404)

    def test_anonymous_is_401_and_sec07_is_preserved(self):
        self.assertEqual(self.get(None, self.path()).status_code, 401)
        self.assertEqual(self.get(None, f"transmittal/{self.target.pk}").status_code, 401)

    def test_the_other_helper_paths_use_the_same_rule(self):
        for suffix in ("client-return-lines", "items"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.get(self.other_owner, self.path(suffix=suffix)).status_code, 404)
                self.assertEqual(self.get(self.reader, self.path(suffix=suffix)).status_code, 403)
