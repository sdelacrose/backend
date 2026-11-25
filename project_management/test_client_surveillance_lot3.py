"""SURVEILLANCE CLIENT A/B/C/D -- LOT 3: distribution and automatic closure.

Real services end to end (return -> retained return -> file -> internal approval -> transmittal -> send -> closure) on
synthetic files; the mail transport is the in-memory backend of the opening fixtures: no real message is ever sent."""
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command

from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceFollowUp, ClientSurveillanceReturn, ContactCompany, ContactUser,
    DocumentRevision, Transmittal, TransmittalEvent, TransmittalItem,
)
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.client_surveillance_opening import open_client_surveillances
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_send import send_transmittal
from project_management.test_client_surveillance_followup import SMTP_FAIL
from project_management.test_client_surveillance_lot2 import Lot2Base

Category = ClientSurveillanceReturn.Category


class Lot3Base(Lot2Base):
    def journey(self, category):
        """return -> retained return -> file (-> internal approval for B/C/D). Returns (follow-up, revision)."""
        record = self.ret(category)
        followup = self.confirm(record)
        if category == Category.ACCEPTED:
            return followup, self.provide_bpe(followup)
        revision = fu_service.provide_file(followup, self.updater, self.file(f"file-{category}"))
        return followup, self.approve(revision)

    def purpose(self, followup):
        return fu_service.PURPOSE_OF_STATE[followup.expected_state]

    def closed(self, followup):
        return ClientSurveillanceFollowUp.objects.get(pk=followup.pk)

    def second_client(self):
        company = ContactCompany.objects.create(company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="b@x.invalid")
        contact = ContactUser.objects.create(company_profile=self.company, contact_company=company, name="Bob", surname="Roe", email="bob@second.invalid")
        return company, contact

    def kinds(self, surveillance):
        return list(surveillance.events.order_by("pk").values_list("event_type", flat=True))


class CodeADistributionTests(Lot3Base):
    def test_a_the_bpe_send_closes_and_makes_the_index_applicable(self):  # 1, 21
        followup, revision = self.journey(Category.ACCEPTED)
        count = ClientSurveillance.objects.count()
        self.assertIsNone(self.closed(followup).closed_at)  # nothing closes before the send
        transmittal, event = self.send_rev(revision, "FOR_INFORMATION")
        followup = self.closed(followup)
        self.assertEqual((followup.closing_send_event_id, followup.closed_at is not None, followup.applicable_at), (event.pk, True, event.created_at))
        self.assertIsNone(followup.next_surveillance_id)
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED"))
        self.assertEqual(ClientSurveillance.objects.count(), count)  # a BPE opens no surveillance
        self.assertEqual(self.kinds(self.surveillance).count("AUTO_CLOSED"), 1)
        closed = self.surveillance.events.get(event_type="AUTO_CLOSED").data
        self.assertEqual((closed["send_event"], closed["revision"], closed["issue_state"], closed["purpose"]), (event.pk, revision.pk, "BPE", "FOR_INFORMATION"))
        kinds = self.kinds(self.surveillance)
        self.assertLess(kinds.index("FU_OPENED"), kinds.index("FU_FILE"))
        self.assertLess(kinds.index("FU_FILE"), kinds.index("AUTO_CLOSED"))  # history in the real order
        self.assertEqual(list(transmittal.events.order_by("pk").values_list("event_type", flat=True))[-2:], ["SEND_STARTED", "SEND_SUCCEEDED"])

    def test_a_a_failed_send_closes_nothing_and_a_retry_does_not_duplicate(self):  # 2, 23
        followup, revision = self.journey(Category.ACCEPTED)
        transmittal = self.issue([(revision, "FOR_INFORMATION")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            failed = send_transmittal(transmittal, self.owner)
        self.assertEqual(failed.event_type, "SEND_FAILED")
        followup = self.closed(followup)
        self.assertEqual((followup.closed_at, followup.applicable_at, followup.closing_send_event_id), (None, None, None))
        self.surveillance.refresh_from_db()
        self.assertNotEqual(self.surveillance.status, "CLOSED")
        self.assertEqual(fu_service.next_step(followup), {"code": "EN_ATTENTE_DIFFUSION", "detail": "send_failed"})
        _, event = self.send_rev(revision, "FOR_INFORMATION")  # a new transmittal: the failed one stays in the history
        self.assertEqual(self.closed(followup).closing_send_event_id, event.pk)
        self.assertEqual(self.kinds(self.surveillance).count("AUTO_CLOSED"), 1)
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 1)

    def test_a_the_wrong_file_can_never_be_distributed_in_place_of_the_expected_one(self):  # 3, 14
        record = self.ret(Category.ACCEPTED)
        followup = self.confirm(record)
        first = self.provide_bpe(followup, "first")
        second = self.provide_bpe(self.closed(followup), "second")  # the first technical version is replaced
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, DocumentRevision.objects.get(pk=first.pk), "FOR_INFORMATION", self.owner)  # replaced
        stranger = DocumentRevision.objects.get(pk=self.revision.pk)
        self.assertIsNone(fu_service.followup_of_revision(first))
        # the historical PREL file of the accepted index is an ordinary distribution: it closes nothing
        _, event = self.send_rev(stranger, "FOR_INFORMATION")
        self.assertIsNone(self.closed(followup).closed_at)
        self.assertEqual(fu_service.process_send_event(event), [])
        _, event = self.send_rev(second, "FOR_INFORMATION")
        self.assertEqual(self.closed(followup).closing_send_event_id, event.pk)  # only the expected version closes
        self.assertEqual(self.closed(followup).derived_revision_id, second.pk)

    def test_a_wrong_client_and_wrong_purpose_are_refused_before_the_send(self):  # 4, 5
        followup, revision = self.journey(Category.ACCEPTED)
        other_company, other_contact = self.second_client()
        wrong_client = self.create_transmittal(external_company=other_company.id)
        with self.assertRaises(ValidationError) as error:
            add_transmittal_item(wrong_client, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(error.exception.message_dict["code"][0], "bpe_version_restricted")  # LOT 5-FIX: a translatable business code
        right = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError) as error:
            add_transmittal_item(right, revision, "FOR_REVIEW", self.owner)
        self.assertEqual(error.exception.message_dict["distribution_purpose"][0][:20], "This revision answer")
        self.assertIsNone(self.closed(followup).closed_at)
        self.assertEqual(TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", transmittal__in=[wrong_client, right]).count(), 0)


class CodeBDistributionTests(Lot3Base):
    def test_b_needs_the_approved_index_then_the_bpe_send_closes_and_applies(self):  # 6, 7
        record = self.ret(Category.ACCEPTED_WITH_COMMENTS)
        followup = self.confirm(record)
        revision = fu_service.provide_file(followup, self.updater, self.file("b"))
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)  # internal validation is mandatory
        revision = self.approve(revision)
        count = ClientSurveillance.objects.count()
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        followup = self.closed(followup)
        self.assertEqual((followup.closing_send_event_id, followup.applicable_at), (event.pk, event.created_at))
        self.assertIsNone(followup.next_surveillance_id)
        self.assertEqual(ClientSurveillance.objects.count(), count)  # no new PREL review
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED_WITH_COMMENTS"))
        self.assertEqual(revision.revision_label, "B")

    def test_b_a_failed_send_keeps_everything_open(self):  # 8
        followup, revision = self.journey(Category.ACCEPTED_WITH_COMMENTS)
        transmittal = self.issue([(revision, "FOR_INFORMATION")])
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            send_transmittal(transmittal, self.owner)
        followup = self.closed(followup)
        self.assertEqual((followup.closed_at, followup.applicable_at, followup.next_surveillance_id), (None, None, None))
        self.send_rev(revision, "FOR_INFORMATION")
        self.assertIsNotNone(self.closed(followup).applicable_at)


class CodeCDDistributionTests(Lot3Base):
    def run_cd(self, category):  # 9, 10, 11
        followup, revision = self.journey(category)
        self.assertEqual(followup.expected_state, "PREL")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)  # a PREL goes for review
        _, event = self.send_rev(revision, "FOR_REVIEW")
        followup = self.closed(followup)
        new = ClientSurveillance.objects.get(opening_send_event=event)
        self.assertEqual((followup.closing_send_event_id, followup.next_surveillance_id), (event.pk, new.pk))
        self.assertIsNone(followup.applicable_at)  # never applicable for C / D
        self.assertEqual(new.client_issue.document_revision_id, revision.pk)  # the new surveillance follows the SENT revision
        self.assertEqual(new.status, "UNDER_REVIEW")
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", category))
        self.assertEqual(ClientSurveillance.objects.filter(opening_send_event=event).count(), 1)
        return followup, new

    def test_c_closes_the_initial_return_and_links_the_new_surveillance(self):
        self.run_cd(Category.COMMENTED)

    def test_d_closes_the_initial_return_and_links_the_new_surveillance(self):
        self.run_cd(Category.REJECTED)

    def test_the_new_surveillance_can_receive_its_own_return_and_follow_up(self):
        followup, new = self.run_cd(Category.COMMENTED)
        record = self.record(new.client_issue, category=Category.ACCEPTED)
        self.assertEqual(self.confirm(record, issue=new.client_issue).document_id, self.document.pk)  # a new cycle, same document and client


class IdempotenceTests(Lot3Base):
    def test_a_double_send_succeeded_and_a_double_processing_never_duplicate(self):  # 12, 13
        followup, revision = self.journey(Category.COMMENTED)
        transmittal, event = self.send_rev(revision, "FOR_REVIEW")
        new = ClientSurveillance.objects.filter(opening_send_event=event).count()
        self.assertEqual(fu_service.process_send_event(event), [])
        duplicate = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner, metadata={"duplicate": True})
        self.assertEqual(fu_service.process_send_event(duplicate), [])
        total = ClientSurveillance.objects.count()
        self.assertEqual({action for action, _ in open_client_surveillances(duplicate)} - {"RESEND"}, set())  # at most a traced resend, never a new cycle
        self.assertEqual(ClientSurveillance.objects.count(), total)
        self.assertEqual(open_client_surveillances(event), [])
        self.assertEqual(ClientSurveillance.objects.filter(opening_send_event=event).count(), new)
        self.assertEqual(self.kinds(self.surveillance).count("AUTO_CLOSED"), 1)
        self.assertEqual(self.closed(followup).closing_send_event_id, event.pk)
        self.assertEqual([r["action"] for r in fu_service.plan_followups(event)], ["ALREADY_DONE"])
        self.assertEqual([r["action"] for r in fu_service.plan_followups(duplicate)], ["ALREADY_CLOSED"])

    def test_a_partial_failure_leaves_the_send_and_the_open_follow_up_and_the_replay_finishes(self):  # 22, 23
        followup, revision = self.journey(Category.ACCEPTED)
        with patch("project_management.services.client_surveillance_followup.process_send_event", side_effect=RuntimeError("db hiccup")):
            _, event = self.send_rev(revision, "FOR_INFORMATION")
        self.assertEqual(event.event_type, "SEND_SUCCEEDED")  # the message stays sent
        self.assertIsNone(self.closed(followup).closed_at)
        call_command("process_client_followups", stdout=StringIO())  # dry run writes nothing
        self.assertIsNone(self.closed(followup).closed_at)
        call_command("process_client_followups", "--apply", stdout=StringIO())
        followup = self.closed(followup)
        self.assertEqual((followup.closing_send_event_id, followup.applicable_at), (event.pk, event.created_at))
        call_command("process_client_followups", "--apply", stdout=StringIO())  # replay: nothing more
        self.assertEqual(self.kinds(self.surveillance).count("AUTO_CLOSED"), 1)

    def test_when_the_new_cycle_could_not_be_opened_the_follow_up_waits_and_the_replay_closes_it(self):  # 22
        followup, revision = self.journey(Category.REJECTED)
        with patch("project_management.services.client_surveillance_opening.open_client_surveillances_safely"):
            _, event = self.send_rev(revision, "FOR_REVIEW")
        self.assertIsNone(self.closed(followup).closed_at)  # no closure without the opened cycle
        self.assertFalse(ClientSurveillance.objects.filter(opening_send_event=event).exists())
        open_client_surveillances(event)
        self.assertEqual(len(fu_service.process_send_event(event)), 1)
        followup = self.closed(followup)
        self.assertEqual(followup.next_surveillance.opening_send_event_id, event.pk)
        self.assertIsNone(followup.applicable_at)


class GuardTests(Lot3Base):
    def test_a_corrected_return_blocks_the_distribution_before_and_at_send_time(self):  # 15
        first = self.ret(Category.ACCEPTED)
        followup = self.confirm(first)
        revision = self.provide_bpe(followup)
        transmittal = self.issue([(revision, "FOR_INFORMATION")])  # finalized, not sent yet
        self.record(category=Category.COMMENTED, comment="c", supersedes_id=first.pk, correction_reason="mistake")
        self.assertEqual(self.closed(followup).needs_review, "return_replaced_after_followup")
        with self.assertRaises(ValidationError):
            send_transmittal(transmittal, self.owner)  # re-checked right before the send
        self.assertFalse(transmittal.events.filter(event_type__in=["SEND_STARTED", "SEND_SUCCEEDED"]).exists())
        self.assertIsNone(self.closed(followup).closed_at)

    def test_reconfirmation_is_required_before_any_documentary_step_and_then_the_course_resumes(self):  # 16
        first = self.ret(Category.ACCEPTED_WITH_COMMENTS)
        followup = self.confirm(first)
        corrected = self.record(category=Category.REJECTED, comment="c", supersedes_id=first.pk, correction_reason="mistake")
        self.assertEqual(self.closed(followup).needs_review, "reconfirmation_required")
        with self.assertRaises(ValidationError):
            fu_service.provide_file(followup, self.updater, self.file("early"))
        self.confirm(corrected)
        revision = self.approve(fu_service.provide_file(self.closed(followup), self.updater, self.file("late")))
        self.assertEqual(self.closed(followup).expected_state, "PREL")
        _, event = self.send_rev(revision, "FOR_REVIEW")
        self.assertEqual(self.closed(followup).closing_send_event_id, event.pk)

    def test_a_wrong_index_is_refused_at_distribution_and_a_conflict_blocks_it(self):  # 17
        followup, revision = self.journey(Category.ACCEPTED_WITH_COMMENTS)
        ClientSurveillanceFollowUp.objects.filter(pk=followup.pk).update(expected_index="Z")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError) as error:
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(error.exception.message_dict["code"][0], "index_mismatch")
        ClientSurveillanceFollowUp.objects.filter(pk=followup.pk).update(expected_index="B", needs_review="index_conflict")
        with self.assertRaises(ValidationError) as error:
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        self.assertEqual(error.exception.message_dict["code"][0], "followup_in_review")

    def test_a_wrong_issue_state_is_refused_at_distribution(self):  # 18
        followup, revision = self.journey(Category.ACCEPTED_WITH_COMMENTS)
        self.force(revision, issue_state="PREL")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError) as error:
            add_transmittal_item(transmittal, DocumentRevision.objects.get(pk=revision.pk), "FOR_INFORMATION", self.owner)
        self.assertEqual(error.exception.message_dict["code"][0], "issue_state_mismatch")

    def test_a_frozen_hash_that_differs_from_the_stored_one_never_closes(self):
        followup, revision = self.journey(Category.ACCEPTED_WITH_COMMENTS)
        transmittal = self.issue([(revision, "FOR_INFORMATION")])
        item = transmittal.items.get()
        event = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner, metadata={})
        self.assertEqual(fu_service._check(self.closed(followup), item, event), (None, None))  # consistent: it would close
        item.evidence_snapshot["revision"]["file_sha256"] = "f" * 64  # forged in memory only (the snapshot is immutable)
        self.assertEqual(fu_service._check(self.closed(followup), item, event), ("review", "hash_mismatch"))


class MultipleClientsTests(Lot3Base):
    def test_a_send_closes_only_the_follow_up_of_the_addressed_client(self):  # 19
        other_company, other_contact = self.second_client()
        self.issue_and_send([(self.revision, "FOR_REVIEW")], company=other_company, contacts=[other_contact])
        other_issue = ClientIssue.objects.get(document_revision=self.revision, client_company=other_company)
        mine = self.confirm(self.ret(Category.ACCEPTED))
        theirs = self.confirm(self.record(other_issue, category=Category.ACCEPTED))
        mine_rev = self.provide_bpe(mine, "mine")
        theirs_rev = self.provide_bpe(self.closed(theirs), "theirs")
        wrong = self.create_transmittal(external_company=other_company.id)
        with self.assertRaises(ValidationError):  # never another client's version to this client
            add_transmittal_item(wrong, mine_rev, "FOR_INFORMATION", self.owner)
        _, event = self.send_rev(mine_rev, "FOR_INFORMATION")
        self.assertEqual((self.closed(mine).closing_send_event_id, self.closed(theirs).closed_at), (event.pk, None))
        other_surveillance = ClientSurveillance.objects.get(client_issue=other_issue)
        self.assertNotEqual(other_surveillance.status, "CLOSED")
        _, event2 = self.send_rev(theirs_rev, "FOR_INFORMATION", company=other_company, contacts=[other_contact])
        self.assertEqual(self.closed(theirs).closing_send_event_id, event2.pk)
        self.assertEqual(self.closed(mine).closing_send_event_id, event.pk)  # unchanged
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(closed_at__isnull=True).count(), 0)

    def test_the_closure_never_touches_a_historical_surveillance_without_a_confirmed_follow_up(self):
        self.ret(Category.ACCEPTED)  # recorded, NOT retained
        other = self.review()
        self.issue_and_send([(other, "FOR_REVIEW")])
        self.surveillance.refresh_from_db()
        self.assertNotEqual(self.surveillance.status, "CLOSED")
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 0)
