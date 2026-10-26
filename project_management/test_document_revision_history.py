from unittest.mock import patch

from django.core.files.base import ContentFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
)
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.test_document_control_revision_workflow import ProjectRevisionBase
from project_management.test_document_workflow import THREE, TWO
from project_management.views.document_revision import DocumentRevisionViewSet

ACCESS = "project_management.views.document_revision.get_register_document_or_404"


class RevisionHistoryPayloadBase(ProjectRevisionBase):
    """The revisions list API is the canonical Revision History payload."""

    def history(self, document=None):
        document = document or self.doc
        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.creator)
        with patch(ACCESS, return_value=document):
            response = view(request, document_id=document.id)
        self.assertEqual(response.status_code, 200)
        return response.data["results"]

    def row(self, revision, document=None):
        return next(r for r in self.history(document) if r["id"] == revision.id)

    def approve_three_step(self, revision=None):
        revision = revision or self.new_version()
        wf = start_workflow(revision, THREE, self.alice, self.bob, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.bob, "VALIDATE", "v")
        perform_action(wf, self.carol, "APPROVE", "a")
        revision.refresh_from_db()
        return revision


class HistoryRowTests(RevisionHistoryPayloadBase):
    def test_version_revision_author_dates_and_actors_of_an_approved_version(self):
        self.project_document()
        revision = self.approve_three_step()
        row = self.row(revision)
        self.assertEqual(row["version_number"], revision.version_number)
        self.assertEqual(row["revision_label"], "A")
        self.assertEqual(row["created_by"]["id"], self.creator.id)  # author of the version
        self.assertEqual(row["created_at"], revision.created_at)  # creation, not approval
        workflow = row["workflow"]
        self.assertEqual(workflow["status"], "APPROVED")
        self.assertEqual(workflow["workflow_type"], THREE)
        self.assertEqual(workflow["verifier"]["id"], self.bob.id)
        self.assertEqual(workflow["approver"]["id"], self.carol.id)
        completed = DocumentWorkflow.objects.get(revision=revision).completed_at
        self.assertEqual(workflow["approved_at"], completed)
        self.assertGreater(workflow["approved_at"], row["created_at"])

    def test_revision_label_zero_is_preserved(self):
        self.project_document(revision={"scheme": "NUMERIC", "start": "0"})
        row = self.row(self.approve_three_step())
        self.assertEqual(row["revision_label"], "0")
        self.assertIsNotNone(row["revision_label"])

    def test_two_step_has_no_reviewer_but_an_approver(self):
        self.project_document()
        revision = self.approve()  # TWO_STEP
        workflow = self.row(revision)["workflow"]
        self.assertEqual(workflow["workflow_type"], TWO)
        self.assertIsNone(workflow["verifier"])
        self.assertEqual(workflow["approver"]["id"], self.carol.id)
        self.assertIsNotNone(workflow["approved_at"])

    def test_rejected_and_pending_workflows_have_no_approval_date(self):
        self.project_document()
        rejected = self.reject()
        pending = self.new_version(b"p")
        start_workflow(pending, THREE, self.alice, self.bob, self.carol, self.creator)
        rejected_row, pending_row = self.row(rejected), self.row(pending)
        self.assertEqual(rejected_row["workflow"]["status"], "REJECTED")
        self.assertIsNone(rejected_row["workflow"]["approved_at"])
        self.assertIsNone(rejected_row["revision_label"])
        self.assertEqual(pending_row["workflow"]["status"], "DRAFT")
        self.assertIsNone(pending_row["workflow"]["approved_at"])
        self.assertIsNone(pending_row["revision_label"])
        # a rejected workflow does have a completion timestamp: it must not leak as approval
        self.assertIsNotNone(DocumentWorkflow.objects.get(revision=rejected).completed_at)

    def test_a_version_without_workflow_has_no_workflow_data(self):
        self.project_document()
        row = self.row(self.new_version())
        self.assertIsNone(row["workflow"])
        self.assertIsNone(row["revision_label"])


class HistoryListTests(RevisionHistoryPayloadBase):
    def build_history(self):
        """rejected, approved A, rejected, approved B, draft (current)."""
        self.project_document()
        versions = [
            self.reject(),
            self.approve_three_step(),
            self.reject(),
            self.approve_three_step(),
        ]
        current = self.new_version(b"current")
        wf = start_workflow(current, THREE, self.alice, self.bob, self.carol, self.creator)
        return versions + [current], wf

    def test_all_versions_stay_visible_with_labels_only_on_approved_ones(self):
        versions, _wf = self.build_history()
        rows = self.history()
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(
            [by_id[v.id]["revision_label"] for v in versions], [None, "A", None, "B", None]
        )
        self.assertEqual(
            [by_id[v.id]["workflow"]["status"] for v in versions],
            ["REJECTED", "APPROVED", "REJECTED", "APPROVED", "DRAFT"],
        )
        self.assertEqual(len([r for r in rows if r["id"] in by_id and r["workflow"]]), 5)

    def test_order_is_deterministic_by_version_number_newest_first(self):
        versions, _wf = self.build_history()
        rows = self.history()
        numbers = [r["version_number"] for r in rows]
        self.assertEqual(numbers, sorted(numbers, reverse=True))
        self.assertEqual(len(set(numbers)), len(numbers))
        self.assertEqual(rows[0]["id"], versions[-1].id)
        self.assertEqual([r["is_current"] for r in rows], [True] + [False] * (len(rows) - 1))

    def test_each_version_points_to_its_own_immutable_file(self):
        self.project_document()
        first = self.new_version(b"first")
        second = self.new_version(b"second")
        rows = {r["id"]: r for r in self.history()}
        self.assertNotEqual(rows[first.id]["file_url"], rows[second.id]["file_url"])
        self.assertTrue(rows[first.id]["file_url"].endswith(first.file.url))
        self.assertTrue(rows[second.id]["file_url"].endswith(second.file.url))
        # the Document's current file is the newest version: never used for older rows
        self.assertEqual(Document.objects.get(pk=self.doc.pk).file.name, second.file.name)
        self.assertNotEqual(rows[first.id]["file_url"].split("/media/")[-1], self.doc.file.name)

    def test_reading_the_history_changes_nothing(self):
        self.build_history()
        before = (
            list(DocumentRevision.objects.values_list("id", "version_number", "revision_label")),
            list(DocumentWorkflow.objects.values_list("id", "status", "completed_at")),
            DocumentWorkflowEvent.objects.count(),
            Document.objects.get(pk=self.doc.pk).file.name,
        )
        self.history()
        self.history()
        after = (
            list(DocumentRevision.objects.values_list("id", "version_number", "revision_label")),
            list(DocumentWorkflow.objects.values_list("id", "status", "completed_at")),
            DocumentWorkflowEvent.objects.count(),
            Document.objects.get(pk=self.doc.pk).file.name,
        )
        self.assertEqual(before, after)

    def test_workflow_actors_do_not_add_queries_per_version(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.project_document()
        self.approve_three_step()
        with CaptureQueriesContext(connection) as small:
            self.history()
        for _ in range(3):  # a longer history: verifier/approver must not add queries
            self.reject()
            self.approve_three_step()
        with CaptureQueriesContext(connection) as large:
            self.history()
        self.assertEqual(len(small), len(large))


class SyntheticLegacyRowTests(RevisionHistoryPayloadBase):
    def test_a_legacy_document_keeps_its_synthetic_row_and_is_not_materialized(self):
        legacy = Document(folder=self.document.folder, file_name="old.txt", created_by=self.creator)
        legacy.file.save("old.txt", ContentFile(b"old"), save=False)
        legacy.save()
        rows = self.history(legacy)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row["id"])
        self.assertEqual(row["version_number"], 1)
        self.assertIsNone(row["revision_label"])
        self.assertIsNone(row["workflow"])
        self.assertEqual(row["created_by"]["id"], self.creator.id)
        self.assertTrue(row["file_url"])
        self.assertTrue(row["is_current"])
        self.assertFalse(DocumentRevision.objects.filter(document=legacy).exists())
