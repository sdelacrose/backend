"""SURVEILLANCE IN LOT C: fewer manual steps. A unique, unambiguous categorised return is recorded AND retained in the same action
(individual form, grouped proof of Lot B, received IN transmittal); anything ambiguous keeps the explicit confirmation; the
automatic confirmation is traced; a return received the same day as the send is accepted."""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from project_management.models import (
    ClientIssue, ClientSurveillanceEvent, ClientSurveillanceFollowUp, ClientSurveillanceReturn, Document, TransmittalEvent,
)
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.client_surveillance_return import record_client_return
from project_management.services.correspondence_client_returns import default_received_at
from project_management.services.transmittal_client_return import qualify_client_return_line
from project_management.test_client_surveillance_return import pdf
from project_management.test_client_surveillance_return import ReturnBase
from project_management.test_client_proof_selector import ProofBase
from project_management.test_client_surveillance_lot2 import Lot2Base as Lot2FollowBase
from project_management.test_correspondence_client_returns import Lot2Base as ProofApiBase
from project_management.test_transmittal_client_return import ClientInBase

Category = ClientSurveillanceReturn.Category


def followups(issue):
    return ClientSurveillanceFollowUp.objects.filter(client_return__client_issue=issue)


class IndividualTests(ProofApiBase):
    def test_a_unique_return_is_recorded_and_retained_in_one_action_with_its_next_step(self):  # 1, 3
        response = self.post(self.user, {"category": "ACCEPTED"})
        self.assertEqual(response.status_code, 201, response.data)
        retention = response.data["retention"]
        self.assertTrue(retention["retained"] and retention["automatic"])
        self.assertEqual(retention["followup"]["next_step"], "EN_ATTENTE_FICHIER_BPE")  # the next documentary action is shown at once
        followup = followups(self.client_issue).get()
        self.assertEqual(followup.client_return_id, response.data["id"])

    def test_the_automatic_confirmation_is_traced_explicitly(self):
        self.post(self.user, {"category": "ACCEPTED"})
        event = ClientSurveillanceEvent.objects.filter(surveillance__client_issue=self.client_issue, event_type="FU_OPENED").get()
        self.assertTrue(event.data["automatic"])
        self.assertEqual((event.data["automatic_reason"], event.data["explicit_selection"], event.actor_id), ("single_unambiguous_return", False, self.user.pk))
        self.assertTrue(ClientSurveillanceEvent.objects.filter(surveillance__client_issue=self.client_issue, event_type="RETURN_RECORDED").exists())

    def test_the_four_codes_keep_their_rules(self):  # 17
        for category, step, state in (("ACCEPTED_WITH_COMMENTS", "EN_ATTENTE_FICHIER", "BPE"), ("COMMENTED", "EN_ATTENTE_FICHIER", "PREL"), ("REJECTED", "EN_ATTENTE_FICHIER", "PREL")):
            issue = self.extra_issue(f"DOC-{category}-1")
            response = self.post(self.user, {"category": category, "comment": "see sheet"}, issue_id=issue.pk, document=issue.document_revision.document)
            self.assertEqual(response.status_code, 201, response.data)
            self.assertEqual((response.data["retention"]["followup"]["next_step"], response.data["retention"]["followup"]["expected_state"]), (step, state))

    def test_record_only_keeps_the_explicit_confirmation(self):
        response = self.post(self.user, {"category": "ACCEPTED", "auto_retain": "false"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(followups(self.client_issue).exists())
        self.assertEqual(fu_service.confirm_retained_return(self.client_issue, self.user).client_return_id, response.data["id"])

    def test_a_second_return_on_the_same_distribution_is_ambiguous_and_never_retained_automatically(self):  # 4, 6
        first = self.post(self.user, {"category": "COMMENTED", "comment": "one", "auto_retain": "false"})
        second = self.post(self.user, {"category": "ACCEPTED_WITH_COMMENTS", "comment": "two"})
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual((second.data["retention"]["retained"], second.data["retention"]["reason"]), (False, "several_returns"))
        self.assertFalse(followups(self.client_issue).exists())
        with self.assertRaises(ValidationError) as error:  # the explicit confirmation still asks for the choice
            fu_service.confirm_retained_return(self.client_issue, self.user)
        self.assertEqual(self.code(error), "selection_required")
        chosen = fu_service.confirm_retained_return(self.client_issue, self.user, return_id=first.data["id"])
        self.assertEqual(chosen.client_return_id, first.data["id"])

    def test_a_corrected_return_keeps_the_explicit_confirmation(self):  # 5
        first = self.post(self.user, {"category": "ACCEPTED"})
        self.assertTrue(first.data["retention"]["retained"])
        fix = self.post(self.user, {"category": "COMMENTED", "comment": "wrong code", "supersedes": first.data["id"], "correction_reason": "wrong code"})
        self.assertEqual(fix.status_code, 201, fix.data)
        self.assertEqual((fix.data["retention"]["retained"], fix.data["retention"]["reason"]), (False, "correction"))
        self.assertEqual(followups(self.client_issue).count(), 1)  # never a second follow-up
        followup = followups(self.client_issue).get()
        self.assertEqual(followup.needs_review, fu_service.RECONFIRM)  # the reconfirmation stays explicit

    def test_a_non_internal_document_is_recorded_but_not_retained(self):  # 10
        Document.objects.filter(pk=self.document.pk).update(document_origin="SUPPLIER")
        response = self.post(self.user, {"category": "ACCEPTED"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["retention"]["retained"], response.data["retention"]["reason"]), (False, "not_internal"))
        self.assertEqual(ClientSurveillanceReturn.objects.filter(client_issue=self.client_issue).count(), 1)
        self.assertFalse(followups(self.client_issue).exists())

    def test_the_listing_tells_which_documents_follow_the_automated_path(self):  # 9, 10
        self.post(self.user, {"category": "ACCEPTED"})
        row = self.listing(self.user).data["results"][0]
        self.assertTrue(row["internal_document"] and row["surveillance"]["automated"])  # internal + automated: no manual closure block
        Document.objects.filter(pk=self.document.pk).update(document_origin="SUPPLIER")
        self.assertFalse(self.listing(self.user).data["results"][0]["internal_document"])

    def test_without_the_right_nothing_is_recorded_nor_retained(self):  # 14
        viewer = self.grant("lotc.viewer@links-local.invalid", self.project, ["documents.view"])
        self.assertEqual(self.post(viewer, {"category": "ACCEPTED"}).status_code, 403)
        self.assertFalse(ClientSurveillanceReturn.objects.exists() or followups(self.client_issue).exists())


class GroupedTests(ProofApiBase):
    def test_three_documents_a_b_c_are_saved_once_and_the_three_follow_ups_are_created(self):  # 2, 3
        b, c = self.extra_issue("DOC-B-C1"), self.extra_issue("DOC-C-C1")
        response = self.submit([self.line(self.client_issue, "ACCEPTED"), self.line(b, "ACCEPTED_WITH_COMMENTS", "Fix"), self.line(c, "COMMENTED", "Redo")])
        self.assertEqual((response.status_code, response.data["created"]), (201, 3), response.data)
        self.assertTrue(all(r["retained"] for r in response.data["results"]), response.data["results"])
        self.assertEqual([r["next_step"] for r in response.data["results"]], ["EN_ATTENTE_FICHIER_BPE", "EN_ATTENTE_FICHIER", "EN_ATTENTE_FICHIER"])
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 3)

    def test_a_double_submit_creates_no_duplicate_follow_up(self):  # 16
        lines = [self.line(self.client_issue, "ACCEPTED")]
        self.submit(lines)
        again = self.submit(lines)
        self.assertEqual((again.status_code, again.data["results"][0]["created"], again.data["results"][0]["retained"]), (200, False, True))
        self.assertEqual((ClientSurveillanceFollowUp.objects.count(), ClientSurveillanceReturn.objects.count()), (1, 1))

    def test_a_distribution_with_another_return_is_left_to_the_explicit_confirmation(self):  # 4, 6
        self.record(self.client_issue, category=Category.COMMENTED, comment="earlier")
        response = self.submit([self.line(self.client_issue, "ACCEPTED")])
        self.assertEqual(response.status_code, 201, response.data)
        result = response.data["results"][0]
        self.assertEqual((result["retained"], result["retention_reason"]), (False, "several_returns"))
        self.assertFalse(ClientSurveillanceFollowUp.objects.exists())

    def test_a_refused_line_still_stops_everything_including_the_retentions(self):
        b = self.extra_issue("DOC-B-C2")
        response = self.submit([self.line(self.client_issue, "ACCEPTED"), self.line(b, "COMMENTED", "")])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ClientSurveillanceReturn.objects.exists() or ClientSurveillanceFollowUp.objects.exists())

    def test_without_the_right_the_grouped_save_is_refused_without_any_follow_up(self):  # 14
        viewer = self.grant("lotc.viewer2@links-local.invalid", self.project, ["documents.view", "documents.correspondence_view"])
        response = self.submit([self.line(self.client_issue, "ACCEPTED")], user=viewer)
        self.assertEqual((response.status_code, response.data["errors"][0]["code"]), (400, "permission_denied"))
        self.assertFalse(ClientSurveillanceFollowUp.objects.exists())

    def test_history_of_the_document_shows_the_return_then_the_automatic_confirmation(self):  # 15
        self.submit([self.line(self.client_issue, "ACCEPTED")])
        kinds = list(ClientSurveillanceEvent.objects.filter(surveillance__client_issue=self.client_issue, event_type__in=["RETURN_RECORDED", "FU_OPENED"]).order_by("pk").values_list("event_type", flat=True))
        self.assertEqual(kinds, ["RETURN_RECORDED", "FU_OPENED"])


class OriginalDistributionTests(ProofApiBase):
    def second_distribution(self):
        """A second revision of the SAME document, sent to the same client: a second distribution of that document."""
        from project_management.services.document_revision import add_document_revision
        from project_management.services.document_workflow import perform_action, start_workflow
        revision = add_document_revision(self.document, SimpleUploadedFile("v2.txt", b"v2"), self.owner)
        workflow = start_workflow(revision, "TWO_STEP", self.owner, None, self.approver, self.owner)
        perform_action(workflow, self.owner, "SIGN", "sign")
        perform_action(workflow, self.approver, "APPROVE", "approve")
        revision.refresh_from_db()
        self.issue_and_send([(revision, "FOR_REVIEW")])
        return revision.client_issues.get(client_company=self.client_company)

    def test_a_document_with_several_distributions_lists_each_with_its_transmittal_and_index(self):  # 7
        other = self.second_distribution()
        rows = [r for r in self.search().data["results"] if r["document_id"] == self.document.pk]
        self.assertEqual({r["client_issue_id"] for r in rows}, {self.client_issue.pk, other.pk})
        self.assertTrue(all(r["sends"] and r["sends"][0]["transmittal_reference"] and r["client_company_name"] for r in rows))

    def test_the_explicit_choice_links_exactly_that_distribution_and_revision(self):  # 8
        other = self.second_distribution()
        response = self.submit([self.line(other, "ACCEPTED")])
        self.assertEqual(response.status_code, 201, response.data)
        record = self.linked().get()
        self.assertEqual((record.client_issue_id, record.client_issue.document_revision_id), (other.pk, other.document_revision_id))
        self.assertFalse(ClientSurveillanceReturn.objects.filter(client_issue=self.client_issue).exists())  # the other distribution is untouched

    def test_only_the_authorised_distributions_are_offered(self):
        other = self.second_distribution()
        blind = self.grant("lotc.blind@links-local.invalid", self.project, ["documents.view", "documents.correspondence_view"])
        rows = self.search(user=blind).data["results"]
        self.assertTrue(rows and all(r["can_process"] is False for r in rows))  # shown as not processable: the panel disables them
        forged = self.submit([self.line(other, "ACCEPTED")], user=blind)
        self.assertEqual((forged.status_code, forged.data["errors"][0]["code"]), (400, "permission_denied"))


class ReceiptDateTests(ProofApiBase):
    def sent_at(self):
        return TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", transmittal__items__client_issue=self.client_issue).get().created_at

    def test_a_return_received_after_the_send_the_same_day_is_accepted(self):  # 12
        sent = self.sent_at()
        received = sent + (timezone.now() - sent) / 2  # after the send, the same day, never in the future
        response = self.post(self.user, {"category": "ACCEPTED", "received_at": received.isoformat()})
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_return_before_the_send_is_refused(self):  # 13
        received = self.sent_at() - timedelta(seconds=30)
        response = self.post(self.user, {"category": "ACCEPTED", "received_at": received.isoformat()})
        self.assertEqual(response.status_code, 400)
        self.assertIn("received_at", response.data)
        self.assertFalse(ClientSurveillanceReturn.objects.exists())

    def test_a_proof_date_never_becomes_midnight_and_never_the_future(self):  # 12
        yesterday = timezone.localdate() - timedelta(days=1)
        self.proof_row.received_date = yesterday
        self.proof_row.save(update_fields=["received_date"])
        suggested = timezone.localtime(default_received_at(self.proof_row))
        self.assertEqual((suggested.date(), suggested.hour, suggested.minute), (yesterday, 23, 59))
        self.proof_row.received_date = timezone.localdate()
        self.proof_row.save(update_fields=["received_date"])
        self.assertLessEqual(default_received_at(self.proof_row), timezone.now())
        self.assertTrue(self.search().data["default_received_estimated"] is False)
        self.proof_row.received_date = yesterday
        self.proof_row.save(update_fields=["received_date"])
        self.assertTrue(self.search().data["default_received_estimated"])  # the UI says the time is an estimate

    def test_a_grouped_return_dated_the_day_of_the_send_is_accepted(self):  # 12
        self.proof_row.received_date = timezone.localdate()
        self.proof_row.save(update_fields=["received_date"])
        response = self.submit([self.line(self.client_issue, "ACCEPTED")])
        self.assertEqual(response.status_code, 201, response.data)


class InTransmittalTests(ClientInBase):
    def test_the_reception_retains_the_unique_returns_without_a_second_confirmation(self):  # 11, 3
        draft = self.draft()
        a = self.line(draft, self.issue1, category="ACCEPTED", comment="")
        c = self.line(draft, self.issue2, category="COMMENTED", comment="redo")
        d = self.line(draft, self.issue3, category="REJECTED", comment="no", uploads=[pdf("x")])
        self.confirm(draft)
        for issue in (self.issue1, self.issue2, self.issue3):
            self.assertEqual(followups(issue).count(), 1, issue.pk)
        a.refresh_from_db(), c.refresh_from_db(), d.refresh_from_db()
        self.assertTrue(all(line.client_return_id for line in (a, c, d)))
        event = ClientSurveillanceEvent.objects.filter(surveillance__client_issue=self.issue1, event_type="FU_OPENED").get()
        self.assertTrue(event.data["automatic"])

    def test_a_received_transmittal_cannot_be_confirmed_twice_and_creates_no_duplicate(self):  # 16
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED", comment="")
        self.confirm(draft)
        with self.assertRaises(Exception):
            self.confirm(draft)
        self.assertEqual((followups(self.issue1).count(), ClientSurveillanceReturn.objects.filter(client_issue=self.issue1).count()), (1, 1))

    def test_a_line_the_receiver_cannot_qualify_is_qualified_and_retained_later_in_one_action(self):  # 11
        draft = self.draft()
        line = self.line(draft, self.issue1, category="ACCEPTED", comment="")
        self.confirm(draft, user=self.receiver)  # no documents.update: the line stays to qualify
        line.refresh_from_db()
        self.assertIsNone(line.client_return_id)
        record = qualify_client_return_line(line.pk, self.qualifier)
        self.assertEqual(followups(self.issue1).get().client_return_id, record.pk)

    def test_a_line_received_just_after_the_send_is_accepted_and_one_before_is_refused(self):  # 12, 13
        send = TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", transmittal__items__client_issue=self.issue1).get().created_at
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED", comment="", received_at=send + (timezone.now() - send) / 2)
        with self.assertRaises(ValidationError):
            self.line(self.draft(), self.issue2, category="ACCEPTED", comment="", received_at=send - timedelta(days=1))

    def test_a_distribution_with_another_return_is_not_retained_by_the_reception(self):  # 4
        self.record_direct(self.issue1, "COMMENTED", "earlier")
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED", comment="")
        self.confirm(draft)
        self.assertFalse(followups(self.issue1).exists())  # two returns: explicit choice

    def record_direct(self, issue, category, comment):
        return record_client_return(issue, self.controller, category=category, received_at=timezone.now(), source_channel="EMAIL", comment=comment)


class HistoryTests(ProofApiBase):
    def test_the_history_shows_the_automatic_retention_with_its_own_type(self):  # 15
        from project_management.services.document_history_events import list_document_history_events
        self.submit([self.line(self.client_issue, "ACCEPTED")])
        events = [e for e in list_document_history_events(self.document) if e.get("event_type") == "FU_OPENED"]
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["metadata"]["automatic"], events[0]["metadata"]["code"], events[0]["metadata"]["category"]), (True, "A", "ACCEPTED"))
