"""HISTORY-3: the Revision History list endpoint's new Supplier Surveillance block --
targeted tests only. INTERNAL's own equivalent (own exact workflow per revision, old
revisions staying visible, N+1 avoidance) is already fully covered by
test_document_revision_history.py and is unchanged by this lot; these tests cover only the
new `surveillance` key this lot adds to the same existing read path.
"""

from unittest.mock import patch

from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.services.document_revision import add_document_revision
from project_management.test_transmittal import TransmittalTestBase
from project_management.views.document_revision import DocumentRevisionViewSet

ACCESS = "project_management.views.document_revision.get_register_document_or_404"


class SupplierRevisionHistoryBase(TransmittalTestBase):
    def history(self, document):
        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.owner)
        with patch(ACCESS, return_value=document):
            response = view(request, document_id=document.id)
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    def row(self, document, revision):
        return next(r for r in self.history(document) if r["id"] == revision.id)

    def add_supplier_revision(self, document, external_revision, content=b"v"):
        return add_document_revision(
            document, self._uploaded(content), self.owner,
            revision_type="minor", external_revision=external_revision,
        )

    @staticmethod
    def _uploaded(content):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile("rev.txt", content)


# 8/9. Multiple Supplier revisions all returned, each with its own exact Surveillance.
class MultipleRevisionsTests(SupplierRevisionHistoryBase):
    def test_multiple_supplier_revisions_all_returned_with_their_own_surveillance(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        revision_a = self.add_supplier_revision(document, "A", b"a")
        revision_b = self.add_supplier_revision(document, "B", b"b")
        surveillance_a = self.make_surveillance(revision_a, "CLOSED", result="REJECTED")
        surveillance_b = self.make_surveillance(revision_b, "CLOSED", result="REVIEWED")

        rows = self.history(document)
        # 3, not 2: add_document_revision's own documented behavior silently materializes
        # the Document's pre-existing file as an unlabeled Version 1 the first time it is
        # called (services.document_revision._materialize_first_revision) -- itself a real,
        # permanently-visible revision with no Surveillance, never this test's concern.
        self.assertEqual(len(rows), 3)
        row_a, row_b = self.row(document, revision_a), self.row(document, revision_b)
        self.assertEqual(row_a["surveillance"]["id"], surveillance_a.id)
        self.assertEqual(row_b["surveillance"]["id"], surveillance_b.id)
        self.assertNotEqual(row_a["surveillance"]["id"], row_b["surveillance"]["id"])


# 10/13. Supplier A REJECTED remains visible/unchanged after Supplier B REVIEWED exists;
# new_revision_required and review_due_date/is_overdue surface correctly per revision.
class RevisionAStaysVisibleTests(SupplierRevisionHistoryBase):
    def test_rejected_revision_a_remains_rejected_after_b_is_reviewed(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        revision_a = self.add_supplier_revision(document, "A", b"a")
        self.make_surveillance(revision_a, "CLOSED", result="REJECTED")
        revision_a.refresh_from_db()

        revision_b = self.add_supplier_revision(document, "B", b"b")
        self.make_surveillance(revision_b, "CLOSED", result="REVIEWED")

        row_a = self.row(document, revision_a)
        self.assertEqual(row_a["surveillance"]["status"], "CLOSED")
        self.assertEqual(row_a["surveillance"]["result"], "REJECTED")

    def test_new_revision_required_and_review_due_fields_surface(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        revision = self.add_supplier_revision(document, "A", b"a")
        surveillance = self.make_surveillance(revision, "CLOSED", result="REJECTED")
        surveillance.new_revision_required = True
        surveillance.save(update_fields=["new_revision_required"])

        row = self.row(document, revision)
        self.assertTrue(row["surveillance"]["new_revision_required"])
        self.assertIn("review_due_date", row["surveillance"])
        self.assertIn("is_overdue", row["surveillance"])


# 11. A FOR_INFORMATION revision with no mandatory Surveillance is not treated as an error.
class ForInformationNoSurveillanceTests(SupplierRevisionHistoryBase):
    def test_for_information_revision_without_surveillance_has_no_surveillance_block(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        document.supplier_transmittal_category = "FOR_INFORMATION"
        document.save(update_fields=["supplier_transmittal_category"])
        revision = self.add_supplier_revision(document, "A", b"a")

        row = self.row(document, revision)
        self.assertIsNone(row["surveillance"])


# N+1 avoidance, mirroring test_document_revision_history.py's own
# test_workflow_actors_do_not_add_queries_per_version.
class QueryCountTests(SupplierRevisionHistoryBase):
    def test_surveillance_lookup_does_not_add_queries_per_revision(self):
        document = self.make_document(origin="SUPPLIER", name="s.txt")
        revision = self.add_supplier_revision(document, "A", b"a")
        self.make_surveillance(revision, "CLOSED", result="REVIEWED")
        with CaptureQueriesContext(connection) as small:
            self.history(document)
        for letter in "BCD":
            revision = self.add_supplier_revision(document, letter, letter.encode())
            self.make_surveillance(revision, "CLOSED", result="REVIEWED")
        with CaptureQueriesContext(connection) as large:
            self.history(document)
        self.assertEqual(len(small), len(large))


# 16/17. CLIENT-origin: external_revision is the Client Revision, no fabricated process.
class ClientOriginHistoryTests(SupplierRevisionHistoryBase):
    def test_client_origin_revision_has_external_revision_and_no_fabricated_process(self):
        revision = self.make_client_revision()
        document = revision.document
        row = self.row(document, revision)
        self.assertIsNone(row["workflow"])
        self.assertIsNone(row["surveillance"])


# 18. Security: an inaccessible Document's history is never exposed. Same established
# pattern as test_document_revision.py's own test_inaccessible_document_is_a_404_for_history_
# and_download: mock the access-resolution helper itself (its own correctness is tested where
# it is defined) and verify this view propagates its 404, never falling through to serialize
# any surveillance/workflow data regardless.
class SecurityTests(SupplierRevisionHistoryBase):
    def test_inaccessible_document_returns_404_not_history_data(self):
        from django.http import Http404

        document = self.make_document(origin="SUPPLIER", name="s.txt")
        revision = self.add_supplier_revision(document, "A", b"a")
        self.make_surveillance(revision, "CLOSED", result="REVIEWED")

        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.other_owner)
        with patch(ACCESS, side_effect=Http404):
            self.assertEqual(view(request, document_id=document.id).status_code, 404)
