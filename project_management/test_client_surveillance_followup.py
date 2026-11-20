"""LOT 1: automated client surveillance follow-up (A/B/C/D) of internal documents.

Real send service, real workflow approval, real revisions and storage fixtures of the WF.4.x tests: the only stub is
the licence check and the SMTP transport (a failing one for the failed-send cases)."""
import hashlib
from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    ClientSurveillance, ClientSurveillanceFollowUp, ClientSurveillanceReturn, DocumentRevision, Transmittal, TransmittalEvent,
)
from project_management.models.document import DocumentOrigin
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.document_revision import add_document_revision
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_client_surveillance_return import MANAGE, ReturnBase
from project_management.views.client_surveillance import ClientSurveillanceFollowUpFileView, ClientSurveillanceView

Category = ClientSurveillanceReturn.Category
SMTP_FAIL = "project_management.services.transmittal_send._deliver_via_provider"


class FollowBase(ReturnBase):
    def setUp(self):
        super().setUp()
        self.document.status = "PREL"
        self.document.save(update_fields=["status"])
        self.editor = self.grant("lot1.editor@links-local.invalid", self.project, ["documents.view", "documents.update"])

    def ret(self, category, issue=None, **kw):
        return self.record(issue or self.client_issue, category=category, comment="c" if category != Category.ACCEPTED else "", **kw)

    def file(self, tag):
        return SimpleUploadedFile(f"{tag}.txt", f"content-{tag}".encode())

    def follow(self, record):
        """LOT 2: a return opens nothing by itself -- the retained return is confirmed explicitly (idempotent helper)."""
        followup = ClientSurveillanceFollowUp.objects.filter(client_return=record).first()
        if followup is None:
            followup = fu_service.confirm_retained_return(record.client_issue, self.editor, return_id=record.pk)
        return followup

    def provide(self, followup, tag="new"):
        return fu_service.provide_file(followup, self.editor, self.file(tag), confirm_bpe=True)

    def send_rev(self, revision, purpose, **kw):
        return self.issue_and_send([(revision, purpose)], **kw)

    def refreshed(self, followup):
        return ClientSurveillanceFollowUp.objects.get(pk=followup.pk)


class CodeATests(FollowBase):
    def test_a_opens_a_followup_expecting_the_bpe_file_without_any_new_revision_or_workflow(self):
        before = DocumentRevision.objects.filter(document=self.document).count()
        record = self.ret(Category.ACCEPTED)
        self.assertFalse(ClientSurveillanceFollowUp.objects.exists())  # LOT 2: a return opens nothing by itself
        followup = self.follow(record)  # explicit confirmation of the retained return
        self.assertEqual((followup.expected_state, followup.derived_revision_id), ("BPE", None))
        self.assertEqual(fu_service.next_step(followup)["code"], "EN_ATTENTE_FICHIER_BPE")
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), before)
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.status, ClientSurveillance.Status.READY_FOR_CLOSURE)  # receipt advances the review but never closes it
        self.assertTrue(self.surveillance.events.filter(event_type="FU_OPENED").exists())

    def test_the_bpe_file_keeps_the_index_stays_distinct_and_needs_no_validation(self):
        record = self.ret(Category.ACCEPTED)
        followup = self.follow(record)
        old_hash, old_file = self.revision.file_sha256, self.revision.file.name
        revision = self.provide(followup)
        self.revision.refresh_from_db()
        self.assertEqual((self.revision.file.name, self.revision.file_sha256), (old_file, old_hash))  # PREL file untouched
        self.assertIsNone(self.revision.issue_state)  # created before this lot: not qualified, never rewritten
        self.assertEqual(revision.issue_state, "BPE")
        self.assertIsNone(revision.revision_label)  # no label of its own: same index, no new cycle
        self.assertEqual(fu_service.display_index(revision), self.revision.revision_label)
        self.assertFalse(revision.workflows.exists())
        self.assertEqual(self.refreshed(followup).derived_revision_id, revision.pk)
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_DIFFUSION")
        self.assertTrue(self.surveillance.events.filter(event_type="FU_FILE", data__revision=revision.pk).exists())

    def test_the_index_is_not_applicable_before_the_verified_bpe_send(self):
        record = self.ret(Category.ACCEPTED)
        followup = self.follow(record)
        self.assertIsNone(followup.applicable_at)  # LOT 2: neither the return nor its confirmation makes it applicable
        self.assertIsNone(followup.closed_at)
        self.assertEqual(fu_service.next_step(followup)["code"], "EN_ATTENTE_FICHIER_BPE")

    def test_the_accepted_pdf_may_be_identical_and_does_not_block_the_course(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        same = SimpleUploadedFile("accepted.txt", b"v1")  # byte-identical to the PREL revision the client accepted
        revision = fu_service.provide_file(followup, self.editor, same, confirm_bpe=True)
        self.assertEqual(revision.file_sha256, self.revision.file_sha256)
        self.assertEqual((revision.issue_state, revision.revision_label), ("BPE", None))
        self.assertNotEqual(revision.pk, self.revision.pk)  # a distinct technical version, the PREL one is kept
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_DIFFUSION")
        # and it is still distributable for information (exemption) and closes after the verified send
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        self.assertEqual(self.refreshed(followup).closing_send_event_id, event.pk)

    def test_the_other_codes_still_refuse_a_previous_file(self):
        for category in (Category.ACCEPTED_WITH_COMMENTS,):
            followup = self.follow(self.ret(category))
            with self.assertRaises(ValidationError):
                fu_service.provide_file(followup, self.editor, SimpleUploadedFile("again.txt", b"v1"))
            self.assertIsNone(self.refreshed(followup).derived_revision_id)

    def test_bpe_is_sent_for_information_with_the_exemption_and_closes_after_the_verified_send(self):
        record = self.ret(Category.ACCEPTED)
        followup = self.follow(record)
        revision = self.provide(followup)
        count = ClientSurveillance.objects.count()
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        transmittal.external_contacts.add(self.client_contact)
        with self.assertRaises(ValidationError):  # BPE never goes for review
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        from project_management.services.transmittal_finalize import finalize_transmittal
        from project_management.services.transmittal_send import send_transmittal
        transmittal = finalize_transmittal(transmittal, self.owner)
        item = transmittal.items.get()
        self.assertEqual(item.evidence_snapshot["revision"]["issue_state"], "BPE")
        self.assertEqual(item.evidence_snapshot["revision"]["index_label"], self.revision.revision_label)
        event = send_transmittal(transmittal, self.owner)
        self.assertEqual(event.event_type, "SEND_SUCCEEDED")
        followup = self.refreshed(followup)
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED"))
        # applicable only once the verified BPE send has happened
        self.assertEqual((followup.closing_send_event_id, followup.applicable_at), (event.pk, event.created_at))
        self.assertEqual(ClientSurveillance.objects.count(), count)  # BPE opens no surveillance
        self.assertEqual(self.surveillance.events.filter(event_type="AUTO_CLOSED").count(), 1)
        self.assertEqual(fu_service.next_step(followup)["code"], "CLOTURE")

    def test_the_exemption_is_limited_to_that_revision_and_that_client(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        revision = self.provide(followup)
        from project_management.models import ContactCompany
        other = ContactCompany.objects.create(company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="b@x.invalid")
        wrong = self.create_transmittal(external_company=other.id)
        with self.assertRaises(ValidationError):  # another client: no exemption, no label -> not distributable
            add_transmittal_item(wrong, revision, "FOR_INFORMATION", self.owner)
        # another unapproved BPE-like version of the same document gets nothing either
        stranger = add_document_revision(self.document, self.file("stranger"), self.owner, issue_state="BPE")
        right = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(right, stranger, "FOR_INFORMATION", self.owner)

    def test_a_failed_send_closes_nothing_and_a_later_success_does(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        revision = self.provide(followup)
        transmittal = self.issue([(revision, "FOR_INFORMATION")])
        from project_management.services.transmittal_send import send_transmittal
        with patch(SMTP_FAIL, side_effect=RuntimeError("smtp down")):
            failed = send_transmittal(transmittal, self.owner)
        self.assertEqual(failed.event_type, "SEND_FAILED")
        self.assertEqual(fu_service.process_send_event(failed), [])
        followup = self.refreshed(followup)
        self.assertIsNone(followup.closed_at)
        self.assertEqual(fu_service.next_step(followup), {"code": "EN_ATTENTE_DIFFUSION", "detail": "send_failed"})
        self.surveillance.refresh_from_db()
        self.assertNotEqual(self.surveillance.status, "CLOSED")
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        self.assertIsNotNone(self.refreshed(followup).closed_at)

    def test_replaying_the_same_send_event_never_closes_or_traces_twice(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        revision = self.provide(followup)
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        self.assertEqual(fu_service.process_send_event(event), [])
        self.assertEqual(fu_service.process_send_event(event), [])
        self.assertEqual(self.surveillance.events.filter(event_type="AUTO_CLOSED").count(), 1)
        self.assertEqual([r["action"] for r in fu_service.plan_followups(event)], ["ALREADY_DONE"])

    def test_the_send_to_another_client_never_closes_and_leaves_one_review_event(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        revision = self.provide(followup)
        from project_management.models import ContactCompany, TransmittalItem
        other = ContactCompany.objects.create(company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="b@x.invalid")
        # The service refuses such an item (the exemption is per client): build it by hand to prove the closing guard too.
        transmittal = self.create_transmittal(external_company=other.id)
        TransmittalItem.objects.create(transmittal=transmittal, document_revision=revision, distribution_purpose="FOR_INFORMATION")
        event = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner, metadata={})
        self.assertEqual([row[0] for row in fu_service.process_send_event(event)], ["REVIEW"])
        self.assertEqual(fu_service.process_send_event(event), [])  # one review event, never duplicated
        self.assertIsNone(self.refreshed(followup).closed_at)
        self.assertEqual(self.surveillance.events.filter(event_type="REVIEW_NEEDED", data__reason="wrong_client").count(), 1)


class CodeBTests(FollowBase):
    def test_b_needs_the_next_index_file_full_validation_then_closes_after_the_bpe_send(self):
        record = self.ret(Category.ACCEPTED_WITH_COMMENTS)
        followup = self.follow(record)
        self.assertEqual(fu_service.next_step(followup)["code"], "EN_ATTENTE_FICHIER")
        revision = self.provide(followup, "b-file")
        self.assertEqual((revision.issue_state, revision.revision_label), ("BPE", None))
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_VALIDATION")
        # not distributable before the full internal validation
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        revision = self.approve(revision)
        self.assertEqual(revision.revision_label, "B")  # next index, assigned by the existing workflow
        self.assertTrue(self.surveillance.events.filter(event_type="FU_VALIDATED", data__revision=revision.pk).exists())
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_DIFFUSION")
        count = ClientSurveillance.objects.count()
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        followup = self.refreshed(followup)
        self.assertEqual(followup.closing_send_event_id, event.pk)
        self.assertIsNotNone(followup.applicable_at)
        self.assertEqual(ClientSurveillance.objects.count(), count)  # no new PREL review
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", "ACCEPTED_WITH_COMMENTS"))

    def test_a_rejected_validation_keeps_the_followup_waiting_and_the_file_can_be_corrected(self):
        followup = self.follow(self.ret(Category.ACCEPTED_WITH_COMMENTS))
        revision = self.provide(followup, "b1")
        from project_management.services.document_workflow import perform_action, start_workflow
        workflow = start_workflow(revision, "TWO_STEP", self.owner, None, self.approver, self.owner)
        perform_action(workflow, self.owner, "SIGN", "sign")
        perform_action(workflow, self.approver, "REJECT", "no")
        step = fu_service.next_step(self.refreshed(followup))
        self.assertEqual((step["code"], step["detail"]), ("EN_ATTENTE_VALIDATION", "rejected"))
        corrected = self.provide(followup, "b2")
        self.assertNotEqual(corrected.pk, revision.pk)
        self.assertEqual(self.refreshed(followup).derived_revision_id, corrected.pk)
        self.assertFalse(ClientSurveillanceFollowUp.objects.filter(derived_revision=revision).exists())
        self.assertEqual(DocumentRevision.objects.get(pk=revision.pk).file.name, revision.file.name)  # history kept

    def test_a_validated_revision_cannot_be_replaced(self):
        followup = self.follow(self.ret(Category.ACCEPTED_WITH_COMMENTS))
        self.approve(self.provide(followup, "b1"))
        with self.assertRaises(ValidationError):
            self.provide(followup, "b2")


class CodeCDTests(FollowBase):
    def _cycle(self, category):
        followup = self.follow(self.ret(category))
        revision = self.approve(self.provide(followup, "cd"))
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_DIFFUSION")
        return followup, revision

    def _run_cd(self, category):
        followup, revision = self._cycle(category)
        self.assertEqual(revision.issue_state, "PREL")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        transmittal.external_contacts.add(self.client_contact)
        with self.assertRaises(ValidationError):  # PREL is always for review
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        _, event = self.send_rev(revision, "FOR_REVIEW")
        followup = self.refreshed(followup)
        new = ClientSurveillance.objects.get(opening_send_event=event)
        self.assertEqual(followup.next_surveillance_id, new.pk)
        self.assertEqual(followup.closing_send_event_id, event.pk)
        self.assertIsNone(followup.applicable_at)  # PREL is never applicable
        self.assertEqual(new.client_issue.document_revision_id, revision.pk)  # indices are never mixed
        self.assertNotEqual(new.pk, self.surveillance.pk)
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("CLOSED", category))
        self.assertEqual(new.status, "UNDER_REVIEW")

    def test_c_commented_sends_prel_for_review_closes_the_old_cycle_and_opens_the_new_one_with_the_same_send(self):
        self._run_cd(Category.COMMENTED)

    def test_d_rejected_sends_prel_for_review_closes_the_old_cycle_and_opens_the_new_one_with_the_same_send(self):
        self._run_cd(Category.REJECTED)

    def test_a_replayed_cd_send_neither_closes_nor_opens_twice(self):
        followup, revision = self._cycle(Category.COMMENTED)
        _, event = self.send_rev(revision, "FOR_REVIEW")
        self.assertEqual(fu_service.process_send_event(event), [])
        self.assertEqual(ClientSurveillance.objects.filter(opening_send_event=event).count(), 1)
        self.assertEqual(self.surveillance.events.filter(event_type="AUTO_CLOSED").count(), 1)


class GuardsTests(FollowBase):
    def test_database_refuses_two_followups_for_one_return_and_one_revision_for_two(self):
        record = self.ret(Category.ACCEPTED_WITH_COMMENTS)
        self.follow(record)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillanceFollowUp.objects.create(client_return=record, surveillance=self.surveillance, expected_state="BPE")

    def test_a_second_return_never_starts_a_competing_followup_and_a_correction_before_any_file_follows_the_new_category(self):
        first = self.ret(Category.ACCEPTED)
        second = self.ret(Category.COMMENTED)
        self.follow(first)
        with self.assertRaises(ValidationError):  # LOT 2: a second retention is an explicit business refusal
            fu_service.confirm_retained_return(self.client_issue, self.editor, return_id=second.pk)
        self.assertFalse(ClientSurveillanceFollowUp.objects.filter(client_return=second).exists())
        corrected = self.record(self.client_issue, category=Category.REJECTED, comment="x", supersedes_id=first.pk, correction_reason="mistake")
        # LOT 2: the correction invalidates the confirmation -- no automatic follow-up on the corrected return
        self.assertFalse(ClientSurveillanceFollowUp.objects.filter(client_return=corrected).exists())
        followup = ClientSurveillanceFollowUp.objects.get(client_return=first)
        self.assertEqual((followup.needs_review, ClientSurveillanceFollowUp.objects.count()), ("reconfirmation_required", 1))
        reconfirmed = fu_service.confirm_retained_return(self.client_issue, self.editor, return_id=corrected.pk)
        self.assertEqual((reconfirmed.pk, reconfirmed.expected_state, reconfirmed.needs_review), (followup.pk, "PREL", ""))

    def test_a_correction_after_the_file_was_provided_needs_an_explicit_review(self):
        first = self.ret(Category.ACCEPTED)
        followup = self.follow(first)
        self.provide(followup)
        self.record(self.client_issue, category=Category.COMMENTED, comment="x", supersedes_id=first.pk, correction_reason="mistake")
        self.assertEqual(self.refreshed(followup).needs_review, "return_replaced_after_followup")
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "REVUE_EXPLICITE")

    def test_a_late_return_after_closure_opens_nothing(self):
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(status="CLOSED")
        self.ret(Category.ACCEPTED)
        self.assertFalse(ClientSurveillanceFollowUp.objects.exists())

    def test_a_revision_without_filiation_never_closes_an_open_surveillance(self):
        self.ret(Category.COMMENTED)
        stranger = self.approve(add_document_revision(self.document, self.file("stranger"), self.owner))
        _, event = self.send_rev(stranger, "FOR_REVIEW")
        self.surveillance.refresh_from_db()
        self.assertNotEqual(self.surveillance.status, "CLOSED")
        self.assertEqual(fu_service.process_send_event(event), [])

    def test_the_manual_transitions_are_refused_for_an_automated_surveillance_but_kept_for_historical_ones(self):
        full = self.full_user("lot1.full@links-local.invalid")
        response = self.patch(full, {"status": "READY_FOR_CLOSURE"})
        self.assertEqual(response.status_code, 200)  # historical: no follow-up yet, manual path preserved
        record = self.ret(Category.ACCEPTED)
        self.follow(record)  # the retained return is confirmed: a follow-up now drives the surveillance
        response = self.patch(full, {"status": "CLOSED", "retained_return": record.pk})
        self.assertEqual(response.status_code, 400)
        self.assertIn("automatically", str(response.data))

    def test_a_flagged_followup_gives_the_manual_path_back(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        ClientSurveillanceFollowUp.objects.filter(pk=followup.pk).update(needs_review="return_replaced_after_followup")
        from project_management.views.client_surveillance import surveillance_is_automated
        self.assertFalse(surveillance_is_automated(self.surveillance))

    def test_documents_update_records_a_return_and_provides_the_file(self):
        viewer = self.grant("lot1.viewer@links-local.invalid", self.project, ["documents.view"])
        self.assertEqual(self.post(viewer, {"category": "ACCEPTED"}).status_code, 403)
        self.assertEqual(self.post(self.editor, {"category": "ACCEPTED"}).status_code, 201)  # LOT 2: documents.update is enough
        followup = self.follow(ClientSurveillanceReturn.objects.get())

        def upload(user, tag):
            request = APIRequestFactory().post("/x/", {"file": self.file(tag), "confirm_bpe": "true"}, format="multipart")
            force_authenticate(request, user=user)
            return ClientSurveillanceFollowUpFileView.as_view()(request, document_id=self.document.pk, issue_id=self.client_issue.pk, followup_id=followup.pk)
        self.assertEqual(upload(viewer, "v").status_code, 403)
        self.assertEqual(upload(self.editor, "ok").status_code, 201)

    def test_the_listing_exposes_the_next_step_to_the_manager_only_as_detail(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        row = self.listing(self.manager).data["results"][0]
        self.assertEqual(row["followups"][0]["next_step"], "EN_ATTENTE_FICHIER_BPE")
        self.assertTrue(row["surveillance"]["automated"])
        viewer = self.grant("lot1.viewer2@links-local.invalid", self.project, ["documents.view"])
        self.assertEqual(self.listing(viewer).data["results"][0]["followups"], [])
        self.assertEqual(followup.pk, row["followups"][0]["id"])

    def test_the_recovery_commands_are_dry_runs_by_default_and_never_touch_historical_returns(self):
        followup = self.follow(self.ret(Category.ACCEPTED))
        revision = self.provide(followup)
        _, event = self.send_rev(revision, "FOR_INFORMATION")
        out = StringIO()
        call_command("reconcile_client_surveillance", stdout=out)
        self.assertIn("SIMULATION ONLY", out.getvalue())
        out = StringIO()
        call_command("process_client_followups", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())

    def test_the_supplier_circuit_is_untouched(self):
        supplier = self.make_supplier_revision()
        self.assertEqual(supplier.issue_state, None)
        self.assertFalse(fu_service.is_automated_document(supplier.document))
        self.assertIsNone(fu_service.followup_of_revision(supplier))

    def test_the_issue_state_is_set_once_at_creation_and_never_changed(self):
        self.assertIsNone(self.revision.issue_state)  # created before the document had a PREL/BPE status
        default = add_document_revision(self.document, self.file("d"), self.owner)
        self.assertEqual(default.issue_state, "PREL")  # defaults to the document status of that moment
        revision = add_document_revision(self.document, self.file("x"), self.owner, issue_state="BPE")
        self.assertEqual(revision.issue_state, "BPE")
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(issue_state="PREL")
        revision.issue_state = "PREL"
        with self.assertRaises(ValidationError):
            revision.save()
