# MAIL-1.4-D: reading the proof of a send, downloading its files and asking for a new archiving attempt,
# through the API. Microsoft Graph is always a scripted reader, files go to a temporary folder, requests that
# could reach Graph or sendMail raise, and every address, subject and id is fictitious.
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from project_management.models import (
    CorrespondenceSendEvent,
    CorrespondenceSendProof,
    CorrespondenceSendProofFile,
    EvidenceBlob,
)
from project_management.services.correspondence_send import send_correspondence
from project_management.services.correspondence_send_proof_runner import retry_send_proof_archive as real_retry
from project_management.test_correspondence import grant_correspondence_permissions
from project_management.test_correspondence_send_proof_archive import eml_bytes, sha
from project_management.test_correspondence_send_proof_runner import RunnerBase
from userapis.test_mailbox import _make_company, _make_user

State = CorrespondenceSendProof.State
BASE = "/api/project_management/correspondence"


class ApiBase(RunnerBase):
    def setUp(self):
        super().setUp()
        license_patch = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        license_patch.start()
        self.addCleanup(license_patch.stop)
        grant_correspondence_permissions(self.owner, self.company, actions=("view", "send"))
        self.viewer = self._company_member("viewer")
        grant_correspondence_permissions(self.viewer, self.company, actions=("view",))
        self.nobody = self._company_member("nobody")
        self.foreign = _make_user("foreign-api@example.com", _make_company("Foreign API Co"))
        grant_correspondence_permissions(self.foreign, self.foreign.company, actions=("view", "send"))

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def history(self, corr, user=None):
        return self.api(user or self.owner).get(f"{BASE}/{corr.pk}/send")

    def archived(self, files=(), **kwargs):
        corr, event, graph_id = self.sent_and_copied(files, **kwargs)
        proof, result = self.archive(event)
        proof.refresh_from_db()
        return corr, event, proof, graph_id

    def file_url(self, corr, event, proof_file):
        return f"{BASE}/{corr.pk}/send/{event.pk}/proof/files/{proof_file.pk}/download"

    def retry_url(self, corr, event):
        return f"{BASE}/{corr.pk}/send/{event.pk}/proof/retry"

    def download(self, corr, event, proof_file, user=None):
        return self.api(user or self.owner).get(self.file_url(corr, event, proof_file))

    def body(self, response):
        return b"".join(response.streaming_content)


class HistoryTests(ApiBase):
    def test_the_history_keeps_every_existing_field_and_adds_the_proof(self):
        corr, event, proof, _ = self.archived([("letter.pdf", b"letter content")])
        data = self.history(corr).json()
        item = data["events"][0]
        for key in ("id", "correspondence", "sender", "from_email", "to_recipients", "cc_recipients", "bcc_recipients",
                    "subject", "body", "attachments_snapshot", "provider", "mailbox", "mailbox_email",
                    "provider_message_id", "result", "error_message", "sent_at", "created_at"):
            self.assertIn(key, item)
        self.assertEqual(set(data), {"events", "ready_to_send", "can_resend"})
        shown = item["proof"]
        self.assertEqual((shown["state"], shown["verification_level"], shown["issue"], shown["files_archived"]),
                         ("ARCHIVED", "full", None, 2))
        self.assertIsNotNone(shown["archived_at"])
        self.assertEqual(shown["missing_attachments"], [])
        self.assertEqual([(a["file_name"], a["file_size"]) for a in shown["expected_attachments"]], [("letter.pdf", 14)])
        by_kind = {f["kind"]: f for f in shown["files"]}
        self.assertEqual(by_kind["attachment"]["sha256"], sha(b"letter content"))
        self.assertEqual(by_kind["attachment"]["file_size"], 14)

    def test_nothing_internal_is_ever_exposed(self):
        corr, event, proof, graph_id = self.archived([("letter.pdf", b"letter content")])
        text = json.dumps(self.history(corr).json())
        marker = self.reader.idents[graph_id].send_marker
        for secret in (graph_id, "-att-", marker, self.media, "send_evidence", "blob", "graph_attachment_id",
                       "mailbox_message", "error_code", "marker_mismatch", "TOKEN", "x-links-send"):
            self.assertNotIn(secret, text)
        for f in CorrespondenceSendProofFile.objects.exclude(graph_attachment_id=""):
            self.assertNotIn(f.graph_attachment_id, text)

    def test_an_event_without_proof_has_none(self):
        corr = self.out_with()
        event = send_correspondence(corr, sender=self.owner)            # SMTP: no Sent Items copy, no proof
        item = self.history(corr).json()["events"][0]
        self.assertEqual((item["id"], item["provider"], item["proof"]), (event.pk, "smtp", None))

    def test_a_partial_proof_says_what_is_missing_without_technical_detail(self):
        corr, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        self.reader.attachments[graph_id] = self.reader.attachments[graph_id][:1]
        proof, _ = self.archive(event)
        shown = self.history(corr).json()["events"][0]["proof"]
        self.assertEqual((shown["state"], shown["issue"], shown["verification_level"], shown["files_archived"]),
                         ("PARTIAL", "attachment_missing", "none", 2))
        kept_name = CorrespondenceSendProofFile.objects.get(proof=proof, kind="attachment").file_name
        self.assertEqual([m["file_name"] for m in shown["missing_attachments"]], [({"a.pdf", "b.pdf"} - {kept_name}).pop()])
        self.assertTrue(shown["can_retry"])
        self.assertNotIn("attachment_missing", [k for k in shown])            # the code itself is not a field

    def test_a_pending_proof_lists_everything_as_still_to_come(self):
        corr, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        shown = self.history(corr).json()["events"][0]["proof"]
        self.assertEqual((shown["state"], shown["files_archived"], len(shown["missing_attachments"])), ("PENDING", 0, 1))

    def test_a_send_checked_only_by_name_and_size_is_shown_as_reduced(self):
        corr, event, graph_id = self.sent_and_copied([("letter.pdf", b"letter content")])
        snapshot = [{k: v for k, v in e.items() if k != "sha256"} for e in event.attachments_snapshot]
        CorrespondenceSendEvent.objects.filter(pk=event.pk).update(attachments_snapshot=snapshot)
        self.archive(event)
        shown = self.history(corr).json()["events"][0]["proof"]
        self.assertEqual((shown["state"], shown["verification_level"]), ("ARCHIVED", "reduced"))

    def test_a_copy_that_never_came_is_unavailable_and_a_security_stop_is_only_needs_attention(self):
        corr, event, graph_id = self.sent_and_copied()
        CorrespondenceSendProof.objects.filter(event=event).update(state=State.UNAVAILABLE, error_code="copy_not_found")
        self.assertEqual(self.history(corr).json()["events"][0]["proof"]["issue"], "copy_unavailable")
        CorrespondenceSendProof.objects.filter(event=event).update(state=State.FAILED, error_code="marker_mismatch")
        self.assertEqual(self.history(corr).json()["events"][0]["proof"]["issue"], "needs_attention")
        CorrespondenceSendProof.objects.filter(event=event).update(state=State.FAILED, error_code="temporary")
        self.assertEqual(self.history(corr).json()["events"][0]["proof"]["issue"], "temporary_problem")


class AccessTests(ApiBase):
    def test_the_history_needs_the_right_to_view_in_the_right_company(self):
        corr, *_ = self.archived()
        self.assertEqual(self.history(corr, self.viewer).status_code, 200)
        self.assertEqual(self.history(corr, self.nobody).status_code, 403)
        self.assertEqual(self.history(corr, self.foreign).status_code, 404)
        self.assertIn(APIClient().get(f"{BASE}/{corr.pk}/send").status_code, (401, 403))

    def test_bcc_and_the_eml_are_for_people_who_may_send_not_just_see(self):
        corr = self.out_with([("letter.pdf", b"letter content")])
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox, bcc=["hidden@links-local.invalid"])
        from userapis.test_mailbox_correlation import marker_of
        self.copy(event, marker_of(mock_post, self), self._graph_call(mock_post).kwargs["json"]["message"]["attachments"])
        self.archive(event)
        seen_by_sender = self.history(corr).json()["events"][0]
        self.assertEqual((seen_by_sender["bcc_recipients"], seen_by_sender["bcc_hidden"]),
                         ([{"email": "hidden@links-local.invalid"}], False))
        seen_by_viewer = self.history(corr, self.viewer).json()["events"][0]
        self.assertEqual((seen_by_viewer["bcc_recipients"], seen_by_viewer["bcc_hidden"]), ([], True))
        self.assertNotIn("hidden@links-local.invalid", json.dumps(seen_by_viewer))
        files = {f["kind"]: f for f in seen_by_viewer["proof"]["files"]}
        self.assertEqual((files["eml"]["can_download"], files["eml"]["download_url"]), (False, None))
        self.assertTrue(files["attachment"]["can_download"])
        self.assertTrue(all(f["can_download"] for f in seen_by_sender["proof"]["files"]))
        self.assertTrue(seen_by_sender["proof"]["can_retry"] is False)             # archived: nothing to retry


class DownloadTests(ApiBase):
    def setUp(self):
        super().setUp()
        self.corr, self.event, self.proof, self.graph_id = self.archived([("letter.pdf", b"letter content")])
        self.eml = self.proof.files.get(kind="eml")
        self.att = self.proof.files.get(kind="attachment")

    def test_a_sender_downloads_the_eml_and_the_attachment_byte_for_byte(self):
        response = self.download(self.corr, self.event, self.eml)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), self.reader.eml[self.graph_id])
        self.assertEqual(self.body(self.download(self.corr, self.event, self.att)), b"letter content")

    def test_the_download_cannot_be_displayed_or_run_by_a_browser(self):
        response = self.download(self.corr, self.event, self.att)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertIn("letter.pdf", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("sandbox", response["Content-Security-Policy"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(self.media, response["Content-Disposition"])

    def test_someone_who_can_only_see_the_correspondence_gets_the_attachment_not_the_eml(self):
        self.assertEqual(self.download(self.corr, self.event, self.att, self.viewer).status_code, 200)
        self.assertEqual(self.download(self.corr, self.event, self.eml, self.viewer).status_code, 403)

    def test_other_users_other_companies_and_mismatched_ids_are_refused(self):
        self.assertEqual(self.download(self.corr, self.event, self.att, self.nobody).status_code, 403)
        self.assertEqual(self.download(self.corr, self.event, self.att, self.foreign).status_code, 404)
        other_corr, other_event, other_proof, _ = self.archived([("other.pdf", b"other content")])
        other_file = other_proof.files.get(kind="attachment")
        self.assertEqual(self.download(self.corr, self.event, other_file).status_code, 404)        # a file of another send
        self.assertEqual(self.api(self.owner).get(self.file_url(self.corr, other_event, self.att)).status_code, 404)
        self.assertEqual(self.api(self.owner).get(f"{BASE}/{self.corr.pk}/send/{self.event.pk}/proof/files/999999/download").status_code, 404)
        self.assertIn(APIClient().get(self.file_url(self.corr, self.event, self.att)).status_code, (401, 403))

    def test_a_missing_or_altered_file_is_a_clean_error_that_names_no_path(self):
        path = self.att.blob.file.path
        with open(path, "wb") as handle:
            handle.write(b"altered")
        response = self.download(self.corr, self.event, self.att)
        self.assertEqual((response.status_code, response.json()["code"]), (409, "file_unavailable"))
        os.remove(path)
        response = self.download(self.corr, self.event, self.att)
        self.assertEqual(response.status_code, 409)
        for text in (response.content.decode(), str(response.json())):
            self.assertNotIn(self.media, text)
            self.assertNotIn("send_evidence", text)

    def test_a_blob_of_another_company_is_never_served(self):
        foreign_company = self.foreign.company
        EvidenceBlob.objects.filter(pk=self.att.blob_id).update(company_profile=foreign_company)
        self.assertEqual(self.download(self.corr, self.event, self.att).status_code, 409)

    def test_a_dangerous_file_name_is_made_safe_everywhere(self):
        CorrespondenceSendProofFile.objects.filter(pk=self.att.pk).update(file_name="../../etc/passwd")
        response = self.download(self.corr, self.event, self.att)
        self.assertNotIn("..", response["Content-Disposition"])
        self.assertNotIn("/", response["Content-Disposition"].split("filename=")[-1].replace('"', ""))
        self.assertIn(f"evidence-{self.att.pk}", response["Content-Disposition"])
        shown = {f["id"]: f for f in self.history(self.corr).json()["events"][0]["proof"]["files"]}
        self.assertEqual(shown[self.att.pk]["file_name"], f"evidence-{self.att.pk}")
        CorrespondenceSendProofFile.objects.filter(pk=self.att.pk).update(file_name="inv‮oice\r\n.pdf")
        disposition = self.download(self.corr, self.event, self.att)["Content-Disposition"]
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)
        self.assertNotIn("‮", disposition)

    def test_a_download_changes_nothing_and_sends_nothing(self):
        before = (CorrespondenceSendProof.objects.filter(pk=self.proof.pk).values().get(),
                  CorrespondenceSendEvent.objects.filter(pk=self.event.pk).values().get())
        with patch("requests.post", side_effect=AssertionError("no sendMail")), patch("requests.get", side_effect=AssertionError("no Graph call")):
            self.download(self.corr, self.event, self.eml)
        self.assertEqual((CorrespondenceSendProof.objects.filter(pk=self.proof.pk).values().get(),
                          CorrespondenceSendEvent.objects.filter(pk=self.event.pk).values().get()), before)


class RetryTests(ApiBase):
    def failed(self, code="temporary"):
        corr, event, graph_id = self.sent_and_copied([("a.pdf", b"aaa")])
        proof = self.proof_of(event)
        CorrespondenceSendProof.objects.filter(pk=proof.pk).update(state=State.FAILED, error_code=code, attempts=7)
        return corr, event, proof

    def post_retry(self, corr, event, user=None, queue_error=None):
        """The request only QUEUES the archiving (nothing is run inside it); returns (response, queued)."""
        with patch("project_management.tasks.retry_send_proof_archive_task.apply_async", side_effect=queue_error) as queued, \
                patch("requests.post", side_effect=AssertionError("sendMail must never be called")), \
                patch("requests.get", side_effect=AssertionError("no Graph call inside the request")):
            return self.api(user or self.owner).post(self.retry_url(corr, event)), queued

    def run_task(self, proof, user):
        from project_management.tasks import retry_send_proof_archive_task

        def with_reader(proof_id, *, requested_by):
            return real_retry(proof_id, requested_by=requested_by, reader=self.reader, now=self.t0)

        with patch("project_management.services.correspondence_send_proof_runner.retry_send_proof_archive", side_effect=with_reader), \
                patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            return retry_send_proof_archive_task(proof.pk, user.pk)

    def test_someone_who_may_send_queues_the_archiving_and_nothing_runs_in_the_request(self):
        corr, event, proof = self.failed()
        before = CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get()
        response, queued = self.post_retry(corr, event)
        self.assertEqual((response.status_code, response.json()), (202, {"queued": True}))
        queued.assert_called_once_with(args=[proof.pk, self.owner.pk], retry=False)
        self.assertEqual((self.fresh(proof).state, self.fresh(proof).attempts, self.reader.calls), (State.FAILED, 7, []))
        self.assertEqual(CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get(), before)

    def test_the_queued_task_runs_the_same_service_and_the_history_shows_the_result(self):
        corr, event, proof = self.failed()
        outcome = self.run_task(proof, self.owner)
        self.assertEqual((outcome["outcome"], outcome["state"]), ("archived", "ARCHIVED"))
        shown = self.history(corr).json()["events"][0]["proof"]
        self.assertEqual((shown["state"], shown["verification_level"], shown["can_retry"]), ("ARCHIVED", "full", False))

    def test_the_task_checks_again_so_a_switch_turned_off_or_a_lost_right_stops_it(self):
        corr, event, proof = self.failed()
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            self.assertEqual(self.run_task(proof, self.owner)["code"], "archiving_disabled")
        self.may_send.return_value = False
        self.assertEqual(self.run_task(proof, self.owner)["code"], "not_authorized")
        self.assertEqual((self.fresh(proof).state, self.fresh(proof).attempts, self.reader.calls), (State.FAILED, 7, []))

    def test_it_is_refused_without_the_right_to_send_or_for_another_company_or_without_proof(self):
        corr, event, proof = self.failed()
        for user, expected in ((self.viewer, 403), (self.nobody, 403), (self.foreign, 404)):
            response, queued = self.post_retry(corr, event, user)
            self.assertEqual(response.status_code, expected)
            self.assertEqual(queued.call_count, 0)
        smtp_corr = self.out_with()
        smtp_event = send_correspondence(smtp_corr, sender=self.owner)
        self.assertEqual(self.post_retry(smtp_corr, smtp_event)[0].status_code, 404)
        self.assertEqual(self.api(self.owner).post(f"{BASE}/{corr.pk}/send/999999/proof/retry").status_code, 404)
        self.assertEqual((self.proof_of(event).state, self.reader.calls), (State.FAILED, []))

    def test_an_archived_proof_is_refused_and_untouched(self):
        corr, event, proof = self.failed()
        self.run_task(proof, self.owner)
        stamp = self.fresh(proof).updated_at
        response, queued = self.post_retry(corr, event)
        self.assertEqual((response.status_code, response.json()["code"], queued.call_count), (409, "already_archived", 0))
        self.assertEqual(self.fresh(proof).updated_at, stamp)

    def test_the_master_switch_cannot_be_bypassed_and_hides_the_action(self):
        corr, event, proof = self.failed()
        self.assertTrue(self.history(corr).json()["events"][0]["proof"]["can_retry"])
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            self.assertFalse(self.history(corr).json()["events"][0]["proof"]["can_retry"])
            response, queued = self.post_retry(corr, event)
        self.assertEqual((response.status_code, response.json()["code"], queued.call_count), (409, "archiving_disabled", 0))
        self.assertEqual((self.fresh(proof).state, self.fresh(proof).attempts, self.reader.calls), (State.FAILED, 7, []))

    def test_a_queue_that_cannot_be_reached_is_reported_never_worked_around(self):
        corr, event, proof = self.failed()
        response, _ = self.post_retry(corr, event, queue_error=ConnectionError("broker down"))
        self.assertEqual((response.status_code, response.json()["code"]), (503, "queue_unavailable"))
        self.assertEqual((self.fresh(proof).state, self.fresh(proof).attempts, self.reader.calls), (State.FAILED, 7, []))

    def test_a_retry_that_still_cannot_finish_leaves_the_automatic_schedule_alone(self):
        corr, event, proof = self.failed()
        self.reader.sent_items = []
        self.run_task(proof, self.owner)
        shown = self.history(corr).json()["events"][0]["proof"]
        self.assertEqual((shown["state"], shown["next_attempt_at"]), ("UNAVAILABLE", None))
        self.assertEqual(self.fresh(proof).attempts, 8)


class AuditTests(ApiBase):
    def test_downloads_and_retries_are_traced_with_identifiers_only(self):
        corr, event, proof, graph_id = self.archived([("secret-name.pdf", b"letter content")])
        eml, att = proof.files.get(kind="eml"), proof.files.get(kind="attachment")
        with self.assertLogs("project_management.send_proof_audit", level="INFO") as logs:
            self.download(corr, event, att)
            self.download(corr, event, eml, self.viewer)                    # denied
            os.remove(att.blob.file.path)
            self.download(corr, event, att)                                  # unavailable
            CorrespondenceSendProof.objects.filter(pk=proof.pk).update(state=State.FAILED, error_code="temporary")
            with patch("project_management.tasks.retry_send_proof_archive_task.apply_async"):
                self.api(self.owner).post(self.retry_url(corr, event))
        text = "\n".join(logs.output)
        self.assertIn(f"action=proof_file_download outcome=granted user={self.owner.pk} correspondence={corr.pk} event={event.pk} file={att.pk}", text)
        self.assertIn(f"outcome=denied user={self.viewer.pk}", text)
        self.assertIn("outcome=unavailable", text)
        self.assertIn(f"action=proof_retry outcome=queued user={self.owner.pk}", text)
        marker = self.reader.idents[graph_id].send_marker
        for secret in ("secret-name", "letter content", marker, graph_id, "links-local.invalid", self.media):
            self.assertNotIn(secret, text)

    def test_a_refused_retry_is_traced_too(self):
        corr, event, graph_id = self.sent_and_copied()
        with self.assertLogs("project_management.send_proof_audit", level="INFO") as logs:
            with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
                self.api(self.owner).post(self.retry_url(corr, event))
        self.assertIn("action=proof_retry outcome=archiving_disabled", "\n".join(logs.output))
