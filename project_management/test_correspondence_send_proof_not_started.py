# MAIL-PROOF-FIX: a send whose proof was never started because NO automatic archiving covers it (switch off, or sent before the
# activation date) must not read as "archiving pending". The API says whether the automatic archiving covers the send; the proof is
# still PENDING (never ARCHIVED / Verified), nothing is invented, and it can be built later WITHOUT sending again. Microsoft Graph is
# always a scripted reader; nothing here can reach sendMail.
import json
from datetime import timedelta
from unittest.mock import patch

from django.test import override_settings

from project_management.models import CorrespondenceSendEvent, CorrespondenceSendProof, CorrespondenceSendProofFile
from project_management.test_correspondence_send_proof_api import ApiBase

State = CorrespondenceSendProof.State


class NotStartedStateTests(ApiBase):
    def shown(self, corr, index=0):
        return self.history(corr).json()["events"][index]["proof"]

    def sent(self, files=()):
        corr, event, graph_id = self.sent_and_copied(files)
        return corr, event, CorrespondenceSendProof.objects.get(event=event)

    def test_with_the_switch_off_the_proof_is_pending_but_the_api_says_nothing_covers_it(self):
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            corr, event, proof = self.sent([("letter.pdf", b"letter content")])
            shown = self.shown(corr)
        self.assertEqual((shown["state"], shown["attempts"], shown["automatic_archiving"], shown["files_archived"]), ("PENDING", 0, False, 0))
        self.assertEqual(shown["verification_level"], "none")             # never presented as verified
        self.assertIsNone(shown["archived_at"])
        self.assertFalse(shown["can_retry"])                              # the manual action also stays behind the switch
        self.assertEqual([m["file_name"] for m in shown["missing_attachments"]], ["letter.pdf"])

    def test_a_covered_send_is_flagged_as_automatic(self):
        corr, event, proof = self.sent([("letter.pdf", b"x")])
        self.assertTrue(self.shown(corr)["automatic_archiving"])

    def test_a_send_older_than_the_activation_date_is_not_covered(self):
        corr, event, proof = self.sent()
        later = self.t0 + timedelta(days=30)
        with override_settings(MAIL_SEND_PROOF_AUTO_ARCHIVE_SINCE=later):
            shown = self.shown(corr)
        self.assertEqual((shown["state"], shown["automatic_archiving"]), ("PENDING", False))
        self.assertTrue(shown["can_retry"])                               # ... but a person can still start it

    def test_the_proof_is_resumed_without_sending_again_and_idempotently(self):
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            corr, event, proof = self.sent([("a.pdf", b"aaa"), ("b.pdf", b"bbb")])
        sends_before = CorrespondenceSendEvent.objects.filter(correspondence=corr).count()
        row_before = CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get()
        with patch("requests.post", side_effect=AssertionError("sendMail must never be called")):
            from project_management.services.correspondence_send_proof_runner import retry_send_proof_archive

            first = retry_send_proof_archive(proof.pk, requested_by=self.owner, now=self.t0, reader=self.reader)
            files_after_first = sorted(proof.files.values_list("sha256", "kind"))
        proof.refresh_from_db()
        self.assertEqual((first.outcome, proof.state), ("archived", State.ARCHIVED))
        self.assertEqual(len(files_after_first), 3)                       # the .eml and the two attachments, each with its SHA-256
        self.assertEqual(CorrespondenceSendEvent.objects.filter(correspondence=corr).count(), sends_before)
        self.assertEqual(CorrespondenceSendEvent.objects.filter(pk=event.pk).values().get(), row_before)
        shown = self.shown(corr)
        self.assertEqual((shown["state"], shown["verification_level"], shown["missing_attachments"]), ("ARCHIVED", "full", []))
        # a second run is refused, nothing is duplicated
        from project_management.services.correspondence_send_proof_runner import SendProofRetryRefused, retry_send_proof_archive as again

        with self.assertRaises(SendProofRetryRefused):
            again(proof.pk, requested_by=self.owner, now=self.t0, reader=self.reader)
        self.assertEqual(sorted(proof.files.values_list("sha256", "kind")), files_after_first)
        self.assertEqual(CorrespondenceSendProofFile.objects.filter(proof=proof).count(), 3)

    def test_a_failed_archiving_is_explicit_and_never_verified(self):
        corr, event, proof = self.sent([("a.pdf", b"aaa")])
        CorrespondenceSendProof.objects.filter(pk=proof.pk).update(state=State.FAILED, error_code="temporary", attempts=2)
        shown = self.shown(corr)
        self.assertEqual((shown["state"], shown["issue"], shown["verification_level"], shown["automatic_archiving"]),
                         ("FAILED", "temporary_problem", "none", True))

    def test_the_new_field_exposes_nothing_internal(self):
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            corr, event, proof = self.sent([("letter.pdf", b"letter content")])
            text = json.dumps(self.history(corr).json())
        for secret in ("send_evidence", "graph_attachment_id", "error_code", "x-links-send", "MAIL_SEND_PROOF", "AUTO_ARCHIVE_SINCE"):
            self.assertNotIn(secret, text)

    def test_a_viewer_sees_the_state_but_cannot_retry_and_a_foreign_company_sees_nothing(self):
        with override_settings(MAIL_SEND_PROOF_ARCHIVING_ENABLED=False):
            corr, event, proof = self.sent([("a.pdf", b"aaa")])
        self.assertFalse(self.history(corr, self.viewer).json()["events"][0]["proof"]["can_retry"])
        self.assertIn(self.history(corr, self.foreign).status_code, (403, 404))
