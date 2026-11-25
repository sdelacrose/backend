"""OPTION B1 / LOT 3: every physical file (PREL / BPE) of a logical version is traceable: exact reference, fingerprint, download and the
distributions that really carried THIS file. Everything is read from the existing revision list and distribution history: no new
endpoint, no new relation, nothing written."""
import hashlib
from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.services.client_distribution_history import list_client_distribution_history
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_send import send_transmittal
from project_management.test_client_surveillance_followup import SMTP_FAIL
from project_management.test_document_logical_version import LogicalBase
from project_management.test_transmittal import make_user
from project_management.views.client_distribution_history import ClientDistributionHistoryView
from project_management.views.document_revision import DocumentRevisionViewSet


class FileTraceabilityTests(LogicalBase):
    def history(self, user=None):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user or self.manager)  # documents.view + surveillance detail
        return ClientDistributionHistoryView.as_view()(request, document_id=self.document.pk)

    def rows_of(self, revision, user=None):
        return [r for r in self.history(user).data["results"] if r["document_revision_id"] == revision.pk]

    def download(self, revision, user=None):
        view = DocumentRevisionViewSet.as_view({"get": "download"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user or self.owner)
        with patch("project_management.views.document_revision.get_register_document_or_404", return_value=self.document):
            return view(request, document_id=self.document.pk, revision_id=revision.pk)

    def test_a_file_never_distributed_has_no_distribution(self):  # PREL seul / fichier jamais diffusé
        followup, bpe = self.journey("ACCEPTED")
        self.assertEqual(self.rows_of(bpe), [])  # the BPE exists but was never sent: no row, nothing invented

    def test_prel_and_bpe_keep_their_own_distributions_and_exact_physical_references(self):
        followup, bpe = self.journey("ACCEPTED")
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        prel_rows, bpe_rows = self.rows_of(self.revision), self.rows_of(bpe)
        self.assertEqual([(r["transmittal_reference"], r["distribution_purpose"], r["send_state"]) for r in prel_rows],
                         [(self.transmittal.transmittal_number, "FOR_REVIEW", "SEND_ACCEPTED")])
        self.assertEqual([(r["transmittal_reference"], r["distribution_purpose"], r["send_state"], r["issue_state"], r["is_technical"]) for r in bpe_rows],
                         [(event.transmittal.transmittal_number, "FOR_INFORMATION", "SEND_ACCEPTED", "BPE", True)])
        self.assertTrue(all(r["document_revision_id"] == bpe.pk for r in bpe_rows))  # exact physical reference, never the logical one

    def test_several_sends_of_the_same_file_and_several_companies(self):
        self.second_send()  # the same PREL revision, sent again to the same client
        company, contact = self.second_client()
        self.issue_and_send([(self.revision, "FOR_REVIEW")], company=company, contacts=[contact])
        rows = self.rows_of(self.revision)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["client_company_name"] for r in rows}, {self.client_company.company_name, company.company_name})
        self.assertEqual(len({r["transmittal_reference"] for r in rows}), 3)

    def test_a_failed_send_is_not_shown_as_sent_and_a_draft_is_not_listed(self):
        followup, bpe = self.journey("ACCEPTED")
        draft = self.create_transmittal(external_company=self.client_company.id)
        add_transmittal_item(draft, bpe, "FOR_INFORMATION", self.owner)  # still a DRAFT: no distribution yet
        self.assertEqual(self.rows_of(bpe), [])
        issued = self.issue([(bpe, "FOR_INFORMATION")])
        self.assertEqual([r["send_state"] for r in self.rows_of(bpe)], ["NOT_SENT"])  # issued, not sent: never presented as sent
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            send_transmittal(issued, self.owner)
        rows = self.rows_of(bpe)
        self.assertEqual([r["send_state"] for r in rows], ["SEND_FAILED"])
        self.assertIsNone(rows[0]["sent_at"])

    def test_fingerprints_and_sizes_are_the_recorded_ones_and_match_the_frozen_transmittal_evidence(self):
        followup, bpe = self.journey("ACCEPTED")
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        listing = {r["id"]: r for r in self.api_list().data["results"]}
        for revision in (self.revision, bpe):
            self.assertEqual(listing[revision.pk]["file_sha256"], revision.file_sha256)
            self.assertEqual(listing[revision.pk]["file_size"], revision.file_size)
        self.assertNotEqual(listing[bpe.pk]["file_sha256"], listing[self.revision.pk]["file_sha256"])  # two distinct physical files
        self.assertEqual(event.transmittal.items.get().evidence_snapshot["revision"]["file_sha256"], listing[bpe.pk]["file_sha256"])

    def test_each_physical_file_downloads_with_its_own_bytes(self):
        followup, bpe = self.journey("ACCEPTED")
        for revision in (self.revision, bpe):
            response = self.download(revision)
            self.assertEqual(response.status_code, 200)
            content = b"".join(response.streaming_content) if hasattr(response, "streaming_content") else response.content
            self.assertEqual(hashlib.sha256(content).hexdigest(), revision.file_sha256)

    def test_a_reader_without_transmittal_rights_sees_the_distribution_but_not_the_references(self):
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        reader = self.grant("lot3.reader@links-local.invalid", self.project, ["documents.view"])
        masked = self.rows_of(bpe, user=reader)
        self.assertEqual(len(masked), 1)
        self.assertEqual((masked[0]["transmittal_reference"], masked[0]["transmittal_id"]), (None, None))  # not exposed
        self.assertEqual((masked[0]["client_company_name"], masked[0]["distribution_purpose"], masked[0]["document_revision_id"]),
                         (self.client_company.company_name, "FOR_INFORMATION", bpe.pk))  # the summary stays
        stranger = make_user("lot3.stranger@links-local.invalid")  # no role, no project membership
        self.assertIn(self.history(stranger).status_code, (403, 404))  # no access to the document: nothing exposed

    def test_the_distribution_history_has_no_n_plus_one(self):
        followup, bpe = self.journey("ACCEPTED")
        self.send_rev(bpe, "FOR_INFORMATION")
        with CaptureQueriesContext(connection) as few:
            list_client_distribution_history(self.document)
        for _ in range(3):
            self.second_send()
        with CaptureQueriesContext(connection) as many:
            rows = list_client_distribution_history(self.document)
        self.assertGreaterEqual(len(rows), 5)
        self.assertEqual(len(few), len(many))  # the number of queries does not grow with the number of distributions

    def test_the_revision_list_always_carries_the_file_metadata_keys(self):  # historical rows may hold None: shown as "not recorded"
        for row in self.api_list().data["results"]:
            for key in ("file_name", "file_size", "file_sha256", "created_at", "download_url", "id"):
                self.assertIn(key, row)
