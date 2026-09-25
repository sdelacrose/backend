import re
from contextlib import ExitStack
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from project_management.models import Document
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import TWO, WorkflowTestBase

FORMAT = re.compile(r"^REV-Plan-\d{5}-00$")  # legacy composition, unchanged for now


class PermanentReferenceTests(WorkflowTestBase):
    """full_document_number is generated once at creation and never recalculated."""

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
                "revision": 0,  # the UI always sends the legacy revision (0 by default)
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
        self.assertEqual(self.reference(document), document.full_document_number)

    def test_sequence_allocation_still_works_for_new_documents(self):
        first, second = self.create_document("a.txt"), self.create_document("b.txt")
        self.assertEqual(second.sequence_number, first.sequence_number + 1)
        self.assertNotEqual(first.full_document_number, second.full_document_number)
        self.assertTrue(first.full_document_number.endswith(f"-{first.sequence_number:05d}-00"))

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
