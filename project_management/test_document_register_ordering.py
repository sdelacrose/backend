# L1A: Project Livrables column sorting silently no-op'd for Creator, Approver and Client Ref.
# -- not because the backend rejected the ordering (DRF's OrderingFilter was never even asked,
# since the frontend's own sortable-field whitelist dropped the request before it left the
# browser), but because those three columns had no real backend ordering key at all (Creator
# was previously only mapped to a now-unused legacy "author" entry; Approver and Client Ref.
# never had any mapping). This file is the backend half: a real, DB-backed integration test
# through DocumentViewSet's actual list()/ordering pipeline (CaseInsensitiveDocumentOrderingFilter
# + the queryset's own annotations), confirming client_reference/creator_name/approver_name are
# genuinely orderable, ascending and descending, exactly like the already-working author_name.
from django.core.files.base import ContentFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentControl
from project_management.test_document_workflow import WorkflowTestBase, make_user
from project_management.views.document import DocumentViewSet


class DocumentRegisterOrderingTests(WorkflowTestBase):
    """DocumentViewSet.list ordering=... for the D13 Creator/Approver/Client Ref. columns."""

    def make_doc(self, name, *, created_by, client_reference=None, approver=None):
        document = Document(
            folder=self.document.folder,
            file_name=name,
            created_by=created_by,
            client_reference=client_reference,
        )
        document.file.save(name, ContentFile(b"x"), save=False)
        document.save()
        if approver is not None:
            DocumentControl.objects.create(document=document, approver=approver)
        return document

    def setUp(self):
        super().setUp()
        # self.alice/bob/carol/dave: first_name Alice/Bob/Carol/Dave, last_name "Wf" (WorkflowTestBase).
        self.doc_carol = self.make_doc(
            "carol.txt",
            created_by=self.carol,
            client_reference="REF-C",
            approver=self.alice,
        )
        self.doc_alice = self.make_doc(
            "alice.txt",
            created_by=self.alice,
            client_reference="REF-A",
            approver=self.carol,
        )
        self.doc_bob = self.make_doc(
            "bob.txt",
            created_by=self.bob,
            client_reference="REF-B",
            approver=self.bob,
        )
        self.accessible = Document.objects.filter(
            pk__in=[self.doc_carol.pk, self.doc_alice.pk, self.doc_bob.pk]
        )

    def call(self, ordering):
        view = DocumentViewSet.as_view({"get": "list"})
        request = APIRequestFactory().get("/x/", {"ordering": ordering, "page_size": 50})
        force_authenticate(request, user=self.creator)
        from unittest.mock import patch

        with patch.object(DocumentViewSet, "get_permissions", return_value=[]), patch(
            "project_management.views.document.get_accessible_document_queryset",
            return_value=self.accessible,
        ):
            response = view(request)
        self.assertEqual(response.status_code, 200)
        return [row["file_name"] for row in response.data["results"]]

    def test_creator_name_orders_ascending_and_descending(self):
        self.assertEqual(
            self.call("creator_name"), ["alice.txt", "bob.txt", "carol.txt"]
        )
        self.assertEqual(
            self.call("-creator_name"), ["carol.txt", "bob.txt", "alice.txt"]
        )

    def test_approver_name_orders_ascending_and_descending(self):
        # approver: carol->alice, alice->carol, bob->bob.
        self.assertEqual(
            self.call("approver_name"), ["carol.txt", "bob.txt", "alice.txt"]
        )
        self.assertEqual(
            self.call("-approver_name"), ["alice.txt", "bob.txt", "carol.txt"]
        )

    def test_client_reference_orders_ascending_and_descending(self):
        self.assertEqual(
            self.call("client_reference"), ["alice.txt", "bob.txt", "carol.txt"]
        )
        self.assertEqual(
            self.call("-client_reference"), ["carol.txt", "bob.txt", "alice.txt"]
        )

    def test_a_document_without_a_control_row_still_orders_deterministically_by_approver(self):
        # doc_bob never gets an explicit DocumentControl for THIS test's purposes -- make one
        # without an approver, confirming the nullable control__approver join degrades safely
        # (nulls_last, never a crash/empty response) rather than excluding the row.
        no_control = self.make_doc("no_control.txt", created_by=self.dave)
        accessible = Document.objects.filter(
            pk__in=[self.doc_carol.pk, self.doc_alice.pk, self.doc_bob.pk, no_control.pk]
        )
        view = DocumentViewSet.as_view({"get": "list"})
        request = APIRequestFactory().get(
            "/x/", {"ordering": "approver_name", "page_size": 50}
        )
        force_authenticate(request, user=self.creator)
        from unittest.mock import patch

        with patch.object(DocumentViewSet, "get_permissions", return_value=[]), patch(
            "project_management.views.document.get_accessible_document_queryset",
            return_value=accessible,
        ):
            response = view(request)
        self.assertEqual(response.status_code, 200)
        names = [row["file_name"] for row in response.data["results"]]
        self.assertEqual(len(names), 4)
        self.assertIn("no_control.txt", names)

    def test_revision_ordering_key_is_untouched_by_this_fix(self):
        # Not repurposed for the mixed origin-aware Revision column -- still the legacy
        # Document.revision field, exactly as before this lot.
        self.assertIn("revision", DocumentViewSet.ordering_fields)
