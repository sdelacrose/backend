import io
import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.models import ObjectAssessment, Project
from project_management.serializers.object_assessment import ObjectAssessmentSerializer
from project_management.signals import (
    create_default_folders,
    notify_on_object_assessment_delete,
    notify_on_object_assessment_save,
    notify_on_project_save,
)
from project_management.views.document import FolderViewSet


class ObjectAssessmentRichNotesBackfillCommandTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        pre_delete.disconnect(notify_on_object_assessment_delete, sender=ObjectAssessment)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            notify_on_object_assessment_save,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_object_assessment_delete,
            sender=ObjectAssessment,
        )

        self.user = User.objects.create_user(
            email="notes-owner@example.com",
            password="password",
            first_name="Notes",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Notes Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Notes Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

    def test_backfill_command_converts_legacy_txt_note(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Legacy Note",
            file=SimpleUploadedFile(
                "legacy-notes.txt",
                b"Line 1\n\nLine 2",
                content_type="text/plain",
            ),
        )

        output = io.StringIO()
        call_command("backfill_object_assessment_rich_notes", stdout=output)

        assessment.refresh_from_db()
        self.assertEqual(
            assessment.notes_content_json,
            {
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Line 1"}],
                    },
                    {"type": "paragraph"},
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Line 2"}],
                    },
                ],
            },
        )
        self.assertEqual(
            assessment.notes_content_html,
            "<p>Line 1</p><p></p><p>Line 2</p>",
        )
        self.assertIn("Updated: 1", output.getvalue())

    def test_backfill_command_respects_dry_run(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Dry Run Note",
            file=SimpleUploadedFile(
                "dry-run.txt",
                b"Preview only",
                content_type="text/plain",
            ),
        )

        output = io.StringIO()
        call_command(
            "backfill_object_assessment_rich_notes",
            "--dry-run",
            stdout=output,
        )

        assessment.refresh_from_db()
        self.assertIsNone(assessment.notes_content_json)
        self.assertIsNone(assessment.notes_content_html)
        self.assertIn("Running in dry-run mode", output.getvalue())
        self.assertIn("Updated: 1", output.getvalue())

    def test_backfill_command_skips_existing_rich_note(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Already Migrated",
            file=SimpleUploadedFile(
                "already.txt",
                b"Original legacy text",
                content_type="text/plain",
            ),
            notes_content_json={"type": "doc", "content": [{"type": "paragraph"}]},
            notes_content_html="<p>Already migrated</p>",
        )

        output = io.StringIO()
        call_command("backfill_object_assessment_rich_notes", stdout=output)

        assessment.refresh_from_db()
        self.assertEqual(assessment.notes_content_html, "<p>Already migrated</p>")
        self.assertIn("Skipped: 1", output.getvalue())

    def test_backfill_command_skips_missing_file_safely(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Missing File",
            file=SimpleUploadedFile(
                "missing.txt",
                b"Will be deleted",
                content_type="text/plain",
            ),
        )
        storage = assessment.file.storage
        file_name = assessment.file.name
        storage.delete(file_name)

        output = io.StringIO()
        call_command("backfill_object_assessment_rich_notes", stdout=output)

        assessment.refresh_from_db()
        self.assertIsNone(assessment.notes_content_json)
        self.assertIsNone(assessment.notes_content_html)
        self.assertIn("Errors: 1", output.getvalue())


class ObjectAssessmentRichNotesSerializerTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        pre_delete.disconnect(notify_on_object_assessment_delete, sender=ObjectAssessment)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            notify_on_object_assessment_save,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_object_assessment_delete,
            sender=ObjectAssessment,
        )

        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="serializer-owner@example.com",
            password="password",
            first_name="Serializer",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Serializer Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Serializer Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

    def _build_request(self, method="post"):
        request = getattr(self.factory, method)("/api/project_management/object-assessment/")
        request.user = self.user
        return request

    def test_serializer_creates_notes_record_with_rich_content_without_file(self):
        rich_json = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Rich note"}],
                }
            ],
        }
        serializer = ObjectAssessmentSerializer(
            data={
                "project": self.project.id,
                "record_type": "Notes",
                "record_title": "Rich Note",
                "notes_content_json": json.dumps(rich_json),
                "notes_content_html": "<p>Rich note</p>",
            },
            context={
                "request": self._build_request("post"),
                "user": self.user,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()

        self.assertEqual(instance.record_type, "Notes")
        self.assertEqual(instance.notes_content_json, rich_json)
        self.assertEqual(instance.notes_content_html, "<p>Rich note</p>")
        self.assertFalse(bool(instance.file))
        self.assertEqual(instance.file_name, "rich_note")
        self.assertEqual(instance.created_by, self.user)

    def test_folder_list_handles_fileless_rich_note(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Fileless Note",
            notes_content_json={
                "type": "doc",
                "content": [{"type": "paragraph"}],
            },
            notes_content_html="<p></p>",
            created_by=self.user,
        )
        root_folder_name = f"{self.project.id}_root_folder"
        request = self.factory.get(
            "/api/project_management/folder",
            {"name": root_folder_name, "include_history": "true"},
        )
        force_authenticate(request, user=self.user)
        base_response = Response(
            {
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"name": root_folder_name, "subfolders": []}],
            }
        )

        view = FolderViewSet.as_view({"get": "list"})
        with patch.object(FolderViewSet, "get_permissions", return_value=[]), patch(
            "rest_framework.mixins.ListModelMixin.list",
            return_value=base_response,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        object_assessment_folder = response.data["results"][0]["subfolders"][0]
        note = object_assessment_folder["subfolders"][0]["documents"][0]
        self.assertEqual(note["id"], assessment.id)
        self.assertIsNone(note["file"])

    def test_serializer_updates_existing_notes_record_rich_content(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Updatable Note",
            notes_content_json={
                "type": "doc",
                "content": [{"type": "paragraph"}],
            },
            notes_content_html="<p>Initial</p>",
            created_by=self.user,
        )
        updated_json = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Updated note"}],
                }
            ],
        }
        serializer = ObjectAssessmentSerializer(
            assessment,
            data={
                "notes_content_json": updated_json,
                "notes_content_html": "<p>Updated note</p>",
            },
            partial=True,
            context={
                "request": self._build_request("patch"),
                "user": self.user,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()

        self.assertEqual(updated.notes_content_json, updated_json)
        self.assertEqual(updated.notes_content_html, "<p>Updated note</p>")
        self.assertEqual(updated.file_name, "updatable_note")

    def test_serializer_derives_file_name_for_existing_fileless_note(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Note 4",
            notes_content_json={
                "type": "doc",
                "content": [{"type": "paragraph"}],
            },
            notes_content_html="<p></p>",
            created_by=self.user,
        )

        serialized = ObjectAssessmentSerializer(assessment).data

        self.assertEqual(serialized["file_name"], "note_4")

    def test_serializer_rejects_empty_notes_without_rich_content_or_legacy_file(self):
        serializer = ObjectAssessmentSerializer(
            data={
                "project": self.project.id,
                "record_type": "Notes",
                "record_title": "Empty Note",
            },
            context={
                "request": self._build_request("post"),
                "user": self.user,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("notes_content_json", serializer.errors)

    def test_serializer_keeps_non_notes_file_requirement(self):
        serializer = ObjectAssessmentSerializer(
            data={
                "project": self.project.id,
                "record_type": "Documents",
                "record_title": "Document Without File",
            },
            context={
                "request": self._build_request("post"),
                "user": self.user,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertEqual(serializer.errors["file"], ["This field is required."])

    def test_serializer_allows_non_notes_with_file_without_rich_content(self):
        serializer = ObjectAssessmentSerializer(
            data={
                "project": self.project.id,
                "record_type": "Documents",
                "record_title": "File Record",
                "file": SimpleUploadedFile(
                    "document.txt",
                    b"plain document",
                    content_type="text/plain",
                ),
            },
            context={
                "request": self._build_request("post"),
                "user": self.user,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        instance = serializer.save()

        self.assertEqual(instance.file_name, "document.txt")
        self.assertIsNone(instance.notes_content_json)
        self.assertIsNone(instance.notes_content_html)

    def test_serializer_represents_legacy_note_with_empty_rich_fields(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Legacy Note",
            file=SimpleUploadedFile(
                "legacy.txt",
                b"legacy body",
                content_type="text/plain",
            ),
            created_by=self.user,
        )

        serialized = ObjectAssessmentSerializer(assessment).data

        self.assertIsNone(serialized["notes_content_json"])
        self.assertIsNone(serialized["notes_content_html"])
