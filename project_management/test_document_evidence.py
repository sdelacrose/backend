from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
    Folder,
)
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import (
    document_has_evidence,
    perform_action,
)
from project_management.test_document_revision import MEDIA, make_document
from project_management.test_document_workflow import TWO, THREE, WorkflowTestBase
from project_management.views.document import DocumentViewSet

Status = DocumentWorkflow.Status


@override_settings(MEDIA_ROOT=MEDIA)
class EvidenceProtectionTests(WorkflowTestBase):
    """Approved / workflow documents are documentary evidence: never hard-deleted."""

    def approve(self, revision=None):
        wf = self.start(TWO, revision=revision or self.revision2)
        perform_action(wf, self.alice, "SIGN", "signed")
        perform_action(wf, self.carol, "APPROVE", "approved")
        wf.refresh_from_db()
        return wf

    def reject(self, revision=None):
        wf = self.start(TWO, revision=revision or self.revision2)
        perform_action(wf, self.alice, "REJECT", "not good")
        wf.refresh_from_db()
        return wf

    def delete_via_api(self, document):
        view = DocumentViewSet.as_view({"delete": "destroy"})
        request = APIRequestFactory().delete("/x/")
        force_authenticate(request, user=self.creator)
        with patch.object(
            DocumentViewSet, "get_permissions", return_value=[]
        ), patch.object(DocumentViewSet, "get_object", return_value=document):
            return view(request, pk=document.pk)

    def assert_evidence_intact(self, document, revisions, workflows, events):
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
        self.assertEqual(DocumentRevision.objects.filter(document=document).count(), revisions)
        self.assertEqual(
            DocumentWorkflow.objects.filter(revision__document=document).count(), workflows
        )
        self.assertEqual(
            DocumentWorkflowEvent.objects.filter(
                workflow__revision__document=document
            ).count(),
            events,
        )

    # ---- Document deletion --------------------------------------------------------------

    def test_a_draft_document_without_workflow_keeps_the_existing_delete_behaviour(self):
        self.assertFalse(document_has_evidence(self.document))
        response = self.delete_via_api(self.document)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())
        self.assertFalse(DocumentRevision.objects.filter(pk=self.revision.pk).exists())

    def test_a_document_with_a_running_workflow_cannot_be_deleted(self):
        self.start(TWO, revision=self.revision2)
        self.assertTrue(document_has_evidence(self.document))
        with self.assertRaises(ValidationError):
            self.document.delete()
        self.assert_evidence_intact(self.document, revisions=3, workflows=1, events=1)

    def test_rejected_workflow_evidence_survives_a_document_delete(self):
        self.reject()
        with self.assertRaises(ValidationError):
            self.document.delete()
        self.assert_evidence_intact(self.document, revisions=3, workflows=1, events=2)
        self.assertEqual(
            DocumentWorkflowEvent.objects.filter(action="REJECT").first().comment,
            "not good",
        )

    def test_an_approved_document_cannot_be_deleted_and_its_evidence_survives(self):
        self.approve()
        self.revision2.refresh_from_db()
        self.assertEqual(self.revision2.revision_label, "A")
        with self.assertRaises(ValidationError):
            self.document.delete()
        self.assert_evidence_intact(self.document, revisions=3, workflows=1, events=3)
        self.revision2.refresh_from_db()
        self.assertEqual(self.revision2.revision_label, "A")

    def test_the_direct_api_delete_is_refused_with_the_evidence_message(self):
        self.approve()
        response = self.delete_via_api(self.document)
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be deleted", response.data["detail"])
        self.assert_evidence_intact(self.document, revisions=3, workflows=1, events=3)

    def test_an_official_revision_label_alone_is_evidence(self):
        # e.g. a legacy or repaired row: the label is enough to protect the document
        DocumentRevision.objects.filter(pk=self.revision.pk).update(revision_label="A")
        self.assertTrue(document_has_evidence(self.document))
        with self.assertRaises(ValidationError):
            self.document.delete()

    def test_a_folder_delete_cannot_cascade_over_evidence(self):
        self.approve()
        folder = self.document.folder
        with self.assertRaises(ValidationError), transaction.atomic():
            folder.delete()
        self.assert_evidence_intact(self.document, revisions=3, workflows=1, events=3)

    def test_other_documents_are_unaffected_by_someone_elses_evidence(self):
        self.approve()
        other = Document(folder=self.document.folder, file_name="other.txt", created_by=self.creator)
        other.file.save("other.txt", ContentFile(b"x"), save=False)
        other.save()
        other.delete()
        self.assertFalse(Document.objects.filter(pk=other.pk).exists())
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    # ---- Versions -------------------------------------------------------------------------

    def test_a_stored_version_cannot_be_deleted_or_rewritten(self):
        self.approve()
        revision = DocumentRevision.objects.get(pk=self.revision2.pk)
        with self.assertRaises(ValidationError):
            revision.delete()
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).delete()
        with self.assertRaises(ValidationError):
            revision.file = ContentFile(b"replaced", name="evil.txt")
            revision.save()
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(file="evil.txt")
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(revision_label="Z")
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(
                version_number=99, revision_label="Z"
            )
        revision.refresh_from_db()
        self.assertEqual(revision.revision_label, "A")
        self.assertEqual(revision.file_name, "plan.txt")

    def test_the_approved_file_survives_a_new_version(self):
        self.approve()
        approved_name = DocumentRevision.objects.get(pk=self.revision2.pk).file.name
        newer = add_document_revision(
            self.document, ContentFile(b"v3", name="plan.txt"), self.creator
        )
        self.assertIsNone(newer.revision_label)
        self.assertEqual(
            DocumentRevision.objects.get(pk=self.revision2.pk).file.name, approved_name
        )
        self.assertEqual(DocumentRevision.objects.get(pk=self.revision2.pk).revision_label, "A")

    # ---- Workflows ------------------------------------------------------------------------

    def test_a_completed_workflow_cannot_be_rewritten_or_deleted(self):
        wf = self.approve()
        wf.status = Status.REJECTED
        with self.assertRaises(ValidationError):
            wf.save()
        with self.assertRaises(ValidationError):
            wf.delete()
        with self.assertRaises(ValidationError):
            DocumentWorkflow.objects.filter(pk=wf.pk).update(status=Status.DRAFT)
        with self.assertRaises(ValidationError):
            DocumentWorkflow.objects.filter(pk=wf.pk).delete()
        wf.refresh_from_db()
        self.assertEqual(wf.status, Status.APPROVED)

    def test_workflow_events_cannot_be_removed_by_a_cascade_or_bulk_operation(self):
        wf = self.approve()
        event = wf.events.first()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            DocumentWorkflowEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            DocumentWorkflowEvent.objects.filter(pk=event.pk).update(comment="x")

    # ---- The legitimate transitions still work ---------------------------------------------

    def test_the_two_step_walk_and_the_label_assignment_still_work(self):
        wf = self.start(TWO, revision=self.revision2)
        self.assertEqual(perform_action(wf, self.alice, "SIGN", "s").status, Status.APPROVE)
        self.assertEqual(perform_action(wf, self.carol, "APPROVE", "a").status, Status.APPROVED)
        self.revision2.refresh_from_db()
        self.assertEqual(self.revision2.revision_label, "A")

    def test_the_three_step_walk_reject_and_actor_change_still_work(self):
        from project_management.services.document_workflow import change_actor

        wf = self.start(THREE, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "s")
        changed = change_actor(wf, self.alice, "approver", self.dave, "swap")
        self.assertEqual(changed.approver, self.dave)
        perform_action(wf, self.bob, "VALIDATE", "v")
        rejected = perform_action(wf, self.dave, "REJECT", "no")
        self.assertEqual(rejected.status, Status.REJECTED)
        self.revision2.refresh_from_db()
        self.assertIsNone(self.revision2.revision_label)

    # ---- Controlled local cleanup (never an application path) --------------------------------

    def test_a_controlled_raw_cleanup_can_still_remove_disposable_evidence(self):
        wf = self.approve()
        ids = (wf.pk, self.revision2.pk)
        # The documented local-test recipe: explicit raw deletes, children first.
        DocumentWorkflowEvent.objects.filter(workflow_id=ids[0])._raw_delete("default")
        DocumentWorkflow.objects.filter(pk=ids[0])._raw_delete("default")
        DocumentRevision.objects.filter(document=self.document)._raw_delete("default")
        self.assertFalse(document_has_evidence(self.document))
        self.document.delete()
        self.assertFalse(Document.objects.filter(pk=self.document.pk).exists())


@override_settings(MEDIA_ROOT=MEDIA)
class EvidenceCascadeTests(WorkflowTestBase):
    """A parent deletion (Folder / Project) is refused cleanly before any write."""

    def approve_document(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "signed")
        perform_action(wf, self.carol, "APPROVE", "approved")
        return wf

    def delete_folder_via_api(self, folder):
        from project_management.views.document import FolderViewSet

        view = FolderViewSet.as_view({"delete": "destroy"})
        request = APIRequestFactory().delete("/x/")
        force_authenticate(request, user=self.creator)
        with patch.object(
            FolderViewSet, "get_permissions", return_value=[]
        ), patch.object(FolderViewSet, "get_object", return_value=folder):
            return view(request, pk=folder.pk)

    def counts(self):
        return (
            Document.objects.count(),
            DocumentRevision.objects.count(),
            DocumentWorkflow.objects.count(),
            DocumentWorkflowEvent.objects.count(),
        )

    def test_a_folder_holding_evidence_is_refused_cleanly_and_nothing_is_touched(self):
        self.approve_document()
        folder = self.document.folder
        before = self.counts()
        response = self.delete_folder_via_api(folder)
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be deleted", response.data["detail"])
        self.assertTrue(Folder.objects.filter(pk=folder.pk).exists())
        # no broken transaction is left behind: queries keep working and nothing was removed
        self.assertEqual(self.counts(), before)
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    def test_evidence_in_a_subfolder_blocks_the_parent_folder_delete(self):
        self.approve_document()
        parent = Folder.objects.create(
            company_profile=self.document.folder.company_profile, name="Parent"
        )
        Folder.objects.filter(pk=self.document.folder.pk).update(parent=parent)
        before = self.counts()
        response = self.delete_folder_via_api(parent)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Folder.objects.filter(pk=parent.pk).exists())
        self.assertEqual(self.counts(), before)

    def test_a_folder_with_only_drafts_keeps_the_existing_delete_behaviour(self):
        folder = Folder.objects.create(
            company_profile=self.document.folder.company_profile, name="Drafts only"
        )
        draft = Document(folder=folder, file_name="draft.txt", created_by=self.creator)
        draft.file.save("draft.txt", ContentFile(b"d"), save=False)
        draft.save()
        add_document_revision(draft, ContentFile(b"v", name="draft.txt"), self.creator)
        response = self.delete_folder_via_api(folder)
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Folder.objects.filter(pk=folder.pk).exists())
        self.assertFalse(Document.objects.filter(pk=draft.pk).exists())
        # the evidence-free deletion did not disturb anything else
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())

    def test_the_model_guards_remain_as_defence_in_depth(self):
        self.approve_document()
        folder = self.document.folder
        with self.assertRaises(ValidationError), transaction.atomic():
            folder.delete()
        self.assertTrue(Document.objects.filter(pk=self.document.pk).exists())
        with self.assertRaises(ValidationError):
            self.document.delete()

    def test_the_preflights_see_folder_and_project_evidence(self):
        from datetime import date

        from project_management.models import Project
        from project_management.services.document_workflow import (
            folder_has_evidence,
            project_has_evidence,
        )

        self.assertFalse(folder_has_evidence(self.document.folder))
        self.approve_document()
        self.assertTrue(folder_has_evidence(self.document.folder))
        project = Project.objects.create(
            project_name="Cascade project",
            user=self.creator,
            company_profile=self.document.folder.company_profile,
            contract_value=0,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
        )
        self.assertFalse(project_has_evidence(project))
        Folder.objects.filter(pk=self.document.folder.pk).update(project=project)
        self.assertTrue(project_has_evidence(project))


@override_settings(MEDIA_ROOT=MEDIA)
class EvidenceProjectApiTests(WorkflowTestBase):
    """Project DELETE: refused cleanly when its documents carry evidence."""

    def setUp(self):
        super().setUp()
        from datetime import date

        from project_management.models import Project

        self.project = Project.objects.create(
            project_name="Evidence project",
            user=self.creator,
            company_profile=self.document.folder.company_profile,
            contract_value=0,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
        )
        self.folder = Folder.objects.create(project=self.project, name="Project docs")
        self.project_doc = Document(
            folder=self.folder, file_name="p.txt", created_by=self.creator
        )
        self.project_doc.file.save("p.txt", ContentFile(b"p"), save=False)
        self.project_doc.save()
        self.project_revision = add_document_revision(
            self.project_doc, ContentFile(b"pv", name="p.txt"), self.creator
        )

    def delete_project_via_api(self):
        from project_management.views.project import ProjectViewSet

        view = ProjectViewSet.as_view({"delete": "destroy"})
        request = APIRequestFactory().delete("/x/")
        force_authenticate(request, user=self.creator)
        with patch.object(
            ProjectViewSet, "get_permissions", return_value=[]
        ), patch.object(ProjectViewSet, "get_object", return_value=self.project):
            return view(request, pk=self.project.pk)

    def totals(self):
        return (
            Document.objects.count(),
            DocumentRevision.objects.count(),
            DocumentWorkflow.objects.count(),
            DocumentWorkflowEvent.objects.count(),
        )

    def test_a_project_with_approved_documents_is_refused_cleanly(self):
        from project_management.models import Project

        wf = self.start(TWO, revision=self.project_revision)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        before = self.totals()
        response = self.delete_project_via_api()
        self.assertEqual(response.status_code, 400)
        self.assertIn("cannot be deleted", response.data["detail"])
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertEqual(self.totals(), before)

    def test_a_project_with_only_drafts_keeps_the_existing_delete_behaviour(self):
        response = self.delete_project_via_api()
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Document.objects.filter(pk=self.project_doc.pk).exists())
