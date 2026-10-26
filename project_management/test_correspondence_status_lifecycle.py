# CORR-STATUS-01: the OUT lifecycle / response-status rules.
#   response_required OUT : DRAFT -> validation -> ready to send : response status EMPTY
#                           confirmed send -> ISSUED ("Sent") + AWAITING_REPLY
#                           linked IN reply -> ANSWERED
#   response not required : NOT_REQUIRED throughout (never AWAITING_REPLY)
#   failed / uncertain send: nothing moves, no automatic retry.
# Nothing here reaches a real mailbox (Django's locmem backend / patched send).
from unittest.mock import patch

from django.core import mail

from project_management.models import ContactUser, Correspondence, CorrespondenceSendEvent
from project_management.services.correspondence_response import (
    NO_RESPONSE_STATUS,
    recompute_parent_response_status_from_responses,
)
from project_management.services.correspondence_send import SendBlocked, send_correspondence
from project_management.services.correspondence_workflow import (
    WorkflowType,
    perform_action,
    ready_to_send,
    start_validation,
    validation_state,
)
from project_management.test_correspondence_validation_workflow import WorkflowTestBase

RS = Correspondence.ResponseStatus


class LifecycleBase(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.recipient = ContactUser.objects.create(
            contact_company=self.client_company, name="Jane", surname="Doe",
            email="jane.doe@links-local.invalid",
        )

    def out(self, **data):
        return self.create_out(recipient_contact=self.recipient.id, **data)

    def approve(self, corr):
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr


class BeforeSendTests(LifecycleBase):
    def test_draft_out_requiring_a_reply_has_no_response_status(self):
        corr = self.out(response_required=True)
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)
        self.assertNotEqual(corr.response_status, RS.AWAITING_REPLY)

    def test_validation_in_progress_keeps_it_empty(self):
        corr = self.out(response_required=True)
        start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)

    def test_validated_not_sent_is_ready_to_send_with_an_empty_response_status(self):
        corr = self.approve(self.out(response_required=True))
        self.assertTrue(ready_to_send(corr))
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)

    def test_the_simple_signature_workflow_does_not_start_the_wait_either(self):
        corr = self.out(response_required=True)
        wf = start_validation(corr, WorkflowType.SIGN_AUTHOR, None, None, None, started_by=self.author)
        perform_action(wf, self.author, "APPROVE", "")
        corr.refresh_from_db()
        self.assertTrue(ready_to_send(corr))
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)
        self.assertEqual(len(mail.outbox), 0)

    def test_no_reply_required_stays_not_required_and_never_awaits(self):
        corr = self.out(response_required=False)
        self.assertEqual(corr.response_status, RS.NOT_REQUIRED)
        corr = self.approve(corr)
        self.assertEqual(corr.response_status, RS.NOT_REQUIRED)

    def test_switching_the_requirement_on_a_draft_keeps_the_rule(self):
        corr = self.out(response_required=False)
        corr = self.update(corr, response_required=True)
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)
        corr = self.update(corr, response_required=False)
        self.assertEqual(corr.response_status, RS.NOT_REQUIRED)

    def test_an_incoming_correspondence_is_unchanged(self):
        corr = self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-IN",
            response_required=True,
        )
        self.assertEqual(corr.response_status, RS.TO_ANSWER)


class AfterSendTests(LifecycleBase):
    def test_confirmed_send_makes_it_issued_and_awaiting_a_reply(self):
        corr = self.approve(self.out(response_required=True))
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)
        self.assertEqual(corr.response_status, RS.AWAITING_REPLY)
        self.assertFalse(ready_to_send(corr))
        self.assertEqual(len(mail.outbox), 1)

    def test_confirmed_send_without_required_reply_is_sent_and_not_required(self):
        corr = self.approve(self.out(response_required=False))
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)
        self.assertEqual(corr.response_status, RS.NOT_REQUIRED)

    def test_a_resend_does_not_rewrite_the_response_status(self):
        corr = self.approve(self.out(response_required=True))
        send_correspondence(corr, sender=self.owner)
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, RS.AWAITING_REPLY)

    def test_a_linked_incoming_reply_answers_the_sent_out(self):
        corr = self.approve(self.out(response_required=True))
        send_correspondence(corr, sender=self.owner)
        self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-REPLY",
            parent_correspondence=corr.id,
        )
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, RS.ANSWERED)
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)

    def test_a_reply_linked_before_the_send_makes_it_answered_not_awaiting(self):
        corr = self.approve(self.out(response_required=True))
        self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-EARLY",
            parent_correspondence=corr.id,
        )
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)  # nothing was sent yet
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.response_status, RS.ANSWERED)

    def test_deleting_the_reply_returns_to_awaiting_for_a_sent_out_only(self):
        sent = self.approve(self.out(response_required=True))
        send_correspondence(sent, sender=self.owner)
        reply = self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-DEL",
            parent_correspondence=sent.id,
        )
        reply.delete()
        sent.refresh_from_db()
        recompute_parent_response_status_from_responses(sent)
        sent.refresh_from_db()
        self.assertEqual(sent.response_status, RS.AWAITING_REPLY)
        # A draft OUT never "returns" to awaiting: its starting state is empty.
        draft = self.out(response_required=True)
        recompute_parent_response_status_from_responses(draft)
        draft.refresh_from_db()
        self.assertEqual(draft.response_status, NO_RESPONSE_STATUS)

    def test_recording_an_out_directly_as_issued_awaits_a_reply_immediately(self):
        corr = self.create(
            direction="OUT", status="ISSUED", recipient_contact=self.recipient.id,
            response_required=True,
        )
        self.assertEqual(corr.response_status, RS.AWAITING_REPLY)

    def test_manually_issuing_a_draft_out_starts_the_wait(self):
        corr = self.out(response_required=True)
        corr = self.update(corr, status="ISSUED")
        self.assertEqual(corr.response_status, RS.AWAITING_REPLY)


class OverdueRulesTests(LifecycleBase):
    """CORR-STATUS-02: "Overdue" is derived (never stored) from a PENDING response status plus a
    elapsed response_due_date, so it can only exist once an OUT has really been sent."""

    PAST = "2020-01-01"

    def effective(self, corr):
        corr.refresh_from_db()
        return corr.effective_response_status

    def test_a_draft_with_a_past_due_date_is_never_overdue(self):
        corr = self.out(response_required=True, response_due_date=self.PAST)
        self.assertEqual(self.effective(corr), NO_RESPONSE_STATUS)
        self.assertFalse(corr.is_overdue)

    def test_validated_ready_to_send_with_a_past_due_date_is_never_overdue(self):
        corr = self.approve(self.out(response_required=True, response_due_date=self.PAST))
        self.assertTrue(ready_to_send(corr))
        self.assertEqual(self.effective(corr), NO_RESPONSE_STATUS)

    def test_a_failed_or_uncertain_send_activates_no_deadline(self):
        corr = self.approve(self.out(response_required=True, response_due_date=self.PAST))
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            send_correspondence(corr, sender=self.owner)
        self.assertEqual(self.effective(corr), NO_RESPONSE_STATUS)
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, from_email="x@links-local.invalid",
            provider="graph", result=CorrespondenceSendEvent.Result.FAILED,
            error_message="UNCERTAIN DELIVERY: the outcome could not be confirmed.",
        )
        self.assertEqual(self.effective(corr), NO_RESPONSE_STATUS)

    def test_a_sent_out_becomes_overdue_only_after_a_really_elapsed_due_date(self):
        future = self.approve(self.out(response_required=True, response_due_date="2999-01-01"))
        send_correspondence(future, sender=self.owner)
        self.assertEqual(self.effective(future), RS.AWAITING_REPLY)
        late = self.approve(self.out(response_required=True, response_due_date=self.PAST))
        send_correspondence(late, sender=self.owner)
        self.assertEqual(self.effective(late), RS.OVERDUE)
        # OVERDUE is derived only: the stored value stays AWAITING_REPLY.
        late.refresh_from_db()
        self.assertEqual(late.response_status, RS.AWAITING_REPLY)

    def test_an_answered_out_is_not_overdue_even_with_a_past_due_date(self):
        corr = self.approve(self.out(response_required=True, response_due_date=self.PAST))
        send_correspondence(corr, sender=self.owner)
        self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-LATE",
            parent_correspondence=corr.id,
        )
        self.assertEqual(self.effective(corr), RS.ANSWERED)

    def test_no_reply_required_is_never_overdue(self):
        corr = self.approve(self.out(response_required=False, response_due_date=self.PAST))
        send_correspondence(corr, sender=self.owner)
        self.assertEqual(self.effective(corr), RS.NOT_REQUIRED)


class FailedAndUncertainSendTests(LifecycleBase):
    def test_a_failed_send_moves_nothing(self):
        corr = self.approve(self.out(response_required=True))
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            event = send_correspondence(corr, sender=self.owner)
        self.assertEqual(event.result, CorrespondenceSendEvent.Result.FAILED)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)

    def test_a_retry_after_a_certain_failure_then_succeeds_normally(self):
        corr = self.approve(self.out(response_required=True))
        with patch("django.core.mail.message.EmailMessage.send", side_effect=Exception("SMTP down")):
            send_correspondence(corr, sender=self.owner)
        send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.ISSUED)
        self.assertEqual(corr.response_status, RS.AWAITING_REPLY)

    def test_an_uncertain_send_stays_blocked_with_no_transition_and_no_automatic_retry(self):
        corr = self.approve(self.out(response_required=True))
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, from_email="x@links-local.invalid",
            to_recipients=[{"email": "jane.doe@links-local.invalid"}], provider="graph",
            result=CorrespondenceSendEvent.Result.FAILED,
            error_message="UNCERTAIN DELIVERY: the outcome could not be confirmed.",
        )
        with self.assertRaises(SendBlocked):
            send_correspondence(corr, sender=self.owner)
        corr.refresh_from_db()
        self.assertEqual(corr.status, Correspondence.Status.DRAFT)
        self.assertEqual(corr.response_status, NO_RESPONSE_STATUS)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(CorrespondenceSendEvent.objects.filter(correspondence=corr).count(), 1)
