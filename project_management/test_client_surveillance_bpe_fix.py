"""SURV-BPE-FIX: the verified send of a BPE (or of any version born from a client return) never creates a document revision.
The revisions of the document are counted before and after every step: the send only closes the follow-up / makes the index
applicable; a later revision exists only on an explicit user action."""
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile

from project_management.models import ClientSurveillanceFollowUp, DocumentRevision, TransmittalEvent
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.document_revision import MAJOR, add_document_revision, create_document_revision
from project_management.services.transmittal_send import send_transmittal
from project_management.test_client_surveillance_followup import SMTP_FAIL
from project_management.test_client_surveillance_lot3 import Lot3Base

Category = "ACCEPTED"


class NoExtraRevisionTests(Lot3Base):
    def rows(self, document=None):
        return list(DocumentRevision.objects.filter(document=document or self.document).order_by("pk")
                    .values_list("pk", "version_number", "revision_label", "issue_state", "index_revision_id"))

    def test_a_prel_to_a_bpe_send_creates_no_revision_and_the_bpe_stays_the_last_one(self):
        followup, bpe = self.journey("ACCEPTED")
        before = self.rows()
        self.assertEqual(before[-1][0], bpe.pk)
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        self.assertEqual(self.rows(), before)  # nothing created by the preparation, the finalization or the send
        self.assertEqual(self.rows()[-1][0], bpe.pk)
        followup = self.closed(followup)
        self.assertEqual((followup.closing_send_event_id, followup.applicable_at), (event.pk, event.created_at))
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED"))

    def test_the_preparation_and_the_finalization_of_the_transmittal_create_no_revision_either(self):
        followup, bpe = self.journey("ACCEPTED")
        before = self.rows()
        transmittal = self.issue([(bpe, "FOR_INFORMATION")])  # item added + finalized, not sent yet
        self.assertEqual(self.rows(), before)
        send_transmittal(transmittal, self.owner)
        self.assertEqual(self.rows(), before)

    def test_b_prel_to_a_return_to_b_bpe_send_creates_no_revision(self):
        # the source is the INDEX B (a second approved revision sent for review), the client answers A, the BPE keeps index B
        source = add_document_revision(self.document, SimpleUploadedFile("src-b.txt", b"source B"), self.owner, revision_type=MAJOR, comment="index B")
        source = self.approve(source)
        self.assertEqual(source.revision_label, "B")
        self.send_rev(source, "FOR_REVIEW")
        issue = source.client_issues.get(client_company=self.client_company)
        record = self.ret("ACCEPTED", issue=issue)
        followup = fu_service.confirm_retained_return(issue, self.updater, return_id=record.pk)
        bpe = self.provide_bpe(followup)
        before = self.rows()
        self.send_rev(bpe, "FOR_INFORMATION")
        self.assertEqual(self.rows(), before)
        self.assertEqual((bpe.index_revision_id, self.rows()[-1][0]), (source.pk, bpe.pk))
        self.assertIsNotNone(self.closed(followup).applicable_at)

    def test_a_prel_to_return_b_to_b_bpe_validated_then_sent_creates_no_revision(self):
        followup, revision = self.journey("ACCEPTED_WITH_COMMENTS")  # B file + internal approval
        self.assertEqual(revision.revision_label, "B")
        before = self.rows()
        self.send_rev(revision, "FOR_INFORMATION")
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.rows()[-1][0], revision.pk)
        self.assertEqual(self.closed(followup).applicable_at is not None, True)

    def test_a_failed_send_closes_nothing_and_creates_nothing(self):
        followup, bpe = self.journey("ACCEPTED")
        before = self.rows()
        transmittal = self.issue([(bpe, "FOR_INFORMATION")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            send_transmittal(transmittal, self.owner)
        self.assertEqual(self.rows(), before)
        followup = self.closed(followup)
        self.assertEqual((followup.closed_at, followup.applicable_at), (None, None))
        self.surveillance.refresh_from_db()
        self.assertNotEqual(self.surveillance.status, "CLOSED")

    def test_a_double_notification_of_the_same_send_creates_nothing_and_closes_once(self):
        followup, bpe = self.journey("ACCEPTED")
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        before = self.rows()
        first = self.closed(followup)
        self.assertEqual(fu_service.process_send_event(event), [])  # the replay finds the follow-up already closed
        self.assertEqual(fu_service.process_send_event(event), [])
        self.assertEqual(self.rows(), before)
        again = self.closed(followup)
        self.assertEqual((again.closed_at, again.applicable_at, again.closing_send_event_id), (first.closed_at, first.applicable_at, first.closing_send_event_id))
        self.assertEqual(self.surveillance.events.filter(event_type="AUTO_CLOSED").count(), 1)  # one closing event, no duplicate
        self.assertEqual(TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", pk=event.pk).count(), 1)

    def cd_check(self, category):
        record = self.ret(category)
        followup = self.confirm(record)
        before_file = self.rows()
        revision = fu_service.provide_file(followup, self.updater, self.file(f"prel-{category}"))
        self.assertEqual(len(self.rows()), len(before_file) + 1)  # the PREL revision: explicit file, created once
        revision = self.approve(revision)
        before_send = self.rows()
        self.send_rev(revision, "FOR_REVIEW")
        self.assertEqual(self.rows(), before_send)  # the send adds none (the new cycle is a surveillance, not a revision)
        self.assertEqual(self.rows()[-1][0], revision.pk)

    def test_c_creates_the_expected_prel_revision_once_and_the_send_creates_no_other(self):
        self.cd_check("COMMENTED")

    def test_d_creates_the_expected_prel_revision_once_and_the_send_creates_no_other(self):
        self.cd_check("REJECTED")

    def test_an_explicit_new_revision_after_the_bpe_is_still_possible(self):
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        before = self.rows()
        created = create_document_revision(self.document, SimpleUploadedFile("internal-change.txt", b"an internal modification"), self.owner,
                                           MAJOR, "Internal modification after the BPE")
        after = self.rows()
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1][0], created.pk)
        self.assertNotEqual(created.pk, bpe.pk)

    def test_history_and_statuses_stay_coherent_after_the_bpe_send(self):
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        kinds = self.kinds(self.surveillance)
        self.assertEqual(kinds.count("AUTO_CLOSED"), 1)
        self.assertEqual(kinds.count("FU_FILE"), 1)
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(derived_revision=bpe).count(), 1)
        self.assertEqual(DocumentRevision.objects.filter(index_revision=bpe).count(), 0)  # nothing derives from the BPE
