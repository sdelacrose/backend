from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import User
from project_management.models import Document, DocumentRevision, DocumentWorkflow
from project_management.services.document_revision import create_document_revision
from project_management.services.document_workflow import (
    change_actor,
    get_workflow_for_revision,
    perform_action,
    start_workflow_for_document,
)
from project_management.test_document_workflow import (
    THREE,
    TWO,
    Status,
    WorkflowTestBase,
)
from project_management.views.document_revision import DocumentRevisionViewSet
from project_management.views.document_workflow import DocumentWorkflowViewSet


def event_snapshot(workflow):
    return [
        (e.id, e.step, e.action, e.actor_id, e.comment, e.created_at)
        for e in workflow.events.all()
    ]


def read_file(revision):
    with revision.file.open("rb") as handle:
        return handle.read()


class WorkflowLifecycleTests(WorkflowTestBase):
    """Rejection / approval / correction: history is never rewritten."""

    def revise(self, kind="minor", content=b"corrected"):
        return create_document_revision(
            self.document,
            ContentFile(content, name="plan.txt"),
            self.creator,
            kind,
            "Correction",
        )

    def reject_current(self):
        wf = self.start(THREE, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "signed")
        perform_action(wf, self.bob, "VALIDATE", "verified")
        return perform_action(wf, self.carol, "REJECT", "Wrong dimensions")

    def approve_current(self):
        wf = self.start(TWO, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "signed")
        return perform_action(wf, self.carol, "APPROVE", "ok")

    def test_active_workflow_blocks_minor_and_major(self):
        self.start(TWO, revision=self.revision2)
        for kind in ("minor", "major"):
            with self.assertRaises(ValidationError):
                self.revise(kind)

    def test_rejected_revision_can_be_corrected_and_history_is_untouched(self):
        rejected = self.reject_current()
        before_events = event_snapshot(rejected)
        before_file = read_file(self.revision2)
        before_path = self.revision2.file.name

        corrected = self.revise("minor", b"fixed")

        self.assertEqual(corrected.label, "0-03")
        # The reviewed revision, its file and its rejected workflow are unchanged.
        self.revision2.refresh_from_db()
        self.assertEqual(self.revision2.file.name, before_path)
        self.assertEqual(read_file(self.revision2), before_file)
        rejected.refresh_from_db()
        self.assertEqual(rejected.status, Status.REJECTED)
        self.assertEqual(event_snapshot(rejected), before_events)
        self.assertEqual(rejected.events.last().comment, "Wrong dimensions")
        # The correction is a new revision with no workflow yet, and becomes current.
        self.assertIsNone(get_workflow_for_revision(corrected))
        self.document.refresh_from_db()
        self.assertEqual(self.document.file.name, corrected.file.name)

    def test_corrected_revision_gets_its_own_separate_workflow(self):
        rejected = self.reject_current()
        corrected = self.revise()
        second = self.start(THREE, revision=corrected)
        perform_action(second, self.alice, "SIGN", "s2")
        perform_action(second, self.bob, "VALIDATE", "v2")
        second = perform_action(second, self.carol, "APPROVE", "a2")
        self.assertEqual(second.status, Status.APPROVED)
        self.assertNotEqual(second.pk, rejected.pk)
        first_ids = set(rejected.events.values_list("id", flat=True))
        second_ids = set(second.events.values_list("id", flat=True))
        self.assertFalse(first_ids & second_ids)
        self.assertEqual(second.revision_id, corrected.pk)
        self.assertEqual(rejected.revision_id, self.revision2.pk)
        self.assertEqual(rejected.events.count(), 4)  # start, sign, validate, reject
        self.assertEqual(second.events.count(), 4)  # start, sign, validate, approve

    def test_approved_revision_allows_next_minor(self):
        approved = self.approve_current()
        self.assertEqual(self.revise("minor").label, "0-03")
        approved.refresh_from_db()
        self.assertEqual(approved.status, Status.APPROVED)

    def test_approved_revision_allows_next_major(self):
        approved = self.approve_current()
        before = event_snapshot(approved)
        self.assertEqual(self.revise("major").label, "1-00")
        self.assertEqual(event_snapshot(approved), before)
        self.assertEqual(read_file(self.revision2), b"rev2")


class WorkflowActorChangeTests(WorkflowTestBase):
    def test_actors_can_be_replaced_before_their_step(self):
        wf = self.start(THREE, revision=self.revision2)
        change_actor(wf, self.alice, "approver", self.dave, "Carol is away")
        change_actor(wf, self.alice, "verifier", self.dave, "Bob is away")
        wf.refresh_from_db()
        self.assertEqual((wf.verifier, wf.approver), (self.dave, self.dave))
        perform_action(wf, self.alice, "SIGN", "s")
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.bob, "VALIDATE", "old verifier")
        perform_action(wf, self.dave, "VALIDATE", "new verifier")
        # The approver can still be replaced while its step is pending.
        change_actor(wf, self.alice, "approver", self.carol, "Back again")
        wf.refresh_from_db()
        self.assertEqual(wf.approver, self.carol)

    def test_replacement_creates_an_immutable_audit_event(self):
        wf = self.start(THREE, revision=self.revision2)
        change_actor(wf, self.alice, "verifier", self.dave, "Bob is on leave")
        event = wf.events.get(action="CHANGE_ACTOR")
        self.assertEqual(event.step, "VERIFY")
        self.assertEqual(event.actor, self.alice)  # who made the change
        self.assertEqual(event.previous_actor, self.bob)
        self.assertEqual(event.new_actor, self.dave)
        self.assertEqual(event.comment, "Bob is on leave")
        self.assertIsNotNone(event.created_at)
        event.comment = "edited"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_replacement_requires_a_reason(self):
        wf = self.start(THREE, revision=self.revision2)
        for comment in ("", "  ", None):
            with self.assertRaises(ValidationError):
                change_actor(wf, self.alice, "verifier", self.dave, comment)
        wf.refresh_from_db()
        self.assertEqual(wf.verifier, self.bob)
        self.assertEqual(wf.events.count(), 1)

    def test_a_completed_step_cannot_be_replaced(self):
        wf = self.start(THREE, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.bob, "VALIDATE", "v")
        with self.assertRaises(ValidationError):
            change_actor(wf, self.alice, "verifier", self.dave, "too late")
        wf.refresh_from_db()
        self.assertEqual(wf.verifier, self.bob)

    def test_terminal_workflows_cannot_change_actors(self):
        approved = self.start(TWO, revision=self.revision)
        perform_action(approved, self.alice, "SIGN", "s")
        perform_action(approved, self.carol, "APPROVE", "a")
        rejected = self.start(THREE, revision=self.revision2)
        perform_action(rejected, self.alice, "REJECT", "no")
        for wf in (approved, rejected):
            for role in ("approver", "verifier"):
                with self.assertRaises(ValidationError):
                    change_actor(wf, self.alice, role, self.dave, "nope")
        approved.refresh_from_db()
        self.assertEqual(approved.approver, self.carol)

    def test_rules_author_only_inequality_and_roles(self):
        wf = self.start(THREE, revision=self.revision2)
        with self.assertRaises(PermissionDenied):
            change_actor(wf, self.bob, "approver", self.dave, "not the author")
        with self.assertRaises(ValidationError):  # author != verifier stays enforced
            change_actor(wf, self.alice, "verifier", self.alice, "self")
        with self.assertRaises(ValidationError):
            change_actor(wf, self.alice, "verifier", self.bob, "same person")
        with self.assertRaises(ValidationError):
            change_actor(wf, self.alice, "author", self.dave, "author is fixed")
        with self.assertRaises(ValidationError):
            change_actor(
                wf,
                self.alice,
                "verifier",
                self.dave,
                "x",
                allowed_users=User.objects.filter(pk=self.alice.pk),
            )
        # verifier == approver stays allowed
        change_actor(wf, self.alice, "approver", self.bob, "same as verifier")
        two = self.start(TWO, revision=self.revision)
        with self.assertRaises(ValidationError):
            change_actor(two, self.alice, "verifier", self.dave, "no verifier here")
        self.assertEqual(wf.events.filter(action="CHANGE_ACTOR").count(), 1)


class LegacyMaterializationTests(WorkflowTestBase):
    ACCESS = "project_management.views.document_workflow.get_register_document_or_404"
    REV_ACCESS = (
        "project_management.views.document_revision.get_register_document_or_404"
    )
    CAN_UPDATE = (
        "project_management.views.document_workflow.assert_can_update_document"
    )
    ALLOWED = "project_management.views.document_workflow.get_allowed_owner_queryset"

    def setUp(self):
        super().setUp()
        # A legacy document: a file but no stored revision.
        self.legacy = Document(
            folder=self.document.folder, file_name="old.txt", created_by=self.creator
        )
        self.legacy.file.save("old.txt", ContentFile(b"legacy"), save=False)
        self.legacy.save()

    def revisions_of_legacy(self):
        return DocumentRevision.objects.filter(document=self.legacy)

    def test_first_revision_is_materialized_only_when_the_workflow_starts(self):
        self.assertEqual(self.revisions_of_legacy().count(), 0)
        wf = start_workflow_for_document(
            self.legacy, TWO, self.alice, None, self.carol, self.creator
        )
        revisions = list(self.revisions_of_legacy())
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].label, "0-00")
        self.assertEqual(revisions[0].file.name, self.legacy.file.name)  # not copied
        self.assertEqual(read_file(revisions[0]), b"legacy")
        self.assertEqual(wf.revision_id, revisions[0].pk)

    def test_failed_start_creates_nothing(self):
        with self.assertRaises(ValidationError):
            start_workflow_for_document(
                self.legacy, THREE, self.alice, self.alice, self.carol, self.creator
            )
        self.assertEqual(self.revisions_of_legacy().count(), 0)
        self.assertFalse(
            DocumentWorkflow.objects.filter(revision__document=self.legacy).exists()
        )

    def test_existing_current_revision_is_reused_not_duplicated(self):
        wf = start_workflow_for_document(
            self.document, TWO, self.alice, None, self.carol, self.creator
        )
        self.assertEqual(wf.revision_id, self.revision2.pk)  # the latest one
        self.assertEqual(
            DocumentRevision.objects.filter(document=self.document).count(), 3
        )

    def test_read_only_access_creates_nothing(self):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.creator)
        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        with patch(self.REV_ACCESS, return_value=self.legacy):
            response = view(request, document_id=self.legacy.id)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["results"][0]["id"])  # synthetic entry
        self.assertIsNone(response.data["results"][0]["workflow"])
        self.assertEqual(self.revisions_of_legacy().count(), 0)
        self.assertFalse(
            DocumentWorkflow.objects.filter(revision__document=self.legacy).exists()
        )

    def test_start_endpoint_on_the_current_revision_of_a_legacy_document(self):
        view = DocumentWorkflowViewSet.as_view(
            {"post": "start_current"}, permission_classes=[]
        )
        request = APIRequestFactory().post(
            "/x/",
            {"workflow_type": TWO, "author": self.alice.pk, "approver": self.carol.pk},
            format="json",
        )
        force_authenticate(request, user=self.creator)
        with patch(self.ACCESS, return_value=self.legacy), patch(
            self.CAN_UPDATE
        ), patch(self.ALLOWED, return_value=User.objects.all()):
            response = view(request, document_id=self.legacy.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.revisions_of_legacy().count(), 1)
        self.assertEqual(
            response.data["revision"], self.revisions_of_legacy().first().pk
        )

    def test_actor_change_through_the_action_endpoint(self):
        wf = self.start(THREE, revision=self.revision2)
        view = DocumentWorkflowViewSet.as_view({"post": "act"}, permission_classes=[])

        def call(user, data):
            request = APIRequestFactory().post("/x/", data, format="json")
            force_authenticate(request, user=user)
            with patch(self.ACCESS, return_value=self.document), patch(
                self.ALLOWED, return_value=User.objects.all()
            ):
                return view(
                    request,
                    document_id=self.document.id,
                    revision_id=self.revision2.id,
                )

        ok = call(
            self.alice,
            {
                "action": "CHANGE_APPROVER",
                "new_actor": self.dave.pk,
                "comment": "Carol is away",
            },
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.data["approver"]["id"], self.dave.pk)
        change = [e for e in ok.data["events"] if e["action"] == "CHANGE_ACTOR"][0]
        self.assertEqual(change["previous_actor"]["id"], self.carol.pk)
        self.assertEqual(change["new_actor"]["id"], self.dave.pk)
        not_author = call(
            self.bob,
            {"action": "CHANGE_APPROVER", "new_actor": self.carol.pk, "comment": "x"},
        )
        self.assertEqual(not_author.status_code, 403)
        no_reason = call(
            self.alice,
            {"action": "CHANGE_VERIFIER", "new_actor": self.dave.pk, "comment": ""},
        )
        self.assertEqual(no_reason.status_code, 400)
        wf.refresh_from_db()
        self.assertEqual(wf.approver, self.dave)
