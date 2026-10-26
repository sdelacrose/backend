from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework import serializers

from project_management.serializers.document import DocumentSerializer
from project_management.serializers.file_name import validate_display_file_name
from project_management.serializers.object_assessment import ObjectAssessmentSerializer


class DisplayFileNameValidationTests(SimpleTestCase):
    def test_adds_the_existing_extension_to_a_base_name(self):
        self.assertEqual(
            validate_display_file_name(
                "Updated specification",
                current_file_name="Original specification.PDF",
            ),
            "Updated specification.PDF",
        )

    def test_keeps_the_existing_extension_when_it_is_supplied(self):
        self.assertEqual(
            validate_display_file_name(
                "Updated specification.pdf",
                current_file_name="Original specification.pdf",
            ),
            "Updated specification.pdf",
        )

    def test_rejects_an_extension_change(self):
        with self.assertRaisesMessage(
            serializers.ValidationError,
            "The file extension cannot be changed.",
        ):
            validate_display_file_name(
                "Updated specification.exe",
                current_file_name="Original specification.pdf",
            )

    def test_rejects_paths_and_control_characters(self):
        invalid_names = ["../secret.pdf", "folder\\secret.pdf", "secret\x00.pdf"]

        for file_name in invalid_names:
            with self.subTest(file_name=file_name):
                with self.assertRaises(serializers.ValidationError):
                    validate_display_file_name(
                        file_name,
                        current_file_name="Original specification.pdf",
                    )

    def test_rejects_an_empty_base_name(self):
        with self.assertRaises(serializers.ValidationError):
            validate_display_file_name(
                ".pdf",
                current_file_name="Original specification.pdf",
            )

    def test_rejects_null_for_an_existing_file(self):
        with self.assertRaises(serializers.ValidationError):
            validate_display_file_name(
                None,
                current_file_name="Original specification.pdf",
            )

    def test_document_serializer_uses_the_stored_file_extension(self):
        instance = SimpleNamespace(
            file=SimpleNamespace(name="documents/original.docx"),
            file_name="Original.docx",
        )
        serializer = DocumentSerializer(instance=instance, partial=True)

        self.assertEqual(
            serializer.validate_file_name("Customer specification"),
            "Customer specification.docx",
        )

    def test_object_assessment_serializer_uses_the_replacement_file_extension(self):
        instance = SimpleNamespace(
            file=SimpleNamespace(name="assessments/original.jpg"),
            file_name="Original.jpg",
        )
        serializer = ObjectAssessmentSerializer(
            instance=instance,
            data={
                "file": SimpleUploadedFile("replacement.png", b"image"),
                "file_name": "Updated image",
            },
            partial=True,
        )

        self.assertEqual(
            serializer.validate_file_name("Updated image"),
            "Updated image.png",
        )


class DocumentPartialUpdateTests(SimpleTestCase):
    def test_filename_only_update_keeps_existing_numbering_fields(self):
        folder = SimpleNamespace(id=None)
        instance = SimpleNamespace(
            folder=folder,
            document_type="Specification",
            category="Technical",
            discipline="Mechanical",
            sequence_number=7,
            revision=2,
            author=SimpleNamespace(id=3),
        )
        serializer = DocumentSerializer(instance=instance, partial=True)
        serializer.get_config = Mock(return_value=None)
        with patch(
            "rest_framework.serializers.ModelSerializer.update",
            return_value=instance,
        ) as model_update:
            serializer.update(instance, {"file_name": "Renamed.pdf"})

        validated_data = model_update.call_args.args[1]
        self.assertEqual(validated_data["file_name"], "Renamed.pdf")
        # The reference is generated once at creation and is permanent: an update never recalculates it.
        self.assertNotIn("full_document_number", validated_data)
        self.assertEqual(validated_data["sequence_number"], instance.sequence_number)
        self.assertEqual(validated_data["revision"], instance.revision)


class DocumentAuthorPartialUpdateTests(SimpleTestCase):
    def test_author_only_update_uses_existing_company_folder(self):
        company = SimpleNamespace(id=11)
        folder = SimpleNamespace(
            project=None,
            company_profile=company,
            company_profile_id=company.id,
            freelancer=None,
        )
        author = SimpleNamespace(is_freelancer=False, company=company)
        instance = SimpleNamespace(folder=folder)
        serializer = DocumentSerializer(
            instance=instance,
            data={"author": 42},
            partial=True,
            context={"user": SimpleNamespace(is_freelancer=False)},
        )

        with patch(
            "project_management.serializers.document.Folder.objects.filter",
        ) as folder_filter:
            validated_author = serializer.validate_author(author)

        self.assertIs(validated_author, author)
        folder_filter.assert_not_called()

    def test_author_only_update_uses_existing_project_folder(self):
        project = SimpleNamespace(id=22)
        folder = SimpleNamespace(
            project=project,
            company_profile=None,
            freelancer=None,
        )
        author = SimpleNamespace()
        instance = SimpleNamespace(folder=folder)
        serializer = DocumentSerializer(
            instance=instance,
            data={"author": 42},
            partial=True,
        )

        with patch(
            "project_management.serializers.document.Folder.objects.filter",
        ) as folder_filter:
            validated_author = serializer.validate_author(author)

        self.assertIs(validated_author, author)
        folder_filter.assert_not_called()

    def test_object_assessment_author_only_update_uses_existing_project(self):
        project = SimpleNamespace(id=33)
        author = SimpleNamespace()
        instance = SimpleNamespace(project=project)
        serializer = ObjectAssessmentSerializer(
            instance=instance,
            data={"author": 42},
            partial=True,
        )

        with patch(
            "project_management.serializers.object_assessment.Project.objects.filter",
        ) as project_filter:
            validated_author = serializer.validate_author(author)

        self.assertIs(validated_author, author)
        project_filter.assert_not_called()


class DocumentStatusValidationTests(SimpleTestCase):
    def test_submitted_requires_at_least_one_review_status(self):
        serializer = DocumentSerializer(
            instance=SimpleNamespace(
                status="Draft",
                internal_review_status=None,
                external_review_status=None,
            ),
            partial=True,
        )

        with self.assertRaisesMessage(
            serializers.ValidationError,
            "As the document is submitted, please add a review status.",
        ):
            serializer.validate({"status": "Submitted"})

    def test_submitted_allows_partial_update_when_existing_review_status_remains(self):
        serializer = DocumentSerializer(
            instance=SimpleNamespace(
                status="Draft",
                internal_review_status="Approved",
                external_review_status=None,
            ),
            partial=True,
        )

        validated = serializer.validate({"status": "Submitted"})

        self.assertEqual(validated["status"], "Submitted")

    def test_submitted_allows_new_external_review_status(self):
        serializer = DocumentSerializer(partial=True)

        validated = serializer.validate(
            {
                "status": "Submitted",
                "external_review_status": "Reviewed",
            }
        )

        self.assertEqual(validated["status"], "Submitted")
        self.assertEqual(validated["external_review_status"], "Reviewed")
