from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.http import Http404
from django.test import TestCase, override_settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import User
from project_management.models import (
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
)
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import (
    get_workflow_for_revision,
    perform_action,
    start_workflow,
)
from project_management.test_document_revision import (
    MEDIA,
    NoNotificationsMixin,
    make_document,
)
from project_management.views.document_workflow import DocumentWorkflowViewSet

TWO = DocumentWorkflow.WorkflowType.TWO_STEP
THREE = DocumentWorkflow.WorkflowType.THREE_STEP
Status = DocumentWorkflow.Status


def make_user(name):
    return User.objects.create_user(
        email=f"{name}@links-local.invalid",
        first_name=name.title(),
        last_name="Wf",
        title="Mr",
        user_type="company_user",
    )


@override_settings(MEDIA_ROOT=MEDIA)
class WorkflowTestBase(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        self.creator, self.document = make_document()
        self.alice, self.bob, self.carol, self.dave = (
            make_user(n) for n in ("alice", "bob", "carol", "dave")
        )
        # Two stored versions: only the latest one is locked by a running workflow.
        self.revision = self.new_revision(b"rev")
        self.revision2 = self.new_revision(b"rev2")

    def new_revision(self, content):
        return add_document_revision(
            self.document, ContentFile(content, name="plan.txt"), self.creator
        )

    def start(self, workflow_type=THREE, author=None, verifier=None, approver=None,
              revision=None, **kwargs):
        author = author or self.alice
        if workflow_type == THREE:
            verifier = verifier or self.bob
        approver = approver or self.carol
        return start_workflow(
            revision or self.revision,
            workflow_type,
            author,
            verifier,
            approver,
            self.creator,
            **kwargs,
        )


class WorkflowTransitionTests(WorkflowTestBase):
    def test_two_step_author_then_approver(self):
        wf = self.start(TWO)
        self.assertEqual(wf.status, Status.DRAFT)
        self.assertIsNone(wf.verifier)
        self.assertEqual(perform_action(wf, self.alice, "SIGN", "ok").status, Status.APPROVE)
        wf = perform_action(wf, self.carol, "APPROVE", "approved")
        self.assertEqual(wf.status, Status.APPROVED)
        self.assertIsNotNone(wf.completed_at)
        self.assertEqual(
            [(e.step, e.action) for e in wf.events.all()],
            [("START", "START"), ("AUTHOR_SIGN", "SIGN"), ("APPROVE", "APPROVE")],
        )

    def test_three_step_author_verifier_approver(self):
        wf = self.start(THREE)
        self.assertEqual(perform_action(wf, self.alice, "SIGN", "s").status, Status.VERIFY)
        self.assertEqual(perform_action(wf, self.bob, "VALIDATE", "v").status, Status.APPROVE)
        self.assertEqual(perform_action(wf, self.carol, "APPROVE", "a").status, Status.APPROVED)
        self.assertEqual(wf.events.count(), 4)

    def test_author_cannot_be_the_verifier(self):
        with self.assertRaises(ValidationError) as ctx:
            self.start(THREE, author=self.alice, verifier=self.alice)
        self.assertIn("verifier", ctx.exception.message_dict)
        self.assertFalse(DocumentWorkflow.objects.exists())

    def test_author_may_be_the_approver_and_verifier_may_be_the_approver(self):
        first = self.start(THREE, author=self.alice, verifier=self.bob, approver=self.alice)
        second = self.start(
            THREE,
            author=self.alice,
            verifier=self.bob,
            approver=self.bob,
            revision=self.revision2,
        )
        self.assertEqual(first.approver, first.author)
        self.assertEqual(second.approver, second.verifier)

    def test_required_actors_and_type_rules(self):
        with self.assertRaises(ValidationError):
            start_workflow(self.revision, THREE, self.alice, None, self.carol, self.creator)
        with self.assertRaises(ValidationError):
            start_workflow(self.revision, TWO, self.alice, self.bob, self.carol, self.creator)
        with self.assertRaises(ValidationError):
            start_workflow(self.revision, TWO, None, None, self.carol, self.creator)
        with self.assertRaises(ValidationError):
            start_workflow(self.revision, "OTHER", self.alice, None, self.carol, self.creator)

    def test_actors_must_be_in_the_allowed_population(self):
        allowed = User.objects.filter(pk__in=[self.alice.pk, self.carol.pk])
        with self.assertRaises(ValidationError) as ctx:
            self.start(THREE, allowed_users=allowed)  # bob (verifier) not allowed
        self.assertIn("verifier", ctx.exception.message_dict)

    def test_wrong_actor_is_denied(self):
        wf = self.start(THREE)
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.bob, "SIGN", "not my step")
        perform_action(wf, self.alice, "SIGN", "s")
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.alice, "VALIDATE", "author is not the verifier")
        self.assertEqual(wf.events.count(), 2)

    def test_wrong_order_is_rejected(self):
        wf = self.start(THREE)
        for action, actor in (("VALIDATE", self.bob), ("APPROVE", self.carol)):
            with self.assertRaises(ValidationError):
                perform_action(wf, actor, action, "too early")
        perform_action(wf, self.alice, "SIGN", "s")
        with self.assertRaises(ValidationError):
            perform_action(wf, self.alice, "SIGN", "twice")
        two = self.start(TWO, revision=self.revision2)
        perform_action(two, self.alice, "SIGN", "s")
        with self.assertRaises(ValidationError):
            perform_action(two, self.bob, "VALIDATE", "no verifier step")

    def test_comment_is_required_for_every_decision(self):
        wf = self.start(TWO)
        for comment in ("", "   ", None):
            with self.assertRaises(ValidationError):
                perform_action(wf, self.alice, "SIGN", comment)
        with self.assertRaises(ValidationError):
            perform_action(wf, self.alice, "REJECT", "")
        wf.refresh_from_db()
        self.assertEqual(wf.status, Status.DRAFT)
        self.assertEqual(wf.events.count(), 1)  # START only

    def test_rejection_ends_in_rejected_and_is_final(self):
        wf = self.start(THREE)
        perform_action(wf, self.alice, "SIGN", "s")
        wf = perform_action(wf, self.bob, "REJECT", "Not good enough")
        self.assertEqual(wf.status, Status.REJECTED)
        self.assertIsNotNone(wf.completed_at)
        last = wf.events.last()
        self.assertEqual((last.step, last.action, last.actor), ("VERIFY", "REJECT", self.bob))
        with self.assertRaises(ValidationError):
            perform_action(wf, self.carol, "APPROVE", "too late")
        with self.assertRaises(PermissionDenied):
            perform_action(self.start(TWO, revision=self.revision2),
                           self.carol, "REJECT", "only the current actor can reject")

    def test_events_keep_actor_action_comment_and_timestamp(self):
        wf = self.start(TWO)
        perform_action(wf, self.alice, "SIGN", "Signed by me")
        event = wf.events.get(action="SIGN")
        self.assertEqual(event.actor, self.alice)
        self.assertEqual(event.step, "AUTHOR_SIGN")
        self.assertEqual(event.comment, "Signed by me")
        self.assertIsNotNone(event.created_at)

    def test_events_are_immutable(self):
        wf = self.start(TWO)
        event = wf.events.first()
        event.comment = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            DocumentWorkflowEvent.objects.filter(pk=event.pk).update(comment="x")
        with self.assertRaises(ValidationError):
            DocumentWorkflowEvent.objects.filter(pk=event.pk).delete()
        self.assertEqual(DocumentWorkflowEvent.objects.get(pk=event.pk).comment, "")


class WorkflowRevisionTests(WorkflowTestBase):
    def test_each_workflow_belongs_to_its_own_revision(self):
        second = self.revision2
        wf1 = self.start(TWO)
        wf2 = self.start(THREE, revision=second)
        self.assertEqual(get_workflow_for_revision(self.revision).pk, wf1.pk)
        self.assertEqual(get_workflow_for_revision(second).pk, wf2.pk)
        self.assertEqual(wf1.revision_id, self.revision.pk)

    def test_only_one_active_workflow_per_revision(self):
        self.start(TWO)
        with self.assertRaises(ValidationError):
            self.start(TWO)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DocumentWorkflow.objects.create(
                revision=self.revision, workflow_type=TWO, status=Status.DRAFT,
                author=self.alice, approver=self.carol,
            )

    def test_active_workflow_locks_the_current_revision_and_file(self):
        wf = self.start(TWO, revision=self.revision2)  # the current (latest) revision
        with self.assertRaises(ValidationError):
            self.new_revision(b"blocked")
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), 3)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        self.new_revision(b"after approval")  # unlocked once the workflow is finished
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), 4)

    def test_document_file_replacement_is_blocked_during_a_workflow(self):
        from project_management.services.document_workflow import assert_document_file_unlocked

        assert_document_file_unlocked(self.document)  # no workflow: fine
        self.start(TWO, revision=self.revision2)
        with self.assertRaises(ValidationError):
            assert_document_file_unlocked(self.document)


@override_settings(MEDIA_ROOT=MEDIA)
class WorkflowApiTests(WorkflowTestBase):
    ACCESS = "project_management.views.document_workflow.get_register_document_or_404"
    CAN_UPDATE = "project_management.views.document_workflow.assert_can_update_document"
    ALLOWED = "project_management.views.document_workflow.get_allowed_owner_queryset"

    def call(self, method, user, data=None, action_view=False, revision_id=None):
        mapping = {"post": "act"} if action_view else {"get": "retrieve", "post": "start"}
        view = DocumentWorkflowViewSet.as_view(mapping, permission_classes=[])
        factory = APIRequestFactory()
        request = getattr(factory, method)("/x/", data or {}, format="json")
        force_authenticate(request, user=user)
        return view(
            request,
            document_id=self.document.id,
            revision_id=revision_id or self.revision.id,
        )

    def payload(self, **kw):
        data = {
            "workflow_type": THREE,
            "author": self.alice.pk,
            "verifier": self.bob.pk,
            "approver": self.carol.pk,
        }
        data.update(kw)
        return data

    def everything_allowed(self):
        stack = [
            patch(self.ACCESS, return_value=self.document),
            patch(self.CAN_UPDATE),
            patch(self.ALLOWED, return_value=User.objects.all()),
        ]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_get_without_workflow_then_start_and_walk_through(self):
        self.everything_allowed()
        self.assertIsNone(self.call("get", self.creator).data["workflow"])
        started = self.call("post", self.creator, self.payload())
        self.assertEqual(started.status_code, 201)
        self.assertEqual(started.data["status"], "DRAFT")
        self.assertEqual(started.data["revision"], self.revision.id)
        for user, action, expected in (
            (self.alice, "SIGN", "VERIFY"),
            (self.bob, "VALIDATE", "APPROVE"),
            (self.carol, "APPROVE", "APPROVED"),
        ):
            response = self.call(
                "post", user, {"action": action, "comment": "c"}, action_view=True
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["status"], expected)
        got = self.call("get", self.dave).data["workflow"]
        self.assertEqual(len(got["events"]), 4)
        self.assertEqual(got["events"][1]["actor"]["id"], self.alice.pk)

    def test_wrong_actor_403_wrong_order_and_missing_comment_400(self):
        self.everything_allowed()
        self.call("post", self.creator, self.payload())
        self.assertEqual(
            self.call("post", self.bob, {"action": "SIGN", "comment": "c"}, True).status_code, 403
        )
        self.assertEqual(
            self.call("post", self.alice, {"action": "APPROVE", "comment": "c"}, True).status_code, 400
        )
        self.assertEqual(
            self.call("post", self.alice, {"action": "SIGN", "comment": ""}, True).status_code, 400
        )
        self.assertEqual(
            self.call("post", self.alice, {"action": "REJECT", "comment": "no"}, True).data["status"],
            "REJECTED",
        )

    def test_invalid_start_is_a_400(self):
        self.everything_allowed()
        response = self.call(
            "post", self.creator, self.payload(verifier=self.alice.pk)
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("verifier", response.data)

    def test_inaccessible_document_is_a_404(self):
        with patch(self.ACCESS, side_effect=Http404):
            self.assertEqual(self.call("get", self.creator).status_code, 404)
            self.assertEqual(self.call("post", self.creator, self.payload()).status_code, 404)
            self.assertEqual(
                self.call("post", self.alice, {"action": "SIGN", "comment": "c"}, True).status_code, 404
            )

    def test_revision_of_another_document_is_a_404(self):
        self.everything_allowed()
        self.assertEqual(self.call("get", self.creator, revision_id=999999).status_code, 404)

    def test_starting_needs_the_update_permission(self):
        with patch(self.ACCESS, return_value=self.document), patch(
            self.CAN_UPDATE, side_effect=PermissionDenied
        ), patch(self.ALLOWED, return_value=User.objects.all()):
            self.assertEqual(self.call("post", self.creator, self.payload()).status_code, 403)
        self.assertFalse(DocumentWorkflow.objects.exists())
