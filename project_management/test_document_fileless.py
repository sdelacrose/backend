from unittest.mock import patch

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework import serializers
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document,
    DocumentAttachment,
    DocumentRevision,
    DocumentWorkflow,
)
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_workflow import (
    annotate_current_workflow,
    start_workflow_for_document,
)
from project_management.services.project_codes import reference_convention_is_frozen
from project_management.test_document_control_revision_workflow import ProjectRevisionBase
from project_management.test_document_workflow import TWO
from project_management.views.document_revision import DocumentRevisionViewSet

ACCESS = "project_management.views.document_revision.get_register_document_or_404"
CAN_UPDATE = "project_management.views.document_revision.assert_can_update_document"


class FilelessBase(ProjectRevisionBase):
    """A register entry (Document) may exist before its main controlled file."""

    def setUp(self):
        super().setUp()
        self.project, self.folder = self.make_project("Marseille Office Renovation")
        self.project.project_code = "MRS"
        self.project.save()

    def create_fileless(self, title="Plan v1.2", **extra):
        serializer = DocumentSerializer(
            data={
                "folder": self.folder.id,
                "file_name": title,
                "document_type": "Drawing",
                "discipline": "Structure",
                **extra,
            },
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def internal_status(self, document, annotated=False):
        queryset = Document.objects.filter(pk=document.pk)
        instance = (
            annotate_current_workflow(queryset).get()
            if annotated
            else Document.objects.get(pk=document.pk)
        )
        serializer = DocumentSerializer(
            instance, context={"user": self.creator, "include_control": True}
        )
        return serializer.data["internal_status"]

    def revisions_api(self, method, document, data=None, can_update=True):
        view = DocumentRevisionViewSet.as_view(
            {method: "list" if method == "get" else "create"}, permission_classes=[]
        )
        factory = APIRequestFactory()
        request = (
            factory.post("/x/", data, format="multipart")
            if method == "post"
            else factory.get("/x/")
        )
        force_authenticate(request, user=self.creator)
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, document_id=document.id)

    def upload_first_file(self, document, name="plan.pdf", **fields):
        response = self.revisions_api(
            "post", document, {"file": SimpleUploadedFile(name, b"pdf"), **fields}
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        return response.data

    def legacy_document(self):
        legacy = Document(folder=self.folder, file_name="old.txt", created_by=self.creator)
        legacy.file.save("old.txt", ContentFile(b"old"), save=False)
        legacy.save()
        return legacy


class FilelessCreationTests(FilelessBase):
    def test_a_document_can_be_created_without_a_file(self):
        document = self.create_fileless()
        self.assertFalse(document.file)
        self.assertEqual(document.file.name or "", "")
        self.assertEqual(document.file_name, "Plan v1.2")
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)

    def test_a_title_is_required_when_there_is_no_file(self):
        serializer = DocumentSerializer(
            data={"folder": self.folder.id, "document_type": "Drawing"},
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertFalse(serializer.is_valid())
        self.assertIn("file_name", serializer.errors)
        blank = DocumentSerializer(
            data={"folder": self.folder.id, "file_name": "  ", "document_type": "Drawing"},
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertFalse(blank.is_valid())

    def test_the_permanent_reference_is_generated_and_freezes_the_convention(self):
        self.assertFalse(reference_convention_is_frozen(self.project))
        document = self.create_fileless()
        self.assertTrue(document.full_document_number.startswith("MRS-STR-DWG-"))
        self.assertIsNotNone(document.sequence_number)
        self.assertTrue(reference_convention_is_frozen(self.project))

    def test_creating_with_a_file_still_works_as_before(self):
        document = self.create_doc(self.folder)
        self.assertTrue(document.file)
        self.assertTrue(document.full_document_number)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)
        self.assertEqual(self.internal_status(document), "DRAFT")


class ToCreateStatusTests(FilelessBase):
    def test_a_fileless_document_is_to_create_in_detail_and_in_the_list(self):
        document = self.create_fileless()
        self.assertEqual(self.internal_status(document), "TO_CREATE")
        self.assertEqual(self.internal_status(document, annotated=True), "TO_CREATE")

    def test_the_business_status_is_never_touched_by_the_lifecycle(self):
        document = self.create_fileless(status="Issued")
        self.assertEqual(self.internal_status(document), "TO_CREATE")
        self.assertEqual(Document.objects.get(pk=document.pk).status, "Issued")

    def test_a_legacy_document_with_a_file_is_draft_not_to_create(self):
        legacy = self.legacy_document()
        self.assertEqual(self.internal_status(legacy), "DRAFT")
        self.assertEqual(self.internal_status(legacy, annotated=True), "DRAFT")

    def test_a_supporting_attachment_does_not_leave_to_create(self):
        document = self.create_fileless()
        attachment = DocumentAttachment(
            document=document, file_name="annex.txt", uploaded_by=self.creator
        )
        attachment.file = ContentFile(b"annex", name="annex.txt")
        attachment.save()
        fresh = Document.objects.get(pk=document.pk)
        self.assertEqual(self.internal_status(document), "TO_CREATE")
        self.assertEqual(self.internal_status(document, annotated=True), "TO_CREATE")
        self.assertFalse(fresh.file)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)
        # an attachment can never be the workflow file
        with self.assertRaises(DjangoValidationError):
            start_workflow_for_document(
                document, TWO, self.alice, None, self.carol, self.creator
            )


class RevisionsEndpointTests(FilelessBase):
    def test_a_fileless_document_has_no_versions_and_reading_creates_none(self):
        document = self.create_fileless()
        for _ in range(2):
            response = self.revisions_api("get", document)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["results"], [])
            self.assertEqual(response.data["count"], 0)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)
        self.assertFalse(Document.objects.get(pk=document.pk).file)

    def test_a_legacy_document_still_gets_its_synthetic_version_one(self):
        legacy = self.legacy_document()
        response = self.revisions_api("get", legacy)
        rows = response.data["results"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["id"])
        self.assertEqual(rows[0]["version_number"], 1)
        self.assertIsNone(rows[0]["revision_label"])
        self.assertEqual(DocumentRevision.objects.filter(document=legacy).count(), 0)


class FirstMainFileTests(FilelessBase):
    def test_the_first_main_file_creates_version_one_without_type_or_comment(self):
        document = self.create_fileless(status="Issued")
        reference = document.full_document_number
        data = self.upload_first_file(document)  # no revision_type, no comment
        revision = DocumentRevision.objects.get(document=document)
        self.assertEqual(data["version_number"], 1)
        self.assertEqual(revision.version_number, 1)
        self.assertIsNone(revision.revision_label)
        self.assertEqual((revision.major_revision, revision.minor_revision), ("0", 0))
        self.assertEqual(revision.label, "0-00")
        fresh = Document.objects.get(pk=document.pk)
        self.assertEqual(fresh.file.name, revision.file.name)  # compatibility pointer
        self.assertEqual(self.internal_status(fresh), "DRAFT")
        self.assertEqual(self.internal_status(fresh, annotated=True), "DRAFT")
        self.assertEqual(fresh.status, "Issued")
        self.assertEqual(fresh.full_document_number, reference)
        self.assertFalse(DocumentWorkflow.objects.filter(revision=revision).exists())
        listing = self.revisions_api("get", fresh).data["results"]
        self.assertEqual([r["version_number"] for r in listing], [1])

    def test_the_first_file_is_required(self):
        document = self.create_fileless()
        response = self.revisions_api("post", document, {"comment": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.data)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)

    def test_the_title_is_kept_and_never_mangled_by_the_extension(self):
        document = self.create_fileless("Plan v1.2")
        self.upload_first_file(document, "whatever.pdf")
        self.assertEqual(Document.objects.get(pk=document.pk).file_name, "Plan v1.2.pdf")
        plain = self.create_fileless("Site report")
        self.upload_first_file(plain, "r.docx")
        self.assertEqual(Document.objects.get(pk=plain.pk).file_name, "Site report.docx")
        already = self.create_fileless("Schema.pdf")
        self.upload_first_file(already, "x.pdf")
        self.assertEqual(Document.objects.get(pk=already.pk).file_name, "Schema.pdf")

    def test_a_later_new_file_still_swaps_the_extension_of_the_title(self):
        document = self.create_fileless("Plan v1.2")
        self.upload_first_file(document, "a.docx")
        self.assertEqual(Document.objects.get(pk=document.pk).file_name, "Plan v1.2.docx")
        response = self.revisions_api(
            "post",
            document,
            {
                "file": SimpleUploadedFile("b.pdf", b"b"),
                "revision_type": "minor",
                "comment": "pdf now",
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Document.objects.get(pk=document.pk).file_name, "Plan v1.2.pdf")

    def test_the_workflow_is_refused_before_and_works_after_the_first_file(self):
        document = self.create_fileless()
        with self.assertRaises(DjangoValidationError) as refused:
            start_workflow_for_document(
                document, TWO, self.alice, None, self.carol, self.creator
            )
        self.assertIn("no file to review", str(refused.exception))
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)
        self.assertFalse(Document.objects.get(pk=document.pk).file)
        self.upload_first_file(document)
        workflow = start_workflow_for_document(
            Document.objects.get(pk=document.pk),
            TWO, self.alice, None, self.carol, self.creator,
        )
        self.assertEqual(workflow.revision.version_number, 1)
        self.assertEqual(self.internal_status(document), "IN_VALIDATION")

    def test_later_versions_keep_their_rules_and_numbering(self):
        document = self.create_fileless()
        self.upload_first_file(document)
        missing = self.revisions_api(
            "post", document, {"file": SimpleUploadedFile("v2.pdf", b"2")}
        )
        self.assertEqual(missing.status_code, 400)
        self.assertIn("revision_type", missing.data)
        self.assertIn("comment", missing.data)
        ok = self.revisions_api(
            "post",
            document,
            {"file": SimpleUploadedFile("v2.pdf", b"2"), "revision_type": "minor",
             "comment": "second"},
        )
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.data["version_number"], 2)
        self.assertEqual(ok.data["label"], "0-01")
        numbers = list(
            DocumentRevision.objects.filter(document=document)
            .order_by("version_number").values_list("version_number", flat=True)
        )
        self.assertEqual(numbers, [1, 2])

    def test_the_reference_never_changes_after_the_first_upload(self):
        document = self.create_fileless()
        before = document.full_document_number
        self.upload_first_file(document)
        self.assertEqual(Document.objects.get(pk=document.pk).full_document_number, before)


class FileUpdateGuardTests(FilelessBase):
    def patch_document(self, document, **data):
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk), data=data, partial=True,
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def test_an_ordinary_update_cannot_replace_the_main_file(self):
        document = self.create_fileless()
        with self.assertRaises(serializers.ValidationError) as ctx:
            self.patch_document(document, file=SimpleUploadedFile("sneaky.pdf", b"x"))
        self.assertIn("file", ctx.exception.detail)
        fresh = Document.objects.get(pk=document.pk)
        self.assertFalse(fresh.file)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)

    def test_an_ordinary_update_cannot_replace_the_file_of_a_document_with_one(self):
        document = self.create_doc(self.folder)
        original = document.file.name
        with self.assertRaises(serializers.ValidationError):
            self.patch_document(document, file=SimpleUploadedFile("sneaky.pdf", b"x"))
        self.assertEqual(Document.objects.get(pk=document.pk).file.name, original)
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), 0)

    def test_metadata_updates_still_work(self):
        document = self.create_fileless()
        reference = document.full_document_number
        self.patch_document(document, status="Issued", identification_number="ID-9")
        fresh = Document.objects.get(pk=document.pk)
        self.assertEqual(fresh.status, "Issued")
        self.assertEqual(fresh.identification_number, "ID-9")
        self.assertEqual(fresh.full_document_number, reference)
        self.assertFalse(fresh.file)
