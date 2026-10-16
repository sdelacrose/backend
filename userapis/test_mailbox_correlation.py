# MAIL-1.3C: identifying the messages LINKS sent (marker header + Sent Items copy) and correlating
# incoming replies to them. Microsoft Graph and token renewal are always simulated; every address,
# subject and id is fictitious. No correspondence is ever created by any of this.
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import override_settings
from django.utils import timezone

from project_management.models import Correspondence, CorrespondenceSendEvent
from project_management.services.correspondence_send import SendBlocked, send_correspondence
from project_management.test_correspondence_send_graph import GraphSendTestBase
from userapis.mailbox_correlation import (
    MATCH_AMBIGUOUS,
    MATCH_CONFIRMED,
    NO_MATCH,
    build_send_marker,
    correlate_reply,
    identify_sent_message,
    record_sent_copy,
    verify_send_marker,
)
from userapis.mailbox_messages import store_message_metadata
from userapis.mailbox_read import MailboxReadError, ReplyIdentification, normalize_message
from userapis.models import MailboxMessage, MailboxProjectAssignment, MailboxUserGrant
from userapis.test_mailbox_read import FakeGraph, raw_message

Result = CorrespondenceSendEvent.Result


def marker_of(mock_post, base):
    call = base._graph_call(mock_post)
    headers = call.kwargs["json"]["message"].get("internetMessageHeaders")
    return headers[0]["value"] if headers else None


class SendMarkerTests(GraphSendTestBase):
    def test_a_graph_send_carries_one_unforgeable_marker_and_nothing_revealing(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        call = self._graph_call(mock_post)
        message = call.kwargs["json"]["message"]
        self.assertEqual(len(message["internetMessageHeaders"]), 1)
        header = message["internetMessageHeaders"][0]
        self.assertEqual(header["name"], "x-links-send")  # Graph accepts only x- custom headers
        self.assertTrue(verify_send_marker(header["value"], event))
        self.assertNotIn(corr.reference, header["value"])
        self.assertNotIn(event.subject, header["value"])
        # forged / copied-to-another-send markers never verify
        other = self.approved_out(subject="Another one")
        other_event, _, _ = self.send_via_graph(other, self.individual_mailbox)
        self.assertFalse(verify_send_marker(header["value"], other_event))
        self.assertFalse(verify_send_marker(header["value"] + "0", event))
        self.assertFalse(verify_send_marker("", event))
        self.assertEqual(build_send_marker(event.pk, corr.pk), header["value"])

    def test_the_rest_of_the_graph_request_is_unchanged(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        message = self._graph_call(mock_post).kwargs["json"]["message"]
        self.assertEqual(
            set(message), {"subject", "body", "toRecipients", "internetMessageHeaders"}
        )
        self.assertEqual(self._graph_call(mock_post).kwargs["json"]["saveToSentItems"], "true")
        self.assertEqual(event.provider_message_id, "")

    def test_the_marker_can_be_switched_off_leaving_the_original_request(self):
        corr = self.approved_out()
        with override_settings(MS_SEND_CORRELATION_HEADER=False):
            event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(event.result, Result.SUCCESS)
        self.assertNotIn("internetMessageHeaders", self._graph_call(mock_post).kwargs["json"]["message"])

    def test_smtp_sends_carry_no_marker_and_are_unchanged(self):
        from django.core import mail
        corr = self.approved_out()
        event = send_correspondence(corr, sender=self.owner)
        self.assertEqual((event.provider, event.result), ("smtp", Result.SUCCESS))
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("x-links-send", {k.lower() for k in mail.outbox[0].extra_headers})

    def test_a_failed_send_stores_nothing_and_a_retry_still_works(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(corr, self.individual_mailbox, mock_kwargs={"graph_status": 400})
        self.assertEqual(event.result, Result.FAILED)
        self.assertEqual(MailboxMessage.objects.count(), 0)
        retry, _, _ = self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(retry.result, Result.SUCCESS)

    def test_an_uncertain_send_is_still_blocked_and_double_sends_still_refused(self):
        corr = self.approved_out()
        event, _, _ = self.send_via_graph(
            corr, self.individual_mailbox,
            mock_kwargs={"graph_raises": requests.exceptions.ReadTimeout("slow")},
        )
        self.assertEqual(event.result, Result.FAILED)
        self.assertTrue(event.error_message.startswith("UNCERTAIN DELIVERY"))
        with self.assertRaises(SendBlocked):
            self.send_via_graph(corr, self.individual_mailbox)
        self.assertEqual(corr.send_events.count(), 1)
        # an in-flight attempt still blocks a second one
        CorrespondenceSendEvent.objects.filter(pk=event.pk).delete()
        CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, result=Result.IN_PROGRESS, provider="graph",
        )
        with self.assertRaises(SendBlocked):
            self.send_via_graph(corr, self.individual_mailbox)


class SentCopyIdentificationTests(GraphSendTestBase):
    def setUp(self):
        super().setUp()
        self.graph = FakeGraph(me_address="owner@links-local.invalid")
        for target, kwargs in (
            ("userapis.mailbox_read.requests.get", {"side_effect": self.graph}),
            ("userapis.mailbox_read.refresh_outlook_access_token", {"return_value": "TOKEN"}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sent(self):
        corr = self.approved_out()
        event, mock_post, _ = self.send_via_graph(corr, self.individual_mailbox)
        return corr, event, marker_of(mock_post, self)

    def simulate_sent_items(self, event, marker, *, subject=None, internet_id="<sent1@links-local.invalid>"):
        self.graph.list_payload = {"value": [raw_message(
            1, id="AAMk-sent-1", subject=subject or event.subject, internetMessageId=internet_id,
            conversationId="conv-sent-1",
            toRecipients=[{"emailAddress": {"address": "jane.doe@links-local.invalid"}}],
        )]}
        self.graph.item_payload = {
            "internetMessageId": internet_id, "conversationId": "conv-sent-1",
            "internetMessageHeaders": [
                {"name": "Message-ID", "value": internet_id},
                {"name": "x-links-send", "value": marker},
            ],
        }

    def test_the_sent_copy_is_found_by_its_marker_and_its_ids_are_kept(self):
        corr, event, marker = self.sent()
        self.simulate_sent_items(event, marker)
        row = identify_sent_message(event, actor=self.owner)
        self.assertEqual(row.correspondence_id, corr.pk)
        self.assertEqual(row.mailbox_id, self.individual_mailbox.id)
        self.assertEqual(row.internet_message_id, "<sent1@links-local.invalid>")
        self.assertEqual(row.conversation_id, "conv-sent-1")
        self.assertEqual(row.graph_message_id, "AAMk-sent-1")
        self.assertEqual(row.folder, "sentitems")
        # idempotent: a second identification adds nothing
        again = identify_sent_message(event, actor=self.owner)
        self.assertEqual(again.pk, row.pk)
        self.assertEqual(MailboxMessage.objects.count(), 1)

    def test_a_look_alike_without_the_right_marker_is_never_recorded(self):
        corr, event, marker = self.sent()
        self.simulate_sent_items(event, "999.deadbeefdeadbeefdead")
        self.assertIsNone(identify_sent_message(event, actor=self.owner))
        self.simulate_sent_items(event, marker, subject="A different subject")
        self.assertIsNone(identify_sent_message(event, actor=self.owner))
        self.assertEqual(MailboxMessage.objects.count(), 0)

    def test_a_copy_cannot_be_linked_to_another_correspondence_or_failed_send(self):
        corr, event, marker = self.sent()
        other = self.approved_out(subject="Second")
        other_event, _, _ = self.send_via_graph(other, self.individual_mailbox)
        meta = normalize_message(raw_message(1, internetMessageId="<sent1@links-local.invalid>"), "sentitems")
        ident = ReplyIdentification(send_marker=marker)
        self.assertIsNone(record_sent_copy(other_event, meta, ident))  # marker is event 1's
        row = record_sent_copy(event, meta, ident)
        self.assertEqual(row.correspondence_id, corr.pk)
        failed = CorrespondenceSendEvent.objects.create(
            correspondence=corr, sender=self.owner, result=Result.FAILED, provider="graph",
            mailbox=self.individual_mailbox,
        )
        self.assertIsNone(record_sent_copy(failed, meta, ReplyIdentification(send_marker=build_send_marker(failed.pk, corr.pk))))

    def test_the_lookup_asks_graph_only_for_this_send_in_sent_items(self):
        corr, event, marker = self.sent()
        self.simulate_sent_items(event, marker)
        identify_sent_message(event, actor=self.owner)
        listing = [c for c in self.graph.message_calls if "/mailFolders/" in c["url"]]
        self.assertEqual(len(listing), 1)
        self.assertTrue(listing[0]["url"].endswith("/mailFolders/sentitems/messages"))
        flt = listing[0]["params"]["$filter"]
        self.assertTrue(flt.startswith("receivedDateTime ge "))
        self.assertIn(" and receivedDateTime le ", flt)
        self.assertIn(f"subject eq '{event.subject}'", flt)
        self.assertLessEqual(listing[0]["params"]["$top"], 10)
        # never Inbox, never a body or attachment
        self.assertNotIn("inbox", " ".join(c["url"] for c in self.graph.message_calls))
        self.assertNotIn("body", listing[0]["params"]["$select"].lower().replace("hasattachments", ""))

    def test_a_conversation_filter_is_exact_escaped_and_combined_with_the_window(self):
        from userapis.mailbox_read import MailboxGraphReader
        reader = MailboxGraphReader(self.individual_mailbox, actor=self.owner)
        reader.list_messages(
            "sentitems", received_since=timezone.now() - timedelta(hours=1),
            received_until=timezone.now(), conversation_id="conv'x",
        )
        flt = self.graph.message_calls[-1]["params"]["$filter"]
        self.assertTrue(flt.startswith("receivedDateTime ge "))
        self.assertIn(" and receivedDateTime le ", flt)
        self.assertTrue(flt.endswith("and conversationId eq 'conv''x'"))

    def test_a_quote_in_the_subject_is_escaped_in_the_filter(self):
        from userapis.mailbox_read import MailboxGraphReader
        reader = MailboxGraphReader(self.individual_mailbox, actor=self.owner)
        reader.list_messages("sentitems", subject="It's a test")
        flt = self.graph.message_calls[-1]["params"]["$filter"]
        self.assertEqual(flt, "subject eq 'It''s a test'")

    def test_only_someone_who_can_read_the_mailbox_can_identify(self):
        corr, event, marker = self.sent()
        self.simulate_sent_items(event, marker)
        with self.assertRaises(MailboxReadError):
            identify_sent_message(event, actor=self.approver)
        self.assertEqual(MailboxMessage.objects.count(), 0)


class ReplyCorrelationTests(GraphSendTestBase):
    """Correlation works on stored sent copies only: no Graph, no send."""

    def sent_copy(self, corr, mailbox=None, internet_id="<sent-1@links-local.invalid>",
                  conversation="conv-1", n=1):
        mailbox = mailbox or self.individual_mailbox
        meta = normalize_message(raw_message(
            n, id=f"AAMk-sent-{n}-{corr.pk}", internetMessageId=internet_id, conversationId=conversation,
        ), "sentitems")
        row, _ = store_message_metadata(mailbox, meta)
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=corr)
        return row

    def correlate(self, mailbox=None, actor=None, **kw):
        return correlate_reply(mailbox or self.individual_mailbox, actor=actor or self.owner, **kw)

    def test_in_reply_to_confirms_the_match(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        result = self.correlate(in_reply_to="<sent-1@links-local.invalid>")
        self.assertEqual((result.result, result.correspondence_id), (MATCH_CONFIRMED, corr.pk))
        self.assertEqual(result.evidence, ("in_reply_to",))

    def test_references_confirm_a_deeper_reply_in_the_chain(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        result = self.correlate(
            in_reply_to="<someone-elses-reply@example.com>",
            references=("<root@example.com>", "<sent-1@links-local.invalid>", "<someone-elses-reply@example.com>"),
        )
        self.assertEqual((result.result, result.correspondence_id), (MATCH_CONFIRMED, corr.pk))
        self.assertEqual(result.evidence, ("references",))

    def test_evidence_pointing_to_two_correspondences_is_ambiguous(self):
        a, b = self.approved_out(), self.approved_out(subject="Other")
        self.sent_copy(a, internet_id="<sent-a@links-local.invalid>", n=1)
        self.sent_copy(b, internet_id="<sent-b@links-local.invalid>", n=2)
        result = self.correlate(references=("<sent-a@links-local.invalid>", "<sent-b@links-local.invalid>"))
        self.assertEqual(result.result, MATCH_AMBIGUOUS)
        self.assertIsNone(result.correspondence_id)
        self.assertEqual(set(result.candidate_ids), {a.pk, b.pk})

    def test_a_conversation_id_alone_is_never_proof(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        result = self.correlate(conversation_id="conv-1")
        self.assertEqual(result.result, MATCH_AMBIGUOUS)
        self.assertEqual(result.candidate_ids, (corr.pk,))
        self.assertIsNone(result.correspondence_id)

    def test_a_conversation_corroborated_by_the_links_reference_confirms(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        result = self.correlate(conversation_id="conv-1", links_reference=corr.reference.lower())
        self.assertEqual((result.result, result.correspondence_id), (MATCH_CONFIRMED, corr.pk))
        self.assertEqual(result.evidence, ("conversation", "links_reference"))
        # the reference alone, or a reference contradicting the conversation, is not enough
        self.assertEqual(self.correlate(links_reference=corr.reference).result, MATCH_AMBIGUOUS)
        other = self.approved_out(subject="Other")
        self.assertEqual(
            self.correlate(conversation_id="conv-1", links_reference=other.reference).result, MATCH_AMBIGUOUS
        )

    def test_a_similar_subject_never_attaches_anything(self):
        corr = self.approved_out(subject="Contract review")
        self.sent_copy(corr)
        # nothing but a subject-like text is known about the incoming message
        self.assertEqual(self.correlate().result, NO_MATCH)
        self.assertEqual(self.correlate(in_reply_to="<unknown@example.com>").result, NO_MATCH)

    def test_the_repliers_role_never_matters_and_missing_headers_never_force_a_match(self):
        import inspect
        # correlate_reply takes no sender / To / CC argument: a replier who was only in CC of the
        # original is neither rejected nor favoured -- the proof is the headers alone.
        parameters = set(inspect.signature(correlate_reply).parameters)
        self.assertFalse(parameters & {"sender", "from_address", "to", "cc", "recipients"})
        corr = self.approved_out()
        self.sent_copy(corr)
        # a message of the same conversation whose headers Graph did not return (e.g. a copy
        # saved in Sent Items by Outlook): only the conversation hint remains -> AMBIGUOUS
        result = self.correlate(
            conversation_id="conv-1", in_reply_to="", references=(),
            message_internet_id="<reply-from-outlook@links-local.invalid>",
        )
        self.assertEqual(result.result, MATCH_AMBIGUOUS)
        self.assertIsNone(result.correspondence_id)
        # the same message with its real headers is confirmed whoever sent it
        proven = self.correlate(
            conversation_id="conv-1", in_reply_to="<sent-1@links-local.invalid>",
            message_internet_id="<reply-from-outlook@links-local.invalid>",
        )
        self.assertEqual((proven.result, proven.correspondence_id), (MATCH_CONFIRMED, corr.pk))

    def test_an_outgoing_message_from_sent_items_is_never_correlated_as_an_incoming_reply(self):
        from userapis.mailbox_correlation import NOT_APPLICABLE_OUTGOING_REPLY
        corr = self.approved_out()
        self.sent_copy(corr)
        # even with perfect header proof, a message read from Sent Items is the mailbox's own
        # outgoing message: not applicable, never confirmed nor ambiguous
        for kwargs in (
            {"in_reply_to": "<sent-1@links-local.invalid>"},
            {"conversation_id": "conv-1"},
            {},
        ):
            result = self.correlate(message_folder="sentitems", **kwargs)
            self.assertEqual(result.result, NOT_APPLICABLE_OUTGOING_REPLY)
            self.assertIsNone(result.correspondence_id)
            self.assertEqual(result.candidate_ids, ())
        # the same evidence for an incoming message (Inbox) still correlates as before
        self.assertEqual(
            self.correlate(message_folder="inbox", in_reply_to="<sent-1@links-local.invalid>").result,
            MATCH_CONFIRMED,
        )
        # authorization is still checked first
        with self.assertRaises(MailboxReadError):
            self.correlate(actor=self.approver, message_folder="sentitems")

    def test_a_stored_incoming_reply_linked_to_a_correspondence_is_not_a_sent_copy_candidate(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        # the reply itself, stored in Inbox and linked to the correspondence
        reply = normalize_message(raw_message(7, id="AAMk-reply", internetMessageId="<reply-7@x.test>"), "inbox")
        row, _ = store_message_metadata(self.individual_mailbox, reply)
        MailboxMessage.objects.filter(pk=row.pk).update(correspondence=corr)
        # a later message that answers the reply, not LINKS' own sent message
        self.assertEqual(self.correlate(in_reply_to="<reply-7@x.test>").result, NO_MATCH)
        self.assertEqual(
            self.correlate(in_reply_to="<sent-1@links-local.invalid>").result, MATCH_CONFIRMED
        )

    def test_the_message_never_matches_itself(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        result = self.correlate(
            in_reply_to="<sent-1@links-local.invalid>", message_internet_id="<sent-1@links-local.invalid>"
        )
        self.assertEqual(result.result, NO_MATCH)

    def test_no_leak_across_mailboxes_or_companies(self):
        corr = self.approved_out()
        self.sent_copy(corr, mailbox=self.shared_mailbox)
        # the evidence names a message sent from ANOTHER mailbox: not a candidate here
        self.assertEqual(self.correlate(in_reply_to="<sent-1@links-local.invalid>").result, NO_MATCH)
        self.assertEqual(
            self.correlate(mailbox=self.shared_mailbox, in_reply_to="<sent-1@links-local.invalid>").result,
            MATCH_CONFIRMED,
        )
        # a correspondence of another company is never a candidate, even if wrongly linked
        from models.models import CompanyProfile
        foreign_admin = self._company_member("foreignadmin")
        foreign_company = CompanyProfile.objects.create(
            admin_user=foreign_admin, company_name="Foreign Co", company_address="1", post_code="0",
            company_town="T", company_country="C", company_tell_no="0", company_mob_no="1",
            company_web_address="http://f", company_email_address="f@f.invalid",
            company_tax_reference="F", company_iban="F",
        )
        foreign = Correspondence.objects.create(
            direction="OUT", subject="Foreign", company_profile=foreign_company, status="ISSUED",
            reference="FOREIGN-COR-1",
        )
        row = self.sent_copy(foreign, internet_id="<foreign@links-local.invalid>", n=3, conversation="conv-f")
        self.assertEqual(self.correlate(in_reply_to="<foreign@links-local.invalid>").result, NO_MATCH)
        self.assertEqual(
            self.correlate(conversation_id="conv-f", links_reference="FOREIGN-COR-1").result, NO_MATCH
        )
        self.assertIsNotNone(row)

    def test_a_project_correspondence_needs_the_mailbox_assigned_to_its_project(self):
        corr = self.approved_project_out()
        self.sent_copy(corr)
        self.assertEqual(self.correlate(in_reply_to="<sent-1@links-local.invalid>").result, NO_MATCH)
        MailboxProjectAssignment.objects.create(mailbox=self.individual_mailbox, project=self.project)
        self.assertEqual(
            self.correlate(in_reply_to="<sent-1@links-local.invalid>").result, MATCH_CONFIRMED
        )

    def test_an_incoming_correspondence_is_never_a_candidate(self):
        incoming = self.create(
            direction="IN", sender_company=self.client_company.id, external_reference="REF-IN",
        )
        self.sent_copy(incoming)
        self.assertEqual(self.correlate(in_reply_to="<sent-1@links-local.invalid>").result, NO_MATCH)

    def test_reading_rights_are_required_and_nothing_is_created_or_imported(self):
        corr = self.approved_out()
        self.sent_copy(corr)
        before = (Correspondence.objects.count(), MailboxMessage.objects.count())
        with self.assertRaises(MailboxReadError):
            self.correlate(actor=self.approver, in_reply_to="<sent-1@links-local.invalid>")
        # a shared-mailbox sender without can_read cannot correlate either
        MailboxUserGrant.objects.filter(mailbox=self.shared_mailbox, user=self.owner).update(can_read=False)
        with self.assertRaises(MailboxReadError):
            self.correlate(mailbox=self.shared_mailbox, in_reply_to="<x@y.z>")
        self.correlate(in_reply_to="<sent-1@links-local.invalid>")
        self.assertEqual((Correspondence.objects.count(), MailboxMessage.objects.count()), before)

    def approved_project_out(self, **data):
        corr = self.create_out_project(recipient_contact=self.recipient_contact.id, **data)
        from project_management.services.correspondence_workflow import (
            WorkflowType, perform_action, start_validation,
        )
        wf = start_validation(corr, WorkflowType.ONE_STEP, self.author, None, self.approver, started_by=self.author)
        perform_action(wf, self.approver, "APPROVE", "")
        corr.refresh_from_db()
        return corr
