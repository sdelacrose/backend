from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import QueryDict

from project_management.models import Document, DocumentRevision
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.test_document_fileless import FilelessBase
from project_management.test_document_workflow import TWO


class MetadataBase(FilelessBase):
    """description and tags are Document identity metadata (not versioned, not in the reference)."""

    def patch_document(self, document, **data):
        serializer = DocumentSerializer(
            Document.objects.get(pk=document.pk),
            data=data,
            partial=True,
            context={"user": self.creator},
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    def fresh(self, document):
        return Document.objects.get(pk=document.pk)

    def snapshot(self, document):
        fresh = self.fresh(document)
        return {
            "reference": fresh.full_document_number,
            "sequence": fresh.sequence_number,
            "file": fresh.file.name,
            "status": fresh.status,
            "internal": self.internal_status(fresh),
            "versions": list(
                DocumentRevision.objects.filter(document=fresh)
                .order_by("version_number")
                .values_list("id", "version_number", "revision_label", "file")
            ),
        }

    def with_file(self, **extra):
        document = self.create_fileless(**extra)
        self.upload_first_file(document)
        return self.fresh(document)


class DescriptionAndTagsFieldTests(MetadataBase):
    def test_defaults_are_an_empty_description_and_no_tags(self):
        document = self.create_fileless()
        fresh = self.fresh(document)
        self.assertEqual(fresh.description, "")
        self.assertEqual(fresh.tags, [])
        data = DocumentSerializer(fresh, context={"user": self.creator}).data
        self.assertEqual(data["description"], "")
        self.assertEqual(data["tags"], [])

    def test_create_with_description_and_tags(self):
        document = self.create_fileless(
            description="Calculation note for the west wing", tags=["Structural", "IFC"]
        )
        fresh = self.fresh(document)
        self.assertEqual(fresh.description, "Calculation note for the west wing")
        self.assertEqual(fresh.tags, ["Structural", "IFC"])
        data = DocumentSerializer(fresh, context={"user": self.creator}).data
        self.assertEqual(data["description"], fresh.description)
        self.assertEqual(data["tags"], ["Structural", "IFC"])

    def test_tags_are_trimmed_deduplicated_and_keep_their_capitalization(self):
        document = self.create_fileless(tags=[" Structural ", "IFC", "", "  ", "Structural", "ifc"])
        self.assertEqual(self.fresh(document).tags, ["Structural", "IFC", "ifc"])

    def test_tags_submitted_as_a_multipart_list_are_normalized(self):
        query = QueryDict(mutable=True)
        query.update({"folder": str(self.folder.id), "file_name": "Multipart", "document_type": "Drawing"})
        query.setlist("tags", [" A ", "B", "A"])
        serializer = DocumentSerializer(data=query, context={"user": self.creator})
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            document = serializer.save()
        self.assertEqual(self.fresh(document).tags, ["A", "B"])

    def test_metadata_patch_sets_and_clears_description_and_tags(self):
        document = self.create_fileless()
        self.patch_document(document, description="First", tags=["One", "Two"])
        fresh = self.fresh(document)
        self.assertEqual((fresh.description, fresh.tags), ("First", ["One", "Two"]))
        self.patch_document(document, description="Second")
        self.assertEqual(self.fresh(document).tags, ["One", "Two"])  # untouched by a partial patch
        self.patch_document(document, tags=["Three"])
        self.assertEqual(self.fresh(document).description, "Second")
        self.patch_document(document, description="", tags=[])
        fresh = self.fresh(document)
        self.assertEqual((fresh.description, fresh.tags), ("", []))


class MetadataIsolationTests(MetadataBase):
    def test_changing_metadata_never_touches_the_reference_versions_file_or_statuses(self):
        document = self.with_file(status="Issued")
        before = self.snapshot(document)
        self.patch_document(document, description="Context")
        self.assertEqual(self.snapshot(document), before)
        self.patch_document(document, tags=["a", "b"])
        self.assertEqual(self.snapshot(document), before)
        self.assertEqual(before["internal"], "DRAFT")
        self.assertEqual(before["status"], "Issued")
        self.assertEqual(len(before["versions"]), 1)

    def test_the_project_reference_convention_and_code_are_unaffected(self):
        document = self.with_file()
        code = self.project.project_code
        self.patch_document(document, description="x", tags=["y"])
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_code, code)
        self.assertTrue(self.fresh(document).full_document_number.startswith(f"{code}-"))

    def test_metadata_survives_new_versions(self):
        document = self.with_file(description="Kept", tags=["Keep"])
        response = self.revisions_api(
            "post",
            document,
            {
                "file": SimpleUploadedFile("v2.pdf", b"2"),
                "revision_type": "minor",
                "comment": "second",
            },
        )
        self.assertEqual(response.status_code, 201)
        fresh = self.fresh(document)
        self.assertEqual((fresh.description, fresh.tags), ("Kept", ["Keep"]))
        self.assertEqual(DocumentRevision.objects.filter(document=fresh).count(), 2)

    def test_metadata_survives_workflow_start_rejection_and_approval(self):
        document = self.with_file(description="Kept", tags=["Keep"])
        revision = DocumentRevision.objects.get(document=document)
        workflow = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        self.assertEqual(
            (self.fresh(document).description, self.fresh(document).tags), ("Kept", ["Keep"])
        )
        perform_action(workflow, self.alice, "REJECT", "no")
        self.assertEqual(self.fresh(document).tags, ["Keep"])
        self.assertEqual(self.internal_status(document), "DRAFT")
        # a new version, approved
        approved = self.approve(self.new_version_for(document))
        self.assertEqual(approved.revision_label, "A")
        fresh = self.fresh(document)
        self.assertEqual((fresh.description, fresh.tags), ("Kept", ["Keep"]))
        self.assertEqual(self.internal_status(document), "APPROVED")

    def new_version_for(self, document):
        from django.core.files.base import ContentFile

        from project_management.services.document_revision import add_document_revision

        return add_document_revision(
            document, ContentFile(b"next", name="next.pdf"), self.creator
        )


class ToCreateMetadataTests(MetadataBase):
    def test_a_fileless_document_supports_metadata_and_stays_to_create(self):
        document = self.create_fileless(description="Planned", tags=["Later"])
        self.assertEqual(self.internal_status(document), "TO_CREATE")
        reference = self.fresh(document).full_document_number
        self.patch_document(document, description="Planned v2", tags=["Later", "Soon"])
        fresh = self.fresh(document)
        self.assertEqual(fresh.tags, ["Later", "Soon"])
        self.assertEqual(self.internal_status(fresh), "TO_CREATE")
        self.assertEqual(self.internal_status(fresh, annotated=True), "TO_CREATE")
        self.assertFalse(fresh.file)
        self.assertEqual(DocumentRevision.objects.filter(document=fresh).count(), 0)
        self.assertEqual(fresh.full_document_number, reference)

    def test_the_first_main_file_keeps_the_metadata(self):
        document = self.create_fileless(description="Planned", tags=["Later"])
        self.upload_first_file(document)
        fresh = self.fresh(document)
        self.assertEqual((fresh.description, fresh.tags), ("Planned", ["Later"]))
        self.assertEqual(self.internal_status(fresh), "DRAFT")


class MultipartClearingTests(MetadataBase):
    def test_multipart_cannot_send_an_empty_list_so_a_single_blank_value_clears_the_tags(self):
        document = self.create_fileless(tags=["Keep"])
        query = QueryDict(mutable=True)
        query.setlist("tags", [""])  # what a form sends for "no tags"
        serializer = DocumentSerializer(
            self.fresh(document), data=query, partial=True, context={"user": self.creator}
        )
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()
        self.assertEqual(self.fresh(document).tags, [])
