"""LOT 3D-3D2: the SEND step for an approved OUT Correspondence and its auditable evidence
(CorrespondenceSendEvent). Uses Django's test runner's own automatic email backend swap
(locmem) -- django.core.mail.outbox captures every message a test actually "sends"; nothing
here ever reaches a real mailbox (section 20).
"""
from unittest.mock import patch

from django.core import mail
from django.db import IntegrityError

from models.models import User
from project_management.models import ContactUser, Correspondence, CorrespondenceSendEvent
from project_management.services.correspondence_send import (
    SendBlocked,
    resolve_recipient,
    send_correspondence,
)
from project_management.services.correspondence_workflow import (
    WorkflowType,
    can_resend,
    perform_action,
    ready_to_send,
    start_validation,
)
from project_management.test_correspondence_validation_workflow import WorkflowTestBase


class SendTestBase(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.recipient_contact = ContactUser.objects.create(
            contact_company=self.client_company, name="Jane", surname="Doe",
            email="jane.doe@links-local.invalid",
        )

    def approved_out(self, **data):
        corr = self.create_out(recipient_contact=self.recipient_contact.id, **data)
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr


class SendEligibilityTests(SendTestBase):
    def test_in_cannot_send(self):
        corr = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="REF-1",
        )
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)
        self.assertEqual(len(mail.outbox), 0)

    def test_draft_out_without_approved_workflow_cannot_send(self):
        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)
        self.assertEqual(len(mail.outbox), 0)

    def test_pending_validation_out_cannot_send(self):
        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        corr.refresh_from_db()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)

    def test_approved_out_with_no_recipient_email_cannot_send(self):
        corr = self.create_out()  # no recipient_contact at all
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)
        self.assertEqual(len(mail.outbox), 0)

    def test_approved_out_with_valid_recipient_can_send(self):
        corr = self.approved_out()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)

    def test_resolve_recipient_uses_the_contact_email_not_a_company_generic_address(self):
        corr = self.approved_out()
        self.assertEqual(resolve_recipient(corr), {"email": "jane.doe@links-local.invalid", "name": "Jane Doe"})

    def test_issued_with_prior_success_can_be_resent(self):
        # CORR-DIST-2 section 6: superseded LOT 3D-3D2's own "cannot send again" rule -- RESEND
        # is now an approved business rule (same Correspondence, new independent evidence).
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)


class SuccessfulSendTests(SendTestBase):
    def test_send_event_created_with_exact_snapshot(self):
        corr = self.approved_out(subject="Original subject")
        event = send_correspondence(corr, sender=self.owner, subject="Overridden email subject", body="Overridden body")
        self.assertEqual(event.subject, "Overridden email subject")
        self.assertEqual(event.body, "Overridden body")
        self.assertEqual(event.to_recipients, [{"email": "jane.doe@links-local.invalid", "name": "Jane Doe"}])
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertIsNotNone(event.sent_at)

    def test_editing_the_email_subject_never_mutates_the_correspondence(self):
        corr = self.approved_out(subject="Original subject")
        send_correspondence(corr, sender=self.owner, subject="Only for the email")
        corr.refresh_from_db()
        self.assertEqual(corr.subject, "Original subject")

    def test_outbox_receives_exactly_one_message(self):
        corr = self.approved_out()
        mail.outbox = []
        send_correspondence(corr, sender=self.owner)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["jane.doe@links-local.invalid"])

    def test_status_becomes_issued(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_ready_to_send_becomes_false_after_send(self):
        corr = self.approved_out()
        self.assertTrue(ready_to_send(corr))
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertFalse(ready_to_send(corr))

    def test_validation_workflow_status_is_unaffected_by_send(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        from project_management.services.correspondence_workflow import get_latest_workflow

        self.assertEqual(get_latest_workflow(corr).status, "APPROVED")

    def test_issue_date_is_never_overwritten_by_send(self):
        corr = self.approved_out(issue_date="2026-01-15")
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(str(corr.issue_date), "2026-01-15")

    def test_content_stays_locked_after_send(self):
        from project_management.services.correspondence_workflow import is_content_locked

        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertTrue(is_content_locked(corr))

    def test_attachment_snapshot_preserves_id_name_size(self):
        from project_management.models import CorrespondenceAttachment
        from django.core.files.uploadedfile import SimpleUploadedFile

        corr = self.create_out(recipient_contact=self.recipient_contact.id)
        attachment = CorrespondenceAttachment(
            correspondence=corr, file_name="letter.pdf", file_size=1234, uploaded_by=self.owner,
        )
        attachment.file.save("letter.pdf", SimpleUploadedFile("letter.pdf", b"content"), save=False)
        attachment.save()
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(
            event.attachments_snapshot, [{"id": attachment.id, "file_name": "letter.pdf", "file_size": 1234}],
        )


class ReplySendTests(SendTestBase):
    def test_reply_send_answers_the_parent(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="PARENT-1", response_required=True,
        )
        reply = self.create(
            direction="OUT", status="DRAFT", parent_correspondence=parent.id,
            recipient_contact=self.recipient_contact.id, recipient_company=self.client_company.id,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        wf = start_validation(reply, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        reply.refresh_from_db()
        send_correspondence(reply, sender=self.owner)
        reply.refresh_from_db()
        parent.refresh_from_db()
        self.assertEqual(reply.status, Correspondence.Status.ISSUED)
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.ANSWERED)

    def test_standalone_out_requiring_reply_starts_awaiting_a_reply_only_once_sent(self):
        # CORR-STATUS-01 (supersedes LOT 3B's "set at creation"): a reply is awaited only after
        # the send is confirmed -- empty while DRAFT/validating/ready to send.
        corr = self.create_out(recipient_contact=self.recipient_contact.id, response_required=True)
        self.assertEqual(corr.response_status, "")
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, "")
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, Correspondence.ResponseStatus.AWAITING_REPLY)
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)


class FailedSendTests(SendTestBase):
    def test_provider_failure_leaves_correspondence_draft(self):
        corr = self.approved_out()
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.FAILED)
        self.assertIn("SMTP down", event.error_message)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)

    def test_provider_failure_leaves_parent_in_preparation(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="PARENT-2", response_required=True,
        )
        reply = self.create(
            direction="OUT", status="DRAFT", parent_correspondence=parent.id,
            recipient_contact=self.recipient_contact.id, recipient_company=self.client_company.id,
        )
        wf = start_validation(reply, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        reply.refresh_from_db()
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            send_correspondence(reply, sender=self.owner)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

    def test_retry_after_failure_succeeds(self):
        corr = self.approved_out()
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)
        self.assertEqual(corr.send_events.count(), 2)


class ResendTests(SendTestBase):
    """CORR-DIST-2: RESEND -- same Correspondence, a new independent CorrespondenceSendEvent,
    never a second validation workflow, never a mutation of the first SUCCESS evidence."""

    def test_can_resend_is_false_before_any_success(self):
        corr = self.approved_out()
        self.assertFalse(can_resend(corr))

    def test_can_resend_is_true_once_a_prior_success_exists(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertTrue(can_resend(corr))

    def test_resend_creates_a_second_independent_success_event(self):
        corr = self.approved_out()
        first = send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        second = send_correspondence(corr, sender=self.owner, cc=["resend-cc@links-local.invalid"])
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(second.cc_recipients, [{"email": "resend-cc@links-local.invalid"}])

    def test_resend_never_mutates_the_first_success_event(self):
        corr = self.approved_out()
        first = send_correspondence(corr, sender=self.owner)
        first_sent_at = first.sent_at
        corr.refresh_from_db()
        send_correspondence(corr, sender=self.owner)
        first.refresh_from_db()
        self.assertEqual(first.sent_at, first_sent_at)
        self.assertEqual(first.result, CorrespondenceSendEvent.Result.SUCCESS)

    def test_correspondence_remains_issued_after_a_successful_resend(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_resend_does_not_reopen_or_require_a_new_validation_workflow(self):
        from project_management.services.correspondence_workflow import get_latest_workflow

        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        workflow_count_before = CorrespondenceSendEvent.objects.filter(correspondence=corr).count()
        send_correspondence(corr, sender=self.owner)
        self.assertEqual(get_latest_workflow(corr).status, "APPROVED")
        # Exactly one new send event, no new workflow row of any kind was touched.
        self.assertEqual(
            CorrespondenceSendEvent.objects.filter(correspondence=corr).count(),
            workflow_count_before + 1,
        )

    def test_failed_resend_creates_a_failed_event_without_altering_the_prior_success(self):
        corr = self.approved_out()
        first = send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            second = send_correspondence(corr, sender=self.owner)
        self.assertEqual(second.result, CorrespondenceSendEvent.Result.FAILED)
        first.refresh_from_db()
        self.assertEqual(first.result, CorrespondenceSendEvent.Result.SUCCESS)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_retry_after_a_failed_resend_can_succeed(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            send_correspondence(corr, sender=self.owner)
        third = send_correspondence(corr, sender=self.owner)
        self.assertEqual(third.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(corr.send_events.count(), 3)
        self.assertEqual(
            corr.send_events.filter(result=CorrespondenceSendEvent.Result.SUCCESS).count(), 2,
        )

    def test_a_resend_in_flight_still_blocks_a_concurrent_attempt(self):
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        CorrespondenceSendEvent.objects.create(correspondence=corr, result=CorrespondenceSendEvent.Result.IN_PROGRESS)
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)


class ScopeTests(SendTestBase):
    """CORR-DIST-2 section 24/25: the exact same send/resend service works for Company,
    Project, and freelancer owner scope -- no scope-specific send service/model exists."""

    def test_company_scoped_correspondence_can_send_and_resend(self):
        corr = self.approved_out()  # company-scoped by default (WorkflowTestBase.create_out)
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(corr.send_events.count(), 2)

    def test_project_scoped_correspondence_can_send_and_resend(self):
        corr = self.create_out_project(recipient_contact=self.recipient_contact.id)
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        self.assertEqual(corr.send_events.count(), 2)

    def test_freelancer_scoped_correspondence_send_is_not_broken(self):
        # Constructed directly (never through the serializer's company/project scope
        # resolution) -- this proves only that the SEND SERVICE itself has no company/project
        # assumption baked in, exactly section 25's "do not break it, no dedicated UX required".
        corr = Correspondence.objects.create(
            freelancer=self.owner, direction="OUT", status="DRAFT",
            subject="Freelancer subject", recipient_contact=self.recipient_contact,
        )
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.owner, None, self.owner, started_by=self.owner)
        perform_action(wf, self.owner, "APPROVE", "")
        corr.refresh_from_db()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.SUCCESS)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)


class IdempotencyTests(SendTestBase):
    def test_a_second_send_while_one_is_in_flight_is_blocked(self):
        corr = self.approved_out()
        CorrespondenceSendEvent.objects.create(correspondence=corr, result=CorrespondenceSendEvent.Result.IN_PROGRESS)
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)

    def test_the_database_constraint_itself_rejects_a_second_in_flight_row(self):
        corr = self.approved_out()
        CorrespondenceSendEvent.objects.create(correspondence=corr, result=CorrespondenceSendEvent.Result.IN_PROGRESS)
        with self.assertRaises(IntegrityError):
            CorrespondenceSendEvent.objects.create(correspondence=corr, result=CorrespondenceSendEvent.Result.IN_PROGRESS)

    def test_double_send_after_success_is_now_a_resend_not_blocked(self):
        # CORR-DIST-2 section 6 (renamed/rewritten -- see test_issued_with_prior_success_can_be_resent
        # for the dedicated resend test; this one specifically pins the resulting event COUNT).
        corr = self.approved_out()
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        send_correspondence(corr, sender=self.owner)
        self.assertEqual(corr.send_events.filter(result=CorrespondenceSendEvent.Result.SUCCESS).count(), 2)


class SecurityGuardTests(SendTestBase):
    def test_foreign_attachment_is_rejected(self):
        from project_management.models import CorrespondenceAttachment
        from django.core.files.uploadedfile import SimpleUploadedFile

        corr = self.approved_out()
        other_corr = self.create_out(recipient_contact=self.recipient_contact.id)
        foreign = CorrespondenceAttachment(
            correspondence=other_corr, file_name="other.pdf", uploaded_by=self.owner,
        )
        foreign.file.save("other.pdf", SimpleUploadedFile("other.pdf", b"x"), save=False)
        foreign.save()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner, attachment_ids=[foreign.id])

    def test_invalid_cc_email_is_rejected(self):
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner, cc=["not-an-email"])

    def test_invalid_bcc_email_is_rejected(self):
        corr = self.approved_out()
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner, bcc=["also not valid"])

    def test_valid_cc_and_bcc_are_accepted_and_recorded(self):
        corr = self.approved_out()
        event = send_correspondence(
            corr, sender=self.owner, cc=["cc@links-local.invalid"], bcc=["bcc@links-local.invalid"],
        )
        self.assertEqual(event.cc_recipients, [{"email": "cc@links-local.invalid"}])
        self.assertEqual(event.bcc_recipients, [{"email": "bcc@links-local.invalid"}])


class EventImmutabilityTests(SendTestBase):
    def test_a_success_event_cannot_be_modified_directly(self):
        corr = self.approved_out()
        event = send_correspondence(corr, sender=self.owner)
        event.error_message = "tampered"
        with self.assertRaises(Exception):
            event.save()

    def test_a_success_event_cannot_be_deleted(self):
        corr = self.approved_out()
        event = send_correspondence(corr, sender=self.owner)
        with self.assertRaises(Exception):
            event.delete()
