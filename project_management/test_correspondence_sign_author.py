# CORR-SIGN-1: the SIGN_AUTHOR ("simple signature by the author") correspondence workflow type.
# Same engine as ONE_STEP/TWO_STEP; the signer is decided server-side. An internal validation
# signature -- never a certified electronic signature, never an email, never ISSUED.
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIClient

from models.models import CompanyProfile, User
from project_management.models import (
    Correspondence,
    CorrespondenceSendEvent,
    CorrespondenceValidationWorkflow,
)
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_workflow import (
    perform_action,
    ready_to_send,
    start_validation,
)
from project_management.test_correspondence_validation_workflow import (
    ONE_STEP,
    TWO_STEP,
    Status,
    WorkflowTestBase,
)

SIGN_AUTHOR = CorrespondenceValidationWorkflow.WorkflowType.SIGN_AUTHOR


class SignAuthorTestBase(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        # The global product-license check queries package_license tables that do not exist in
        # the test database and is unrelated to this lot; the correspondence permission and the
        # workflow rules under test are NOT stubbed.
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_sign(self, corr=None, user=None):
        corr = corr or self.create_out(recipient_company=self.client_company.id)
        user = user or self.author
        return corr, start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=user)

    def api(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def workflow_url(self, corr, suffix=""):
        return f"/api/project_management/correspondence/{corr.id}/workflow{suffix}"


class SignAuthorStartTests(SignAuthorTestBase):
    def test_start_makes_the_starting_user_sole_author_and_approver(self):
        corr, wf = self.start_sign()
        self.assertEqual(wf.workflow_type, SIGN_AUTHOR)
        self.assertEqual(wf.author_id, self.author.id)
        self.assertEqual(wf.approver_id, self.author.id)
        self.assertIsNone(wf.verifier)
        self.assertEqual(wf.status, Status.PENDING_APPROVAL)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        start_event = wf.events.get()
        self.assertEqual((start_event.step, start_event.action), ("START", "SUBMIT"))
        self.assertEqual(start_event.actor_id, self.author.id)

    def test_signer_is_the_user_starting_it_even_when_started_by_someone_else(self):
        corr, wf = self.start_sign(user=self.approver)
        self.assertEqual(wf.author_id, self.approver.id)
        self.assertEqual(wf.approver_id, self.approver.id)

    def test_api_start_needs_only_the_type_and_ignores_no_client_identity(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        response = self.api(self.author).post(
            self.workflow_url(corr), {"workflow_type": "SIGN_AUTHOR"}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["author"]["id"], self.author.id)
        self.assertEqual(response.data["approver"]["id"], self.author.id)
        self.assertIsNone(response.data["verifier"])
        self.assertEqual(response.data["status"], "PENDING_APPROVAL")

    def test_api_refuses_a_different_signer_or_a_verifier(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        client = self.api(self.author)
        for payload in (
            {"workflow_type": "SIGN_AUTHOR", "approver": self.approver.id},
            {"workflow_type": "SIGN_AUTHOR", "author": self.approver.id},
            {"workflow_type": "SIGN_AUTHOR", "verifier": self.verifier.id},
        ):
            response = client.post(self.workflow_url(corr), payload, format="json")
            self.assertEqual(response.status_code, 400, payload)
        self.assertFalse(CorrespondenceValidationWorkflow.objects.filter(correspondence=corr).exists())

    def test_api_requires_the_correspondence_update_permission(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        no_rights = self._company_member("norights")
        response = self.api(no_rights).post(
            self.workflow_url(corr), {"workflow_type": "SIGN_AUTHOR"}, format="json"
        )
        self.assertIn(response.status_code, (403, 404))
        self.assertFalse(CorrespondenceValidationWorkflow.objects.filter(correspondence=corr).exists())

    def test_user_of_another_company_cannot_start_it(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        other_admin = User.objects.create_user(
            email="other.admin@links-local.invalid", first_name="Other", last_name="Admin",
            title="Mr", user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_admin, company_name="Other Co", company_address="1", post_code="0",
            company_town="T", company_country="C", company_tell_no="0", company_mob_no="1",
            company_web_address="http://y", company_email_address="o@y.invalid",
            company_tax_reference="T2", company_iban="I2",
        )
        other_admin.company = other_company
        other_admin.save(update_fields=["company"])
        response = self.api(other_admin).post(
            self.workflow_url(corr), {"workflow_type": "SIGN_AUTHOR"}, format="json"
        )
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(ValidationError):
            start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=other_admin)

    def test_signer_must_be_in_the_allowed_scope(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        self.outsider.company = None
        self.outsider.save(update_fields=["company"])
        with self.assertRaises(ValidationError):
            start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=self.outsider)

    def test_issued_correspondence_cannot_start_it(self):
        corr = self.create_out(status="ISSUED", recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=self.author)

    def test_second_active_workflow_is_refused(self):
        corr, _ = self.start_sign()
        with self.assertRaises(ValidationError):
            start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=self.author)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)

    def test_database_constraint_refuses_a_verifier_on_sign_author(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            CorrespondenceValidationWorkflow.objects.create(
                correspondence=corr, workflow_type=SIGN_AUTHOR, status=Status.PENDING_APPROVAL,
                author=self.author, verifier=self.verifier, approver=self.author,
            )


class SignAuthorSigningTests(SignAuthorTestBase):
    def test_signing_approves_and_leaves_the_correspondence_draft_without_sending(self):
        corr, wf = self.start_sign()
        self.assertFalse(ready_to_send(corr))
        wf = perform_action(wf, self.author, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)
        self.assertIsNotNone(wf.completed_at)
        approve = wf.events.get(action="APPROVE")
        self.assertEqual((approve.step, approve.actor_id), ("APPROVE", self.author.id))
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertTrue(ready_to_send(corr))
        self.assertEqual(CorrespondenceSendEvent.objects.filter(correspondence=corr).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_signing_through_the_api_never_issues_or_sends(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        client = self.api(self.author)
        client.post(self.workflow_url(corr), {"workflow_type": "SIGN_AUTHOR"}, format="json")
        response = client.post(
            self.workflow_url(corr, "/actions"), {"action": "APPROVE", "comment": ""}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPROVED")
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertEqual(CorrespondenceSendEvent.objects.filter(correspondence=corr).count(), 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_another_user_cannot_sign(self):
        corr, wf = self.start_sign()
        for other in (self.approver, self.verifier, self.outsider):
            with self.assertRaises(PermissionDenied):
                perform_action(wf, other, "APPROVE", "")
        wf.refresh_from_db()
        self.assertEqual(wf.status, Status.PENDING_APPROVAL)

    def test_verify_is_not_an_action_of_this_type(self):
        _, wf = self.start_sign()
        with self.assertRaises(ValidationError):
            perform_action(wf, self.author, "VERIFY", "")

    def test_reject_requires_a_comment_then_allows_a_restart(self):
        corr, wf = self.start_sign()
        with self.assertRaises(ValidationError):
            perform_action(wf, self.author, "REJECT", "  ")
        wf = perform_action(wf, self.author, "REJECT", "Wrong recipient")
        self.assertEqual(wf.status, Status.REJECTED)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        restarted = start_validation(corr, SIGN_AUTHOR, None, None, None, started_by=self.author)
        self.assertNotEqual(restarted.pk, wf.pk)
        self.assertEqual(
            CorrespondenceValidationWorkflow.objects.filter(correspondence=corr).count(), 2
        )

    def test_content_is_locked_while_pending_and_after_signature(self):
        corr, wf = self.start_sign()

        def subject_change_is_refused():
            serializer = CorrespondenceSerializer(
                Correspondence.objects.get(pk=corr.pk), data={"subject": "Changed"},
                partial=True, context={"user": self.owner},
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("subject", serializer.errors)

        subject_change_is_refused()
        perform_action(wf, self.author, "APPROVE", "")
        subject_change_is_refused()


class ExistingTypesUnchangedTests(SignAuthorTestBase):
    def test_one_step_still_takes_a_distinct_approver(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        wf = start_validation(corr, ONE_STEP, self.author, None, self.approver, started_by=self.author)
        self.assertEqual((wf.author_id, wf.approver_id), (self.author.id, self.approver.id))
        with self.assertRaises(PermissionDenied):
            perform_action(wf, self.author, "APPROVE", "")
        wf = perform_action(wf, self.approver, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)

    def test_one_step_still_requires_an_approver_and_refuses_a_verifier(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, ONE_STEP, self.author, None, None, started_by=self.author)
        with self.assertRaises(ValidationError):
            start_validation(
                corr, ONE_STEP, self.author, self.verifier, self.approver, started_by=self.author
            )

    def test_two_step_still_requires_a_distinct_verifier(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, TWO_STEP, self.author, None, self.approver, started_by=self.author)
        wf = start_validation(
            corr, TWO_STEP, self.author, self.verifier, self.approver, started_by=self.author
        )
        self.assertEqual(wf.status, Status.PENDING_VERIFICATION)
        wf = perform_action(wf, self.verifier, "VERIFY", "")
        wf = perform_action(wf, self.approver, "APPROVE", "")
        self.assertEqual(wf.status, Status.APPROVED)

    def test_unknown_type_is_still_refused(self):
        corr = self.create_out(recipient_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            start_validation(corr, "THREE_STEP", self.author, None, self.approver, started_by=self.author)
