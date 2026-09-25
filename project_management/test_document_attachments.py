from unittest.mock import patch

from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document,
    DocumentAttachment,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
)
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action
from project_management.test_document_workflow import THREE, TWO, WorkflowTestBase
from project_management.views.document_attachment import DocumentAttachmentViewSet

ACCESS = "project_management.views.document_attachment.get_register_document_or_404"
CAN_UPDATE = "project_management.views.document_attachment.assert_can_update_document"


class AttachmentTestBase(WorkflowTestBase):
    """Supporting files: independent from the controlled main file and its workflow."""

    def api(self, action, document=None, data=None, attachment_id=None, can_update=True,
            can_view=True):
        document = document or self.document
        methods = {"list": "get", "create": "post", "download": "get", "destroy": "delete"}
        view = DocumentAttachmentViewSet.as_view(
            {methods[action]: action}, permission_classes=[]
        )
        factory = APIRequestFactory()
        request = getattr(factory, methods[action])(
            "/x/", data, format="multipart"
        ) if data else getattr(factory, methods[action])("/x/")
        force_authenticate(request, user=self.creator)
        kwargs = {"document_id": document.id}
        if attachment_id is not None:
            kwargs["attachment_id"] = attachment_id
        access = {"return_value": document} if can_view else {"side_effect": Http404}
        update = {} if can_update else {"side_effect": PermissionDenied}
        with patch(ACCESS, **access), patch(CAN_UPDATE, **update):
            return view(request, **kwargs)

    def upload(self, name="annex.txt", content=b"annex", **kwargs):
        response = self.api(
            "create", data={"file": SimpleUploadedFile(name, content)}, **kwargs
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        return response.data

    def snapshot(self):
        document = Document.objects.get(pk=self.document.pk)
        return {
            "reference": document.full_document_number,
            "file": document.file.name,
            "status": document.status,
            "versions": list(
                DocumentRevision.objects.filter(document=document).values_list(
                    "id", "version_number", "revision_label", "file"
                )
            ),
            "workflows": list(
                DocumentWorkflow.objects.order_by("id").values_list("id", "status")
            ),
            "events": DocumentWorkflowEvent.objects.count(),
        }


class AttachmentApiTests(AttachmentTestBase):
    def test_upload_returns_the_attachment(self):
        data = self.upload("calc.txt", b"12345")
        self.assertEqual(data["file_name"], "calc.txt")
        self.assertEqual(data["file_size"], 5)
        self.assertEqual(data["uploaded_by"]["id"], self.creator.id)
        self.assertIn("created_at", data)
        self.assertTrue(data["download_url"].endswith("/download"))
        self.assertEqual(DocumentAttachment.objects.filter(document=self.document).count(), 1)

    def test_list_returns_the_attachments_of_the_document(self):
        self.upload("a.txt")
        response = self.api("list")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["file_name"], "a.txt")

    def test_download_returns_the_stored_content(self):
        data = self.upload("a.txt", b"payload")
        response = self.api("download", attachment_id=data["id"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"payload")
        self.assertIn("a.txt", response["Content-Disposition"])

    def test_delete_removes_the_row_and_the_stored_file(self):
        data = self.upload("a.txt")
        attachment = DocumentAttachment.objects.get(pk=data["id"])
        storage, name = attachment.file.storage, attachment.file.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api("destroy", attachment_id=data["id"])
        self.assertEqual(response.status_code, 204)
        self.assertFalse(DocumentAttachment.objects.filter(pk=data["id"]).exists())
        self.assertFalse(storage.exists(name))

    def test_user_who_cannot_update_cannot_upload_or_delete(self):
        data = self.upload("a.txt")
        denied = self.api(
            "create", data={"file": SimpleUploadedFile("b.txt", b"b")}, can_update=False
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(
            self.api("destroy", attachment_id=data["id"], can_update=False).status_code, 403
        )
        self.assertEqual(DocumentAttachment.objects.count(), 1)

    def test_a_viewer_who_cannot_update_can_list_and_download(self):
        data = self.upload("a.txt", b"x")
        listing = self.api("list", can_update=False)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 1)
        download = self.api("download", attachment_id=data["id"], can_update=False)
        self.assertEqual(download.status_code, 200)

    def test_a_user_without_access_to_the_document_gets_a_404(self):
        data = self.upload("a.txt")
        for action, kwargs in (("list", {}), ("download", {"attachment_id": data["id"]})):
            self.assertEqual(self.api(action, can_view=False, **kwargs).status_code, 404)

    def test_an_attachment_belongs_only_to_the_requested_document(self):
        data = self.upload("a.txt")
        other = Document(folder=self.document.folder, file_name="o.txt", created_by=self.creator)
        other.file.save("o.txt", ContentFile(b"other"), save=False)
        other.save()
        self.assertEqual(self.api("list", document=other).data["count"], 0)
        self.assertEqual(
            self.api("download", document=other, attachment_id=data["id"]).status_code, 404
        )
        self.assertEqual(
            self.api("destroy", document=other, attachment_id=data["id"]).status_code, 404
        )
        self.assertTrue(DocumentAttachment.objects.filter(pk=data["id"]).exists())

    def test_multiple_attachments_and_duplicate_names_never_overwrite(self):
        first = self.upload("same.txt", b"one")
        second = self.upload("same.txt", b"two")
        third = self.upload("other.txt", b"three")
        self.assertEqual(self.api("list").data["count"], 3)
        self.assertEqual(len({first["id"], second["id"], third["id"]}), 3)
        stored = {
            a.file_name + str(a.pk): a.file.read() for a in DocumentAttachment.objects.all()
        }
        self.assertEqual(sorted(stored.values()), [b"one", b"three", b"two"])
        names = [a.file.name for a in DocumentAttachment.objects.all()]
        self.assertEqual(len(set(names)), 3)  # distinct stored files
        self.assertEqual(first["file_name"], second["file_name"])  # display name kept

    def test_upload_follows_the_document_upload_rules(self):
        # Same policy as document files: a file is required, only its base name is kept,
        # and no extra type/size policy is invented for attachments.
        missing = self.api("create", data={"note": "x"})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("file", missing.data)
        data = self.upload("../../etc/passwd.txt", b"x")
        self.assertEqual(data["file_name"], "passwd.txt")
        stored = DocumentAttachment.objects.get(pk=data["id"]).file.name
        self.assertIn(f"attachments/document_{self.document.id}/", stored)
        self.assertNotIn("..", stored)


class AttachmentIsolationTests(AttachmentTestBase):
    """Attachments never touch the controlled document, its versions or its workflow."""

    def test_upload_and_delete_change_nothing_on_the_controlled_document(self):
        before = self.snapshot()
        data = self.upload("a.txt")
        self.upload("b.txt")
        self.assertEqual(self.snapshot(), before)
        self.api("destroy", attachment_id=data["id"])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(Document.objects.get(pk=self.document.pk).file)

    def test_deleting_an_attachment_keeps_the_main_file(self):
        main = Document.objects.get(pk=self.document.pk).file
        content = main.read()
        data = self.upload("a.txt")
        self.api("destroy", attachment_id=data["id"])
        main = Document.objects.get(pk=self.document.pk).file
        self.assertTrue(main.storage.exists(main.name))
        self.assertEqual(main.read(), content)

    def test_attachments_remain_after_a_new_version(self):
        data = self.upload("a.txt")
        latest = DocumentRevision.objects.filter(document=self.document).count()
        version = add_document_revision(
            self.document, SimpleUploadedFile("plan.txt", b"v3"), self.creator
        )
        self.assertEqual(version.version_number, latest + 1)
        self.assertEqual(self.api("list").data["count"], 1)
        self.assertEqual(self.api("list").data["results"][0]["id"], data["id"])
        self.assertEqual(
            DocumentAttachment.objects.filter(document=self.document).count(), 1
        )

    def test_attachments_remain_and_stay_untouched_after_workflow_approval(self):
        self.upload("a.txt")
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        self.revision2.refresh_from_db()
        self.assertEqual(self.revision2.revision_label, "A")
        attachment = DocumentAttachment.objects.get(document=self.document)
        self.assertEqual(attachment.file_name, "a.txt")
        self.assertEqual(self.api("list").data["count"], 1)

    def test_an_active_workflow_does_not_block_attachment_upload_or_delete(self):
        wf = self.start(THREE, revision=self.revision2)
        before = self.snapshot()
        first = self.upload("during.txt")
        self.upload("during2.txt")
        self.assertEqual(self.api("destroy", attachment_id=first["id"]).status_code, 204)
        wf.refresh_from_db()
        self.assertEqual(wf.status, "DRAFT")
        self.assertEqual(wf.events.count(), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(DocumentAttachment.objects.filter(document=self.document).count(), 1)
