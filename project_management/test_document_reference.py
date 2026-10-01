import re
from contextlib import ExitStack
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from models.company_codes import derive_company_code
from models.models import CompanyConfig

from project_management.models import Document, Folder
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import TWO, WorkflowTestBase
from project_management.test_transmittal import make_project

# Company library document, no project: COMPANY-DOCUMENT_TYPE-SEQUENCE (no discipline is
# ever selected in these tests, so that segment is skipped -- LOT 2D-12).
FORMAT = re.compile(r"^[A-Z0-9]+-PLA-\d{5}$")


class PermanentReferenceTests(WorkflowTestBase):
    """full_document_number is generated once at creation and never recalculated."""

    def setUp(self):
        super().setUp()
        # A real, canonical "Label:CODE" document type list: LOT 2D-12 removed the
        # silent 3-letter fallback, so the type used below ("Plan") needs a configured code.
        CompanyConfig.objects.update_or_create(
            company_profile=self.document.folder.company_profile,
            solution="Energy Consultant Solution",
            defaults={
                "document_types": ["Plan:PLA", "Report:REP", "Manual:MAN", "Design:DSN", "Other:OTH"],
            },
        )
        # The code actually persisted (auto-derived on first save of the CompanyProfile);
        # read it back rather than re-deriving it, to avoid any drift from a real collision.
        self.company_code = (
            self.document.folder.company_profile.company_code
            or derive_company_code(self.document.folder.company_profile.company_name)
        )

    def relaxed_configuration(self):
        # Configuration/permission lookups are not what is under test here.
        stack = ExitStack()
        stack.enter_context(
            patch(
                "project_management.serializers.document.get_valid_choices",
                return_value=["Plan", "Report", "Manual", "Design", "Other"],
            )
        )
        stack.enter_context(
            patch.object(
                DocumentSerializer,
                "_get_valid_choices_for_config_key",
                return_value=[],
            )
        )
        stack.enter_context(
            patch.object(DocumentSerializer, "validate_folder", lambda self, value: value)
        )
        stack.enter_context(
            patch.object(DocumentSerializer, "validate_discipline", lambda self, value: value)
        )
        return stack

    def create_document(self, name="ref.txt"):
        serializer = DocumentSerializer(
            data={
                "folder": self.document.folder_id,
                "file": SimpleUploadedFile(name, b"content"),
                "document_type": "Plan",
                "revision": 0,  # legacy field: no longer part of the reference
            },
            context={"user": self.creator},
        )
        with self.relaxed_configuration():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def update(self, document, **data):
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk),
            data=data,
            partial=True,
            context={"user": self.creator},
        )
        with self.relaxed_configuration():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def reference(self, document):
        return Document.objects.get(pk=document.pk).full_document_number

    # --- creation --------------------------------------------------------------------

    def test_new_document_receives_a_reference_in_the_existing_format(self):
        document = self.create_document()
        self.assertRegex(document.full_document_number, FORMAT)
        self.assertTrue(document.full_document_number.startswith(f"{self.company_code}-"))
        self.assertEqual(self.reference(document), document.full_document_number)

    def test_sequence_allocation_still_works_for_new_documents(self):
        first, second = self.create_document("a.txt"), self.create_document("b.txt")
        self.assertEqual(second.sequence_number, first.sequence_number + 1)
        self.assertNotEqual(first.full_document_number, second.full_document_number)
        self.assertTrue(first.full_document_number.endswith(f"-{first.sequence_number:05d}"))

    # --- metadata updates never recalculate it -----------------------------------------

    def test_metadata_updates_do_not_change_the_reference(self):
        document = self.create_document()
        original = document.full_document_number
        for change in (
            {"document_type": "Report"},
            {"category": "Design"},
            {"discipline": "Civil"},
            {"status": "Issued"},
            {"identification_number": "ID-42"},
            {"transmittal_category": "For review"},
            {"revision": 7},  # legacy Document.revision
            {"revision": 0, "document_type": "Manual", "category": "Other"},
        ):
            self.update(document, **change)
            self.assertEqual(self.reference(document), original, change)

    def test_direct_attempt_to_replace_the_reference_is_ignored(self):
        document = self.create_document()
        original = document.full_document_number
        self.update(document, full_document_number="HACKED-00001-00", status="Sent")
        self.assertEqual(self.reference(document), original)
        self.assertEqual(Document.objects.get(pk=document.pk).status, "Sent")

    def test_a_legacy_blank_reference_stays_blank_on_unrelated_updates(self):
        legacy = Document(
            folder=self.document.folder, file_name="legacy.txt", created_by=self.creator
        )
        legacy.file.save("legacy.txt", ContentFile(b"x"), save=False)
        legacy.save()
        self.assertFalse(legacy.full_document_number)
        self.update(legacy, status="Issued", document_type="Plan")
        self.assertFalse(self.reference(legacy))

    # --- versions and revisions never touch it ------------------------------------------

    def test_versions_approval_and_revision_labels_do_not_change_the_reference(self):
        document = self.create_document()
        original = document.full_document_number
        v1 = add_document_revision(
            document, ContentFile(b"v1", name="f.txt"), self.creator
        )
        self.assertEqual(self.reference(document), original)  # new version
        wf = self.start(TWO, revision=v1)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")  # approval + revision_label
        v1.refresh_from_db()
        self.assertEqual(v1.revision_label, "A")
        self.assertEqual(self.reference(document), original)
        add_document_revision(document, ContentFile(b"v2", name="f.txt"), self.creator)
        self.update(document, status="Issued")
        self.assertEqual(self.reference(document), original)


class OriginAwareReferenceTests(PermanentReferenceTests):
    """D13 live review section 8: Internal Ref. (full_document_number) is never forced onto a
    SUPPLIER/CLIENT-origin Document merely because LINKS stores it. Section 17 I/J/K/L/M/S/T.

    A Company-library Document's origin is always Internal (existing LOT 3D-3C0 rule) --
    SUPPLIER/CLIENT only exist in a Project folder, so these tests need their own Project
    folder, not self.document's own Company-library one.
    """

    def setUp(self):
        super().setUp()
        project = make_project(self.document.folder.company_profile, self.creator)
        self.project_folder = Folder.objects.create(project=project, name="D13 Origin Folder")

    def create_origin_document(self, origin, name="origin-ref.txt", **extra):
        # Exercises the serializer's create() directly (same pattern already established in
        # test_document_numbering.py, including the same get_config patch -- .create() called
        # without a prior is_valid() has no self.initial_data, which that method needs) -- the
        # origin-aware reference-generation guard is what is under test here, not the unrelated
        # issuer-network/document-type-code validation rules a full is_valid() pass would also
        # require satisfying.
        with patch.object(DocumentSerializer, "get_config", return_value=None):
            return DocumentSerializer(context={"user": self.creator}).create(
                {
                    "folder": self.project_folder,
                    "author": self.creator,
                    "file_name": name,
                    "document_origin": origin,
                    **extra,
                }
            )

    def test_supplier_document_has_no_internal_reference(self):  # I, L, M
        document = self.create_origin_document(
            "SUPPLIER", identification_number="VEN-4578-DWG-002",
        )
        self.assertIsNone(document.full_document_number)
        self.assertIsNone(document.sequence_number)
        self.assertEqual(document.identification_number, "VEN-4578-DWG-002")

    def test_client_document_has_no_internal_reference(self):  # J
        document = self.create_origin_document(
            "CLIENT", client_reference="CLI-PROJ-MEC-0187",
        )
        self.assertIsNone(document.full_document_number)
        self.assertIsNone(document.sequence_number)
        self.assertEqual(document.client_reference, "CLI-PROJ-MEC-0187")

    def test_document_with_no_origin_specified_defaults_to_internal_generation(self):  # K
        # The model's own default (DocumentOrigin.INTERNAL) -- confirms the origin-aware guard
        # reads the same default, not a stricter one that would regress existing callers.
        document = self.create_document()
        self.assertRegex(document.full_document_number, FORMAT)

    def test_supplier_and_client_references_persist_independently(self):  # S
        document = self.create_origin_document(
            "SUPPLIER",
            identification_number="VEN-4578-DWG-002",
            client_reference="CLI-PROJ-MEC-0187",
        )
        document.refresh_from_db()
        self.assertEqual(document.identification_number, "VEN-4578-DWG-002")
        self.assertEqual(document.client_reference, "CLI-PROJ-MEC-0187")
        self.assertIsNone(document.full_document_number)

    def test_client_reference_serializes_independently_of_supplier_reference(self):  # S
        document = self.create_origin_document(
            "CLIENT", client_reference="CLI-PROJ-MEC-0187",
        )
        from project_management.serializers.document import DocumentSerializer as DS

        data = DS(document, context={"include_control": True}).data
        self.assertEqual(data["client_reference"], "CLI-PROJ-MEC-0187")
        self.assertIsNone(data["identification_number"])

    def test_all_three_references_are_searchable(self):  # T
        from project_management.views.document import DocumentViewSet

        self.assertIn("identification_number", DocumentViewSet.search_fields)
        self.assertIn("full_document_number", DocumentViewSet.search_fields)
        self.assertIn("client_reference", DocumentViewSet.search_fields)
