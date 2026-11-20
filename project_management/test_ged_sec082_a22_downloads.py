"""GED-SEC.08.2-A2.2: the authenticated downloads of the main file, of a revision and of an attachment of a Document.

Synthetic files in a temporary MEDIA_ROOT and the temporary test database. Requests go through the real view classes
with force_authenticate (the authentication middleware itself is NOT traversed here; the local browser check is)."""
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentAttachment
from project_management.test_ged_wf44b_history import HistoryBase
from project_management.test_transmittal_client_return import VIEW
from project_management.views.document_attachment import DocumentAttachmentViewSet
from project_management.views.document_revision import DocumentRevisionViewSet

MAIN = DocumentRevisionViewSet.as_view({"get": "download_current"})
REVISION = DocumentRevisionViewSet.as_view({"get": "download"})
ATTACHMENT = DocumentAttachmentViewSet.as_view({"get": "download"})


class DownloadBase(HistoryBase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.addCleanup(shutil.rmtree, self._media, True)
        self.addCleanup(self._override.disable)
        super().setUp()
        self.old_rev = self.rev1
        # synthetic bytes under the stored names of the main document and of its revision
        self.main_name = self._put("documents/sec082/main.txt", b"synthetic main file")
        Document.objects.filter(pk=self.doc1.pk).update(file=self.main_name, file_name="main.txt")
        # a stored revision is immutable: only its bytes are replaced, under its own stored name
        self.rev_name = self.old_rev.file.name
        self._put(self.rev_name, b"synthetic revision one", replace=True)
        self.doc1.refresh_from_db()
        attachment_name = self._put("documents/sec082/annex.pdf", b"%PDF-1.4 synthetic")
        self.attachment = DocumentAttachment.objects.create(
            document=self.doc1, file=attachment_name, file_name="annex.pdf", file_size=18, uploaded_by=self.manager)

    def _put(self, name, content, replace=False):
        from django.core.files.storage import default_storage
        if replace:
            default_storage.delete(name)
        return default_storage.save(name, ContentFile(content))

    def get(self, view, user, method="get", **kwargs):
        request = getattr(APIRequestFactory(), method)("/x/")
        if user is not None:
            force_authenticate(request, user=user)
        response = view(request, **kwargs)
        if hasattr(response, "render"):
            response.render()
        return response

    def body(self, response):
        return b"".join(response.streaming_content) if getattr(response, "streaming", False) else response.content


class MainDocumentFileTests(DownloadBase):
    def test_an_authorised_user_gets_the_bytes_with_name_type_and_private_headers(self):
        response = self.get(MAIN, self.manager, document_id=self.doc1.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), b"synthetic main file")
        self.assertIn('filename="main.txt"', response["Content-Disposition"])
        self.assertTrue(response["Content-Disposition"].startswith("attachment"))
        self.assertEqual(response["Content-Type"].split(";")[0], "text/plain")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Vary"], "Authorization")

    def test_a_document_reader_gets_it_too_and_head_works_for_the_preview_probe(self):
        self.assertEqual(self.get(MAIN, self.summary_reader, document_id=self.doc1.pk).status_code, 200)
        head = self.get(MAIN, self.summary_reader, method="head", document_id=self.doc1.pk)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head["Cache-Control"], "private, no-store")

    def test_nothing_reveals_the_physical_path(self):
        response = self.get(MAIN, self.manager, document_id=self.doc1.pk)
        self.body(response)
        blob = " ".join(f"{k}:{v}" for k, v in response.items())
        self.assertNotIn("sec082", blob)
        self.assertNotIn(self._media, blob)

    def test_no_document_right_other_project_unknown_and_anonymous_are_refused(self):
        nobody = self.grant("sec082.nobody@links-local.invalid", self.project, ["documents.update"])
        stranger = self.grant("sec082.stranger@links-local.invalid", self.other_project, [VIEW])
        for user in (nobody, stranger):
            self.assertEqual(self.get(MAIN, user, document_id=self.doc1.pk).status_code, 404)
        self.assertEqual(self.get(MAIN, self.manager, document_id=99999999).status_code, 404)
        self.assertIn(self.get(MAIN, None, document_id=self.doc1.pk).status_code, (401, 403))

    def test_a_withdrawn_document_stays_downloadable(self):
        Document.objects.filter(pk=self.doc1.pk).update(withdrawn_at=timezone.now())
        self.assertEqual(self.get(MAIN, self.summary_reader, document_id=self.doc1.pk).status_code, 200)

    def test_a_missing_stored_file_and_a_document_without_file_are_a_404_not_a_server_error(self):
        from django.core.files.storage import default_storage
        default_storage.delete(self.main_name)
        self.assertEqual(self.get(MAIN, self.manager, document_id=self.doc1.pk).status_code, 404)
        Document.objects.filter(pk=self.doc1.pk).update(file="")
        self.assertEqual(self.get(MAIN, self.manager, document_id=self.doc1.pk).status_code, 404)


class RevisionAndAttachmentFileTests(DownloadBase):
    def test_a_revision_gives_exactly_that_revision_with_its_name(self):
        response = self.get(REVISION, self.manager, document_id=self.doc1.pk, revision_id=self.old_rev.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), b"synthetic revision one")
        self.assertIn(f'filename="{self.old_rev.file_name}"', response["Content-Disposition"])
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.get(REVISION, self.manager, method="head", document_id=self.doc1.pk, revision_id=self.old_rev.pk).status_code, 200)

    def test_a_revision_of_another_document_is_a_wrong_attachment_to_the_document(self):
        other = self.rev2  # belongs to another Document of the same project
        self.assertNotEqual(other.document_id, self.doc1.pk)
        self.assertEqual(self.get(REVISION, self.manager, document_id=self.doc1.pk, revision_id=other.pk).status_code, 404)
        self.assertEqual(self.get(REVISION, self.manager, document_id=self.doc1.pk, revision_id=99999999).status_code, 404)

    def test_revision_refusals_and_missing_file(self):
        nobody = self.grant("sec082.nobody2@links-local.invalid", self.project, ["documents.update"])
        self.assertEqual(self.get(REVISION, nobody, document_id=self.doc1.pk, revision_id=self.old_rev.pk).status_code, 404)
        self.assertIn(self.get(REVISION, None, document_id=self.doc1.pk, revision_id=self.old_rev.pk).status_code, (401, 403))
        from django.core.files.storage import default_storage
        default_storage.delete(self.rev_name)
        self.assertEqual(self.get(REVISION, self.manager, document_id=self.doc1.pk, revision_id=self.old_rev.pk).status_code, 404)

    def test_an_attachment_downloads_with_its_name_type_and_headers(self):
        response = self.get(ATTACHMENT, self.summary_reader, document_id=self.doc1.pk, attachment_id=self.attachment.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.body(response), b"%PDF-1.4 synthetic")
        self.assertIn('filename="annex.pdf"', response["Content-Disposition"])
        self.assertEqual(response["Content-Type"].split(";")[0], "application/pdf")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_attachment_wrong_attachment_other_project_anonymous_and_missing_file(self):
        other_attachment = DocumentAttachment.objects.create(
            document=self.rev2.document, file=self._put("documents/sec082/other.pdf", b"x"), file_name="other.pdf", file_size=1, uploaded_by=self.manager)
        self.assertEqual(self.get(ATTACHMENT, self.manager, document_id=self.doc1.pk, attachment_id=other_attachment.pk).status_code, 404)
        stranger = self.grant("sec082.stranger2@links-local.invalid", self.other_project, [VIEW])
        self.assertEqual(self.get(ATTACHMENT, stranger, document_id=self.doc1.pk, attachment_id=self.attachment.pk).status_code, 404)
        self.assertIn(self.get(ATTACHMENT, None, document_id=self.doc1.pk, attachment_id=self.attachment.pk).status_code, (401, 403))
        from django.core.files.storage import default_storage
        default_storage.delete(self.attachment.file.name)
        self.assertEqual(self.get(ATTACHMENT, self.manager, document_id=self.doc1.pk, attachment_id=self.attachment.pk).status_code, 404)
