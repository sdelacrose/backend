from django.core.files.base import ContentFile

from project_management.models import Document, DocumentRevision
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import (
    annotate_current_workflow,
    perform_action,
    start_workflow,
)
from project_management.test_document_fileless import FilelessBase
from project_management.test_document_workflow import TWO


class RegisterRevisionTests(FilelessBase):
    """The Register shows the official Revision of the CURRENT version, never Document.revision."""

    def register_label(self, document, annotated=True):
        queryset = Document.objects.filter(pk=document.pk)
        instance = annotate_current_workflow(queryset).get() if annotated else queryset.get()
        data = DocumentSerializer(
            instance, context={"user": self.creator, "include_control": True}
        ).data
        return data["revision_label"], data["revision"]

    def approve_current(self, document):
        revision = DocumentRevision.objects.filter(document=document).order_by("-version_number").first()
        workflow = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(workflow, self.alice, "SIGN", "s")
        perform_action(workflow, self.carol, "APPROVE", "a")

    def test_no_official_revision_yet(self):
        fileless = self.create_fileless()
        self.assertEqual(self.register_label(fileless)[0], None)
        with_file = self.create_fileless("Has file")
        self.upload_first_file(with_file)
        self.assertEqual(self.register_label(with_file)[0], None)
        legacy = self.legacy_document()  # file, no stored version
        self.assertEqual(self.register_label(legacy)[0], None)

    def test_approved_revision_a_then_later_current_revision_c_and_legacy_zero_is_ignored(self):
        document = self.create_fileless()
        self.upload_first_file(document)
        self.approve_current(document)
        self.assertEqual(self.register_label(document)[0], "A")
        for expected in ("B", "C"):
            add_document_revision(document, ContentFile(b"n", name="n.pdf"), self.creator)
            self.approve_current(document)
            self.assertEqual(self.register_label(document)[0], expected)
        label, legacy = self.register_label(document)
        self.assertEqual(label, "C")
        self.assertEqual(legacy, 0)  # the legacy field is still 0 and must not be what is displayed

    def test_a_new_unapproved_version_has_no_revision_yet(self):
        document = self.create_fileless()
        self.upload_first_file(document)
        self.approve_current(document)
        add_document_revision(document, ContentFile(b"n", name="n.pdf"), self.creator)
        self.assertEqual(self.register_label(document)[0], None)  # same as the Detail header

    def test_not_annotated_detail_lookup_agrees(self):
        document = self.create_fileless()
        self.upload_first_file(document)
        self.approve_current(document)
        self.assertEqual(self.register_label(document, annotated=False)[0], "A")
