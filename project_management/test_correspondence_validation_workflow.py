"""LOT 3D-3D1: the internal validation workflow of an OUT Correspondence
(CorrespondenceValidationWorkflow/CorrespondenceValidationEvent) -- NOT DocumentWorkflow (see
that model's own docstring for why). Covers: 1-step / 2-step transitions, actor enforcement,
locking, rejection/restart, can_send, and that Correspondence.status/response_status are never
conflated with the validation state (sections 6/17).
"""
from django.core.exceptions import ValidationError
from rest_framework.exceptions import PermissionDenied

from models.models import User
from project_management.models import Correspondence, CorrespondenceValidationWorkflow, ProjectTeamMembers
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_workflow import (
    allowed_actor_queryset,
    get_latest_workflow,
    is_content_locked,
    perform_action,
    ready_to_send,
    start_validation,
    validation_state,
)
from project_management.test_correspondence import CorrespondenceTestBase, grant_correspondence_permissions

ONE_STEP = CorrespondenceValidationWorkflow.WorkflowType.ONE_STEP
TWO_STEP = CorrespondenceValidationWorkflow.WorkflowType.TWO_STEP
Status = CorrespondenceValidationWorkflow.Status


class WorkflowTestBase(CorrespondenceTestBase):
    def setUp(self):
        super().setUp()
        grant_correspondence_permissions(self.owner, self.company)
        self.author = self.owner
        self.verifier = self._company_member("verifier")
        self.approver = self._company_member("approver")
        self.outsider = self._company_member("outsider")

    def _company_member(self, tag):
        user = User.objects.create_user(
            email=f"{tag}@links-local.invalid", first_name=tag.title(), last_name="User",
            title="Mr", user_type="company_user",
        )
        user.company = self.company
        user.save(update_fields=["company"])
        return user

    def create_out(self, **data):
        # DRAFT by default: the model's own field default is ISSUED (unrelated to this lot),
        # but validation only ever applies to a DRAFT OUT (section 7).
        return self.create(**{"direction": "OUT", "status": "DRAFT", **data})

    def create_out_project(self, **data):
        return self.create(**{
            "direction": "OUT", "project": self.project.id, "status": "DRAFT", **data,
        })

    def add_to_project_team(self, user):
        ProjectTeamMembers.objects.update_or_create(
            project=self.project, team_member=user,
            defaults={"role": "Editor", "project_access": "RW"},
        )


class OneStepTransitionTests(WorkflowTestBase):
    def test_draft_to_approved_is_ready_to_send(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        self.assertEqual(validation_state(corr), "DRAFT")
        self.assertFalse(ready_to_send(corr))

        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        self.assertEqual(wf.status, Status.PENDING_APPROVAL)
        self.assertEqual(validation_state(corr), Status.PENDING_APPROVAL)

        wf = perform_action(wf, self.approver, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)
        self.assertEqual(validation_state(corr), Status.APPROVED)
        self.assertTrue(ready_to_send(corr))
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)


class TwoStepTransitionTests(WorkflowTestBase):
    def test_draft_to_verify_to_approved_is_ready_to_send(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(
            corr, TWO_STEP, self.author, self.verifier, self.approver, started_by=self.author,
        )
        self.assertEqual(wf.status, Status.PENDING_VERIFICATION)

        wf = perform_action(wf, self.verifier, "VERIFY", "")
        self.assertEqual(wf.status, Status.PENDING_APPROVAL)

        wf = perform_action(wf, self.approver, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)
        self.assertTrue(ready_to_send(corr))

    def test_wrong_actor_rejected(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(
            corr, TWO_STEP, self.author, self.verifier, self.approver, started_by=self.author,
        )
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.approver, "VERIFY", "")
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.outsider, "VERIFY", "")

    def test_wrong_order_rejected(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(
            corr, TWO_STEP, self.author, self.verifier, self.approver, started_by=self.author,
        )
        with self.assertRaises(ValidationError):
            perform_action(wf, self.approver, "APPROVE", "")


class ActorRuleTests(WorkflowTestBase):
    def test_author_required(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, None, None, self.approver, started_by=self.author)

    def test_approver_required(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, None, started_by=self.author)

    def test_verifier_required_for_two_step(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, TWO_STEP, self.author, None, self.approver, started_by=self.author)

    def test_one_step_rejects_a_verifier(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, self.verifier, self.approver, started_by=self.author)

    def test_author_cannot_equal_verifier(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, TWO_STEP, self.author, self.author, self.approver, started_by=self.author)

    def test_author_may_equal_approver(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.author, started_by=self.author)
        self.assertEqual(wf.approver_id, self.author.id)

    def test_verifier_may_equal_approver(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(
            corr, TWO_STEP, self.author, self.verifier, self.verifier, started_by=self.author,
        )
        self.assertEqual(wf.verifier_id, wf.approver_id)

    def test_actor_must_be_in_allowed_scope(self):
        outsider2 = User.objects.create_user(
            email="stranger@other-invalid.test", first_name="S", last_name="T",
            title="Mr", user_type="company_user",
        )
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, outsider2, started_by=self.author)

    def test_project_scope_allowed_actors_are_the_project_team_plus_the_company(self):
        # Live-validation finding (LOT 3D-3D1): the frontend's own picker for Project scope is
        # project-team UNION company (useInlineEditMemberOptions, sources ["project-team",
        # "company"]) -- the backend must accept exactly what that picker actually offers, not
        # a stricter project-team-only set that would reject a company member the UI itself let
        # the user pick.
        self.add_to_project_team(self.verifier)
        corr = self.create_out_project(recipient_company=self.client_company.id)
        allowed = set(allowed_actor_queryset(corr).values_list("pk", flat=True))
        self.assertIn(self.verifier.id, allowed)
        self.assertIn(self.approver.id, allowed)

    def test_project_scope_excludes_a_user_from_a_different_company_entirely(self):
        outsider2 = User.objects.create_user(
            email="not-in-this-company@links-local.invalid", first_name="N", last_name="C",
            title="Mr", user_type="company_user",
        )
        corr = self.create_out_project(recipient_company=self.client_company.id)
        allowed = set(allowed_actor_queryset(corr).values_list("pk", flat=True))
        self.assertNotIn(outsider2.id, allowed)


class ScopeGuardTests(WorkflowTestBase):
    def test_in_cannot_start_validation(self):
        corr = self.create(direction="IN", sender_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)

    def test_issued_cannot_start_or_restart_validation(self):
        corr = self.create_out(status="ISSUED", recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)

    def test_cannot_start_a_second_active_workflow(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)


class LockingTests(WorkflowTestBase):
    def _locked_corr(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        corr.refresh_from_db()
        return corr

    def test_content_is_locked_while_active(self):
        corr = self._locked_corr()
        self.assertTrue(is_content_locked(corr))

    def test_editing_locked_subject_is_rejected_server_side(self):
        corr = self._locked_corr()
        serializer = CorrespondenceSerializer(
            corr, data={"subject": "New subject"}, partial=True, context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("subject", serializer.errors)

    def test_editing_locked_recipient_is_rejected(self):
        from project_management.models import ContactCompany

        other_company = ContactCompany.objects.create(
            company_profile=self.company, company_name="Other Recipient Co",
            contact_email="other-recipient@links-local.invalid",
        )
        corr = self._locked_corr()  # recipient_company is self.client_company
        serializer = CorrespondenceSerializer(
            corr, data={"recipient_company": other_company.id}, partial=True,
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("recipient_company", serializer.errors)

    def test_responsible_and_response_tracking_remain_editable_while_locked(self):
        corr = self._locked_corr()
        serializer = CorrespondenceSerializer(
            corr, data={"responsible": self.approver.id, "response_required": True},
            partial=True, context={"user": self.owner},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_content_stays_locked_after_approval(self):
        corr = self._locked_corr()
        workflow = get_latest_workflow(corr)
        perform_action(workflow, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        self.assertTrue(is_content_locked(corr))

    def test_content_unlocked_after_rejection(self):
        corr = self._locked_corr()
        workflow = get_latest_workflow(corr)
        perform_action(workflow, self.approver, "REJECT", "Needs rework")
        corr.refresh_from_db()
        self.assertFalse(is_content_locked(corr))
        serializer = CorrespondenceSerializer(
            corr, data={"subject": "Reworked subject"}, partial=True, context={"user": self.owner},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class RejectionRestartTests(WorkflowTestBase):
    def test_reject_requires_a_comment(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        with self.assertRaises(ValidationError):
            perform_action(wf, self.approver, "REJECT", "")

    def test_positive_action_comment_is_optional(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        wf = perform_action(wf, self.approver, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)

    def test_correspondence_stays_draft_after_rejection(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "REJECT", "No good")
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)

    def test_restart_creates_a_new_row_and_preserves_history(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "REJECT", "No good")
        wf2 = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        self.assertNotEqual(wf.id, wf2.id)
        self.assertEqual(corr.validation_workflows.count(), 2)
        self.assertEqual(
            CorrespondenceValidationWorkflow.objects.get(pk=wf.id).status, Status.REJECTED,
        )

    def test_restarted_validation_remains_auditable(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "REJECT", "No good")
        wf2 = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf2, self.approver, "APPROVE", "Good now")
        events = list(get_latest_workflow(corr).events.all())
        self.assertEqual([e.action for e in events], ["SUBMIT", "APPROVE"])
        old_events = list(
            CorrespondenceValidationWorkflow.objects.get(pk=wf.id).events.all()
        )
        self.assertEqual([e.action for e in old_events], ["SUBMIT", "REJECT"])


class ReadyToSendContractTests(WorkflowTestBase):
    def test_false_before_approval(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        self.assertFalse(ready_to_send(corr))
        start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        self.assertFalse(ready_to_send(corr))

    def test_true_after_approval(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        self.assertTrue(ready_to_send(corr))

    def test_false_for_in_even_if_somehow_approved_state_existed(self):
        corr = self.create(direction="IN", sender_company=self.client_company.id)
        self.assertFalse(ready_to_send(corr))

    def test_false_without_a_recipient(self):
        # CorrespondenceTestBase.create defaults recipient_company for every OUT -- explicitly
        # cleared here since this test is specifically about that field being absent.
        corr = self.create_out(recipient_company=None)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        self.assertFalse(ready_to_send(corr))


class ParentResponseStatusBehaviorTests(WorkflowTestBase):
    """Section 6/17: approving the reply's validation must never mark the parent ANSWERED --
    that stays reserved for the future successful SEND."""

    def test_parent_stays_in_preparation_through_validation_approval(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        reply = self.create(direction="OUT", parent_correspondence=parent.id, status="DRAFT")
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        wf = start_validation(reply, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        perform_action(wf, self.approver, "APPROVE", "")
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)
        reply.refresh_from_db()
        self.assertEqual(reply.status, Correspondence.Status.DRAFT)


class EventAuditTests(WorkflowTestBase):
    def test_event_trail_records_actor_step_action_timestamp(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "Looks good")
        events = list(wf.events.all())
        self.assertEqual(len(events), 2)
        start_event, approve_event = events
        self.assertEqual(start_event.action, "SUBMIT")
        self.assertEqual(start_event.actor_id, self.author.id)
        self.assertIsNotNone(start_event.created_at)
        self.assertEqual(approve_event.action, "APPROVE")
        self.assertEqual(approve_event.actor_id, self.approver.id)
        self.assertEqual(approve_event.comment, "Looks good")

    def test_events_are_immutable(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        event = wf.events.first()
        event.comment = "tampered"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()

    def test_a_completed_workflow_cannot_be_modified_directly(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        wf.refresh_from_db()
        wf.status = Status.PENDING_APPROVAL
        with self.assertRaises(ValidationError):
            wf.save()
