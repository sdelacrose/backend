from unittest.mock import patch
import hashlib
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.models import Document, DocumentRevision
from project_management.services.document_revision import create_document_revision
from project_management.test_document_revision import MEDIA, NoNotificationsMixin, make_document
from project_management.views.document_revision import DocumentRevisionViewSet


@override_settings(MEDIA_ROOT=MEDIA)
class DuplicateRevisionTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.user, self.document = make_document()

    def upload(self, content=b"original", kind="minor", name="plan.txt", external_revision=None, reason="Correction"):
        result = create_document_revision(self.document, ContentFile(content, name=name), self.user,
                                         kind, reason, external_revision=external_revision)
        self.document.refresh_from_db()
        return result

    def assert_duplicate(self, **kwargs):
        before = self.document.revisions.count()
        history = self.document.history.count()
        file_name = self.document.file.name
        with self.assertRaises(ValidationError) as caught:
            self.upload(**kwargs)
        self.assertIn("unchanged_document_revision", caught.exception.message_dict["code"])
        self.assertEqual(self.document.revisions.count(), before)
        self.assertEqual(self.document.history.count(), history)
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, file_name)

    def test_identical_legacy_file_does_not_even_materialize_a_revision(self):
        self.assert_duplicate()
        self.assertFalse(self.document.revisions.exists())

    def test_filename_only_is_not_a_document_change(self):
        self.assert_duplicate(name="another-name.txt")

    def test_reason_and_major_choice_do_not_bypass_detection(self):
        self.assert_duplicate(kind="major", reason="A completely different reason")

    def test_same_size_different_bytes_are_accepted(self):
        revision = self.upload(b"modified")  # same byte count as 'original'
        self.assertEqual(revision.file_sha256, hashlib.sha256(b"modified").hexdigest())
        revision.file.open("rb")
        try:
            self.assertEqual(revision.file.read(), b"modified")
        finally:
            revision.file.close()

    def test_real_description_change_is_traced_and_permits_identical_file_once(self):
        self.document.description = "Pump calculation corrected"
        self.document.save(update_fields=["description"])
        revision = self.upload()
        self.assertEqual(revision.file_sha256, hashlib.sha256(b"original").hexdigest())
        self.assertEqual(revision.comment, "Correction")
        self.assert_duplicate()

    def test_logical_title_change_is_document_metadata(self):
        self.document.file_name = "New documentary title.txt"
        self.document.save(update_fields=["file_name"])
        self.upload()
        self.assertEqual(self.document.file_name, "New documentary title.txt")

    def test_new_client_reference_is_significant_and_traced(self):
        self.document.client_reference = "CLIENT-100"
        self.document.save(update_fields=["client_reference"])
        self.upload()
        self.assertEqual(self.document.client_reference, "CLIENT-100")

    def test_tag_order_and_duplicates_are_not_meaningful_changes(self):
        self.document.tags = ["alpha", "beta"]
        self.document.save(update_fields=["tags"])
        self.upload(b"baseline")
        self.document.tags = ["beta", "alpha", "alpha"]
        self.document.save(update_fields=["tags"])
        self.assert_duplicate(content=b"baseline")

    def test_actual_tag_change_permits_identical_file(self):
        self.document.tags = ["new tag"]
        self.document.save(update_fields=["tags"])
        self.upload()

    def test_metadata_change_then_revert_is_not_a_net_change(self):
        self.document.description = "Temporary"
        self.document.save(update_fields=["description"])
        self.document.description = ""
        self.document.save(update_fields=["description"])
        self.assert_duplicate()

    def test_automatic_timestamp_touch_is_ignored(self):
        self.document.save(update_fields=["updated_at"])
        self.assert_duplicate()

    def test_missing_history_cannot_prove_a_metadata_change(self):
        self.document.history.all().delete()  # only this isolated test's fixture
        self.assert_duplicate()

    def test_unchanged_stored_version_is_rejected_too(self):
        self.upload(b"baseline")
        self.assert_duplicate(content=b"baseline")

    def test_unreadable_current_file_fails_without_creating_versions(self):
        with patch.object(self.document.file.storage, "open", side_effect=OSError("unavailable")):
            with self.assertRaises(ValidationError) as caught:
                self.upload(b"different")
        self.assertIn("cannot be read", str(caught.exception))
        self.assertFalse(self.document.revisions.exists())

    def test_first_main_file_still_needs_only_a_file(self):
        self.document.file = ""
        self.document.save(update_fields=["file"])
        revision = create_document_revision(self.document, ContentFile(b"first", name="first.txt"),
                                            self.user, None, None)
        self.assertEqual(revision.version_number, 1)

    def test_external_revision_change_is_significant_for_supplier(self):
        self.document.document_origin = "SUPPLIER"
        self.document.save(update_fields=["document_origin"])
        self.upload(b"supplier", external_revision="A")
        self.upload(b"supplier", external_revision="B")
        self.assert_duplicate(content=b"supplier", external_revision="B")

    def test_internal_document_cannot_bypass_with_external_revision(self):
        self.assert_duplicate(external_revision="Invented")

    def test_api_reports_duplicate_and_preserves_current_version(self):
        request = APIRequestFactory().post("/x/", {"file": SimpleUploadedFile("renamed.txt", b"original"),
            "revision_type": "minor", "comment": "Reason"}, format="multipart")
        force_authenticate(request, user=self.user)
        view = DocumentRevisionViewSet.as_view({"post": "create"}, permission_classes=[])
        with patch("project_management.views.document_revision.get_register_document_or_404", return_value=self.document), \
             patch("project_management.views.document_revision.assert_can_update_document"):
            response = view(request, document_id=self.document.pk)
        self.assertEqual(response.status_code, 400)
        self.assertIn("unchanged_document_revision", response.data["code"])
        self.assertFalse(self.document.revisions.exists())
