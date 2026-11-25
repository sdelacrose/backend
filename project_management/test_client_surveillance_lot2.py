"""SURVEILLANCE CLIENT A/B/C/D -- LOT 2: business rules and permissions.

Real services, real workflow approval and revision storage (fixtures of the WF.4.x tests); no real distribution is
exercised except where the LOT 1 closing path is re-used to prove that "applicable" is only acquired after a verified send."""
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceFollowUp, ClientSurveillanceReturn, ContactCompany, ContactUser,
    DocumentRevision,
)
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import start_workflow
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_client_surveillance_followup import FollowBase
from project_management.test_client_surveillance_return import MANAGE
from project_management.views.client_surveillance import (
    ClientSurveillanceFollowUpFileView, ClientSurveillanceRetainedReturnView,
)

Category = ClientSurveillanceReturn.Category


class Lot2Base(FollowBase):
    def setUp(self):
        super().setUp()
        self.updater = self.grant("lot2.updater@links-local.invalid", self.project, ["documents.view", "documents.update"])

    def force(self, revision, **columns):
        """Raw SQL: the ORM refuses to modify a stored revision, the tests need to forge inconsistent rows."""
        with connection.cursor() as cursor:
            for column, value in columns.items():
                cursor.execute("UPDATE project_management_documentrevision SET %s = %%s WHERE id = %%s" % column, [value, revision.pk])

    def codes(self, error):
        return error.exception.message_dict.get("code", [None])[0]

    def confirm(self, record=None, issue=None, user=None, **kw):
        issue = issue or (record.client_issue if record else self.client_issue)
        return fu_service.confirm_retained_return(issue, user or self.updater, return_id=record.pk if record else None, **kw)

    def confirm_api(self, user, body=None, issue_id=None, document=None):
        request = APIRequestFactory().post("/x/", body or {}, format="json")
        force_authenticate(request, user=user)
        return ClientSurveillanceRetainedReturnView.as_view()(
            request, document_id=(document or self.document).pk, issue_id=issue_id or self.client_issue.pk)

    def provide_api(self, user, followup, tag="bpe", confirm=True):
        data = {"file": self.file(tag)}
        if confirm:
            data["confirm_bpe"] = "true"
        request = APIRequestFactory().post("/x/", data, format="multipart")
        force_authenticate(request, user=user)
        return ClientSurveillanceFollowUpFileView.as_view()(
            request, document_id=self.document.pk, issue_id=self.client_issue.pk, followup_id=followup.pk)

    def retained(self, category, comment_for_ok=True):
        record = self.ret(category)
        return record, self.confirm(record)

    def provide_bpe(self, followup, tag="bpe", content=None):
        upload = SimpleUploadedFile(f"{tag}.txt", content if content is not None else f"content-{tag}".encode())
        return fu_service.provide_file(followup, self.updater, upload, confirm_bpe=True)


class PermissionTests(Lot2Base):
    def test_documents_update_alone_records_a_client_return(self):  # 1
        response = self.post(self.updater, {"category": "ACCEPTED"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_neither_view_alone_nor_the_administrative_right_alone_records_a_return(self):  # 2
        reader = self.grant("lot2.reader@links-local.invalid", self.project, ["documents.view"])
        self.assertEqual(self.post(reader).status_code, 403)
        manage_only = self.grant("lot2.manage@links-local.invalid", self.project, ["documents.view", MANAGE])
        self.assertEqual(self.post(manage_only).status_code, 403)  # client_surveillance_manage is no longer the gate
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)

    def test_the_right_is_checked_in_the_documents_own_project_and_company(self):  # 3
        user = self.grant("lot2.elsewhere@links-local.invalid", self.project, ["documents.view"])
        self.grant("lot2.elsewhere@links-local.invalid", self.other_project, ["documents.update"], user=user)
        self.assertEqual(self.post(user).status_code, 403)
        outsider = self.grant("lot2.outsider@links-local.invalid", self.other_tenant_project,
                              ["documents.view", "documents.update"], company=self.other_company)
        self.assertEqual(self.post(outsider).status_code, 404)  # the document is invisible to another company
        self.assertEqual(self.confirm_api(outsider).status_code, 404)

    def test_the_update_right_sees_what_processing_a_return_needs(self):
        self.ret(Category.ACCEPTED)
        reader = self.grant("lot2.reader2@links-local.invalid", self.project, ["documents.view"])
        row = self.listing(self.updater).data["results"][0]
        self.assertTrue(row["can_process"])
        self.assertTrue(row["detail_visible"])
        self.assertEqual(len(row["returns"]), 1)
        self.assertEqual(len(row["retainable_returns"]), 1)
        row = self.listing(reader).data["results"][0]
        self.assertFalse(row["can_process"])
        self.assertEqual(row["retainable_returns"] if "retainable_returns" in row else [], [])

    def test_the_administrative_operations_keep_their_specific_right(self):  # 19
        # the due date and the manual closure with a result still need client_surveillance_manage
        request = APIRequestFactory().patch("/x/", {"due_at": "2030-01-01T10:00:00Z"}, format="json")
        force_authenticate(request, user=self.updater)
        from project_management.views.client_surveillance import ClientSurveillanceDueDateView
        response = ClientSurveillanceDueDateView.as_view()(request, document_id=self.document.pk, issue_id=self.client_issue.pk)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.patch(self.updater, {"status": "CLOSED"}).status_code, 403)
        # and the file of the derived revision still needs documents.update
        record, followup = self.retained(Category.ACCEPTED)
        manage_only = self.grant("lot2.manage2@links-local.invalid", self.project, ["documents.view", MANAGE])
        self.assertEqual(self.provide_api(manage_only, followup).status_code, 403)


class ConfirmationTests(Lot2Base):
    def test_recording_or_importing_a_return_opens_nothing(self):
        for category in (Category.ACCEPTED, Category.COMMENTED):
            self.ret(category)
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 0)
        self.assertFalse(self.surveillance.events.filter(event_type="FU_OPENED").exists())
        self.assertEqual(self.surveillance.events.filter(event_type="RETURN_RECORDED").count(), 2)  # history kept

    def test_a_single_return_is_confirmed_in_one_action(self):  # 4
        record = self.ret(Category.ACCEPTED)
        followup = fu_service.confirm_retained_return(self.client_issue, self.updater)  # no selection needed
        self.assertEqual((followup.client_return_id, followup.expected_state, followup.created_by_id), (record.pk, "BPE", self.updater.pk))
        event = self.surveillance.events.get(event_type="FU_OPENED")
        self.assertEqual((event.data["return"], event.data["explicit_selection"], event.data["retained_by"]), (record.pk, False, self.updater.pk))
        self.assertEqual((followup.document_id, followup.client_company_id), (self.document.pk, self.client_company.pk))

    def test_several_returns_need_an_explicit_selection(self):  # 5
        first, second = self.ret(Category.COMMENTED), self.ret(Category.ACCEPTED_WITH_COMMENTS)
        with self.assertRaises(ValidationError) as error:
            fu_service.confirm_retained_return(self.client_issue, self.updater)
        self.assertEqual(self.codes(error), "selection_required")
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 0)
        followup = self.confirm(second)
        self.assertEqual(followup.client_return_id, second.pk)
        self.assertEqual(self.surveillance.events.get(event_type="FU_OPENED").data["explicit_selection"], True)
        self.assertEqual(self.surveillance.events.get(event_type="FU_OPENED").data["candidates"], [first.pk, second.pk])
        self.assertTrue(ClientSurveillanceReturn.objects.filter(pk=first.pk).exists())  # the other return stays in the history

    def test_no_retained_return_means_no_documentary_follow_up(self):
        with self.assertRaises(ValidationError) as error:
            fu_service.confirm_retained_return(self.client_issue, self.updater)
        self.assertEqual(self.codes(error), "no_return")
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 0)

    def test_a_double_confirmation_is_refused(self):  # 6
        record = self.ret(Category.ACCEPTED)
        self.confirm(record)
        for target in (record, None):
            with self.assertRaises(ValidationError) as error:
                fu_service.confirm_retained_return(self.client_issue, self.updater, return_id=target.pk if target else None)
            self.assertEqual(self.codes(error), "already_confirmed")
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 1)
        self.assertEqual(self.surveillance.events.filter(event_type="FU_OPENED").count(), 1)

    def test_the_api_confirms_refuses_and_never_answers_500(self):
        record = self.ret(Category.ACCEPTED)
        reader = self.grant("lot2.reader3@links-local.invalid", self.project, ["documents.view"])
        self.assertEqual(self.confirm_api(reader).status_code, 403)
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 0)
        response = self.confirm_api(self.updater, {"return": record.pk})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["return_id"], record.pk)
        again = self.confirm_api(self.updater, {"return": record.pk})
        self.assertEqual((again.status_code, again.data["code"][0]), (400, "already_confirmed"))
        self.assertEqual(self.confirm_api(self.updater, {"return": "x"}).status_code, 400)
        self.assertEqual(self.confirm_api(self.updater, {"return": 999999}).status_code, 400)

    def test_a_corrected_return_cannot_be_retained_and_the_correction_replaces_it(self):  # 17
        first = self.ret(Category.COMMENTED)
        corrected = self.record(category=Category.ACCEPTED_WITH_COMMENTS, comment="c", supersedes_id=first.pk, correction_reason="mistake")
        with self.assertRaises(ValidationError) as error:
            self.confirm(first)
        self.assertEqual(self.codes(error), "return_replaced")
        self.assertEqual(self.confirm(corrected).client_return_id, corrected.pk)

    def corrected(self, first, category=Category.ACCEPTED_WITH_COMMENTS):
        return self.record(category=category, comment="" if category == Category.ACCEPTED else "c", supersedes_id=first.pk, correction_reason="mistake")

    def test_correcting_a_retained_return_before_any_file_requires_a_new_confirmation(self):
        first = self.ret(Category.COMMENTED)
        followup = self.confirm(first)
        corrected = self.corrected(first)
        followup.refresh_from_db()
        # the confirmation is invalidated, nothing continues, no concurrent follow-up is created
        self.assertEqual((followup.needs_review, followup.client_return_id, followup.applicable_at), ("reconfirmation_required", first.pk, None))
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 1)
        self.assertEqual(fu_service.next_step(followup), {"code": "REVUE_EXPLICITE", "detail": "reconfirmation_required"})
        with self.assertRaises(ValidationError):  # no documentary step before the reconfirmation
            fu_service.provide_file(followup, self.updater, self.file("early"))
        self.assertEqual(self.refreshed(followup).derived_revision_id, None)
        self.assertEqual(fu_service.retainable_returns(self.client_issue), [corrected])
        review = self.surveillance.events.get(event_type="REVIEW_NEEDED", data__reason="reconfirmation_required")
        self.assertEqual((review.data["replaced_return"], review.data["return"], review.data["previous_category"], review.data["new_category"]),
                         (first.pk, corrected.pk, "COMMENTED", "ACCEPTED_WITH_COMMENTS"))

    def test_the_reconfirmation_re_points_the_same_follow_up_and_keeps_the_history(self):
        first = self.ret(Category.COMMENTED)
        followup = self.confirm(first)
        corrected = self.corrected(first)
        again = self.confirm(corrected)  # one candidate: one explicit action
        self.assertEqual((again.pk, again.client_return_id, again.expected_state, again.needs_review), (followup.pk, corrected.pk, "BPE", ""))
        self.assertEqual(again.expected_index, "B")
        self.assertEqual(ClientSurveillanceFollowUp.objects.count(), 1)
        events = list(self.surveillance.events.filter(event_type="FU_OPENED").order_by("pk"))
        self.assertEqual([e.data["return"] for e in events], [first.pk, corrected.pk])
        self.assertEqual((events[1].data["reconfirmed"], events[1].data["previous_return"]), (True, first.pk))
        self.assertTrue(ClientSurveillanceReturn.objects.filter(pk=first.pk).exists())
        self.assertEqual(fu_service.next_step(again)["code"], "EN_ATTENTE_FICHIER")
        with self.assertRaises(ValidationError) as error:  # and a second reconfirmation is refused
            self.confirm(corrected)
        self.assertEqual(self.codes(error), "already_confirmed")

    def test_a_code_change_is_taken_into_account_only_after_the_reconfirmation(self):
        first = self.ret(Category.ACCEPTED)
        followup = self.confirm(first)
        self.assertEqual((followup.expected_state, followup.expected_index), ("BPE", self.revision.revision_label))
        corrected = self.corrected(first, Category.REJECTED)
        self.assertEqual(self.refreshed(followup).expected_state, "BPE")  # unchanged until reconfirmed
        again = self.confirm(corrected)
        self.assertEqual((again.expected_state, again.expected_index), ("PREL", "B"))

    def test_even_a_correction_that_keeps_the_code_needs_the_reconfirmation(self):
        first = self.ret(Category.ACCEPTED_WITH_COMMENTS)
        followup = self.confirm(first)
        corrected = self.corrected(first, Category.ACCEPTED_WITH_COMMENTS)
        self.assertEqual(self.refreshed(followup).needs_review, "reconfirmation_required")
        self.assertEqual(self.confirm(corrected).client_return_id, corrected.pk)

    def test_several_corrected_candidates_need_an_explicit_selection(self):
        first = self.ret(Category.COMMENTED)
        followup = self.confirm(first)
        corrected = self.corrected(first)
        other = self.ret(Category.REJECTED)
        with self.assertRaises(ValidationError) as error:
            fu_service.confirm_retained_return(self.client_issue, self.updater)
        self.assertEqual(self.codes(error), "selection_required")
        with self.assertRaises(ValidationError) as error:  # the replaced return can never be retained again
            self.confirm(first)
        self.assertEqual(self.codes(error), "return_replaced")
        self.assertEqual(self.confirm(other).pk, followup.pk)
        self.assertTrue(ClientSurveillanceReturn.objects.filter(pk=corrected.pk).exists())

    def test_the_api_exposes_the_corrected_return_as_retainable_and_reconfirms(self):
        first = self.ret(Category.COMMENTED)
        self.confirm(first)
        corrected = self.corrected(first)
        row = self.row_for(self.updater)
        self.assertEqual((row["retainable_returns"], row["followups"][0]["next_step"], row["followups"][0]["needs_review"]),
                         ([corrected.pk], "REVUE_EXPLICITE", "reconfirmation_required"))
        response = self.confirm_api(self.updater)
        self.assertEqual((response.status_code, response.data["return_id"], response.data["needs_review"]), (201, corrected.pk, None))
        self.assertEqual(self.confirm_api(self.updater, {"return": corrected.pk}).data["code"][0], "already_confirmed")

    def row_for(self, user):
        return self.listing(user).data["results"][0]

    def test_a_retained_return_corrected_after_the_file_needs_an_explicit_review(self):
        first = self.ret(Category.COMMENTED)
        followup = self.confirm(first)
        fu_service.provide_file(followup, self.updater, self.file("c-file"))
        self.record(category=Category.REJECTED, comment="c", supersedes_id=first.pk, correction_reason="mistake")
        followup.refresh_from_db()
        self.assertEqual(followup.needs_review, "return_replaced_after_followup")
        self.assertEqual(DocumentRevision.objects.filter(pk=followup.derived_revision_id).count(), 1)  # nothing deleted
        self.assertEqual(self.refreshed(followup).derived_revision_id, followup.derived_revision_id)  # nor replaced
        corrected = ClientSurveillanceReturn.objects.get(supersedes=first)
        with self.assertRaises(ValidationError) as error:  # a reconfirmation cannot bypass the blocking review
            self.confirm(corrected)
        self.assertEqual(self.codes(error), "followup_in_review")
        with self.assertRaises(ValidationError):
            fu_service.provide_file(followup, self.updater, self.file("again"))

    def test_a_correction_after_the_a_file_blocks_its_distribution_until_the_review_is_resolved(self):
        first = self.ret(Category.ACCEPTED)
        followup = self.confirm(first)
        revision = self.provide_bpe(followup)
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)  # distributable before the correction
        transmittal.items.all().delete()
        self.corrected(first, Category.ACCEPTED_WITH_COMMENTS)
        self.assertEqual(self.refreshed(followup).needs_review, "return_replaced_after_followup")
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, DocumentRevision.objects.get(pk=revision.pk), "FOR_INFORMATION", self.owner)



class CodeAFileTests(Lot2Base):
    def test_a_needs_an_explicit_file_and_never_reuses_the_prel_one(self):  # 7
        record, followup = self.retained(Category.ACCEPTED)
        self.assertEqual(fu_service.next_step(followup)["code"], "EN_ATTENTE_FICHIER_BPE")
        before = DocumentRevision.objects.filter(document=self.document).count()
        with self.assertRaises(ValidationError):
            fu_service.provide_file(followup, self.updater, None, confirm_bpe=True)
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), before)
        self.assertIsNone(self.refreshed(followup).derived_revision_id)  # nothing was picked automatically

    def test_a_needs_the_explicit_bpe_confirmation(self):
        record, followup = self.retained(Category.ACCEPTED)
        before = DocumentRevision.objects.filter(document=self.document).count()
        with self.assertRaises(ValidationError) as error:
            fu_service.provide_file(followup, self.updater, self.file("bpe"))
        self.assertEqual(self.codes(error), "bpe_confirmation_required")
        self.assertEqual(self.provide_api(self.updater, followup, confirm=False).status_code, 400)
        self.assertEqual(DocumentRevision.objects.filter(document=self.document).count(), before)

    def test_a_with_an_explicit_bpe_file_keeps_the_index_through_index_revision(self):  # 8
        record, followup = self.retained(Category.ACCEPTED)
        old = (self.revision.file.name, self.revision.file_sha256, self.revision.revision_label)
        revision = self.provide_bpe(followup)
        self.revision.refresh_from_db()
        self.assertEqual((self.revision.file.name, self.revision.file_sha256, self.revision.revision_label), old)  # PREL kept
        self.assertEqual((revision.index_revision_id, revision.issue_state, revision.revision_label), (self.revision.pk, "BPE", None))
        self.assertEqual(fu_service.display_index(revision), self.revision.revision_label)
        self.assertEqual(self.refreshed(followup).expected_index, self.revision.revision_label)
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_DIFFUSION")
        event = self.surveillance.events.get(event_type="FU_FILE")
        self.assertEqual((event.data["index_revision"], event.data["expected_index"]), (self.revision.pk, self.revision.revision_label))
        self.assertEqual(self.provide_api(self.updater, self.refreshed(followup), "again").status_code, 201)  # replaceable until distributed

    def test_a_with_content_identical_to_the_prel_file_is_accepted_as_a_distinct_version(self):  # 9
        record, followup = self.retained(Category.ACCEPTED)
        revision = self.provide_bpe(followup, content=b"v1")  # same bytes as the PREL revision
        self.assertEqual(revision.file_sha256, self.revision.file_sha256)
        self.assertNotEqual(revision.pk, self.revision.pk)
        self.assertNotEqual(revision.file.name, self.revision.file.name)  # a new physical file

    def test_a_is_not_applicable_before_the_verified_bpe_send(self):  # 10
        record = self.ret(Category.ACCEPTED)
        self.assertIsNone(ClientSurveillanceFollowUp.objects.filter(client_return=record).first())
        followup = self.confirm(record)
        self.assertIsNone(followup.applicable_at)
        revision = self.provide_bpe(followup)
        self.assertIsNone(self.refreshed(followup).applicable_at)
        _, event = self.send_rev(revision, "FOR_INFORMATION")  # LOT 1 closing path, only to prove the acquisition point
        self.assertEqual(self.refreshed(followup).applicable_at, event.created_at)

    def test_a_never_starts_a_new_internal_workflow(self):
        record, followup = self.retained(Category.ACCEPTED)
        revision = self.provide_bpe(followup)
        with self.assertRaises(ValidationError):
            start_workflow(revision, "TWO_STEP", self.owner, None, self.approver, self.owner)
        self.assertFalse(revision.workflows.exists())

    def test_a_exemption_needs_the_confirmed_return_and_the_index_link(self):
        record, followup = self.retained(Category.ACCEPTED)
        stranger = add_document_revision(self.document, self.file("stranger"), self.owner, issue_state="BPE")  # no index_revision
        right = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(right, stranger, "FOR_INFORMATION", self.owner)
        revision = self.provide_bpe(followup)
        wrong_link = DocumentRevision.objects.get(pk=revision.pk)
        self.force(revision, index_revision_id=None)
        with self.assertRaises(ValidationError):
            add_transmittal_item(right, DocumentRevision.objects.get(pk=wrong_link.pk), "FOR_INFORMATION", self.owner)

    def test_a_without_a_confirmed_return_has_no_exemption_at_all(self):
        self.ret(Category.ACCEPTED)  # recorded, NOT retained
        bpe = add_document_revision(self.document, self.file("lonely"), self.owner, issue_state="BPE", index_revision=self.revision)
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, bpe, "FOR_INFORMATION", self.owner)

    def test_a_refuses_a_conflicting_expected_index(self):  # 13 (A)
        record, followup = self.retained(Category.ACCEPTED)
        ClientSurveillanceFollowUp.objects.filter(pk=followup.pk).update(expected_index="Z")
        with self.assertRaises(ValidationError) as error:
            fu_service.provide_file(self.refreshed(followup), self.updater, self.file("bpe"), confirm_bpe=True)
        self.assertEqual(self.codes(error), "index_conflict")


class CodeBCDTests(Lot2Base):
    def test_b_needs_a_modified_file_a_new_index_and_the_full_validation(self):  # 11
        record, followup = self.retained(Category.ACCEPTED_WITH_COMMENTS)
        self.assertEqual(followup.expected_index, "B")
        with self.assertRaises(ValidationError):  # the previous content is never accepted
            fu_service.provide_file(followup, self.updater, SimpleUploadedFile("same.txt", b"v1"))
        revision = fu_service.provide_file(followup, self.updater, self.file("b-new"))
        self.assertEqual((revision.issue_state, revision.revision_label, revision.index_revision_id), ("BPE", None, None))
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_VALIDATION")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):  # no distribution before the validation
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        with self.assertRaises(ValidationError):  # BPE never opens a new client review
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        revision = self.approve(revision)
        self.assertEqual(revision.revision_label, "B")
        self.assertEqual(self.refreshed(followup).needs_review, "")
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)

    def test_c_and_d_need_a_modified_file_a_new_index_the_validation_and_a_prel_for_review(self):  # 12
        record = self.ret(Category.COMMENTED)
        followup = self.confirm(record)
        self.assertEqual(followup.expected_state, "PREL")
        with self.assertRaises(ValidationError):
            fu_service.provide_file(followup, self.updater, SimpleUploadedFile("same.txt", b"v1"))
        revision = fu_service.provide_file(followup, self.updater, self.file("c-new"))
        self.assertEqual((revision.issue_state, revision.revision_label), ("PREL", None))
        self.assertEqual(fu_service.next_step(self.refreshed(followup))["code"], "EN_ATTENTE_VALIDATION")
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        revision = self.approve(revision)
        self.assertEqual(revision.revision_label, "B")
        with self.assertRaises(ValidationError):  # PREL is distributed for review only
            fu_service.assert_purpose(revision, "FOR_INFORMATION")
        fu_service.assert_purpose(revision, "FOR_REVIEW")
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_d_prepares_the_same_prel_treatment_as_c(self):
        record, followup = self.retained(Category.REJECTED)
        self.assertEqual((followup.expected_state, followup.expected_index), ("PREL", "B"))
        revision = fu_service.provide_file(followup, self.updater, self.file("d-new"))
        self.assertEqual((revision.issue_state, fu_service.next_step(self.refreshed(followup))["code"]), ("PREL", "EN_ATTENTE_VALIDATION"))

    def test_a_wrong_index_at_approval_is_a_business_review_not_an_error(self):  # 13
        record, followup = self.retained(Category.ACCEPTED_WITH_COMMENTS)
        revision = fu_service.provide_file(followup, self.updater, self.file("b-new"))
        other = add_document_revision(self.document, self.file("other"), self.owner)
        self.approve(other)  # another version takes the expected index B meanwhile
        revision = self.approve(revision)
        self.assertEqual(revision.revision_label, "C")
        followup = self.refreshed(followup)
        self.assertEqual(followup.needs_review, "index_conflict")
        event = self.surveillance.events.get(event_type="REVIEW_NEEDED", data__reason="index_conflict")
        self.assertEqual((event.data["expected_index"], event.data["actual_index"]), ("B", "C"))
        self.assertEqual(fu_service.next_step(followup)["code"], "REVUE_EXPLICITE")
        with self.assertRaises(ValidationError):  # the follow-up is frozen until an explicit review
            fu_service.provide_file(followup, self.updater, self.file("again"))

    def test_a_wrong_issue_state_never_closes_and_a_wrong_purpose_is_refused(self):  # 14
        record, followup = self.retained(Category.ACCEPTED_WITH_COMMENTS)
        revision = self.approve(fu_service.provide_file(followup, self.updater, self.file("b-new")))
        transmittal = self.create_transmittal(external_company=self.client_company.id)
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)  # BPE is for information
        self.force(revision, issue_state="PREL")  # forced mismatch
        revision = DocumentRevision.objects.get(pk=revision.pk)
        with self.assertRaises(ValidationError):  # LOT 3: refused before the send already
            add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        # ...and, should such an item ever reach a verified send, the closing guard still holds
        from project_management.models import TransmittalEvent, TransmittalItem
        TransmittalItem.objects.create(transmittal=transmittal, document_revision=revision, distribution_purpose="FOR_INFORMATION")
        event = TransmittalEvent.objects.create(transmittal=transmittal, event_type="SEND_SUCCEEDED", actor=self.owner, metadata={})
        self.assertEqual([row[0] for row in fu_service.process_send_event(event)], ["REVIEW"])
        self.assertIsNone(self.refreshed(followup).closed_at)
        self.assertTrue(self.surveillance.events.filter(event_type="REVIEW_NEEDED", data__reason="issue_state_mismatch").exists())

    def test_a_replaced_derived_revision_keeps_the_historical_files(self):  # 17, 18
        record, followup = self.retained(Category.COMMENTED)
        first = fu_service.provide_file(followup, self.updater, self.file("first"))
        second = fu_service.provide_file(self.refreshed(followup), self.updater, self.file("second"))
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.refreshed(followup).derived_revision_id, second.pk)
        self.assertTrue(DocumentRevision.objects.filter(pk=first.pk).exists())
        events = list(self.surveillance.events.filter(event_type="FU_FILE").order_by("pk"))
        self.assertEqual([e.data["revision"] for e in events], [first.pk, second.pk])
        self.assertEqual(events[1].data["replaced_revision"], first.pk)
        self.assertTrue(DocumentRevision.objects.filter(pk=self.revision.pk, revision_label="A").exists())

    def test_the_approvals_of_other_documents_and_versions_are_never_blocked(self):  # 20
        record, followup = self.retained(Category.COMMENTED)
        revision = fu_service.provide_file(followup, self.updater, self.file("c-new"))
        unrelated = self.review()  # another document approved while the follow-up runs
        self.assertTrue(unrelated.revision_label)
        self.assertTrue(self.approve(revision).revision_label)


class ConflictTests(Lot2Base):
    def second_client(self):
        company = ContactCompany.objects.create(company_profile=self.company, company_name="Second Client", company_role="Client", contact_email="b@x.invalid")
        contact = ContactUser.objects.create(company_profile=self.company, contact_company=company, name="Bob", surname="Roe", email="bob@second.invalid")
        return company, contact

    def test_a_second_open_follow_up_for_the_same_document_and_client_is_a_business_refusal(self):  # 15
        record, followup = self.retained(Category.COMMENTED)
        newer = self.approve(add_document_revision(self.document, self.file("newer"), self.owner))
        self.issue_and_send([(newer, "FOR_REVIEW")])  # a second distribution -> a second surveillance, same client
        second_issue = ClientIssue.objects.get(document_revision=newer, client_company=self.client_company)
        second = self.record(second_issue, category=Category.ACCEPTED)
        with self.assertRaises(ValidationError) as error:
            self.confirm(second)
        self.assertEqual(self.codes(error), "followup_conflict")
        self.assertEqual(self.confirm_api(self.updater, {"return": second.pk}, issue_id=second_issue.pk).status_code, 400)
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(document=self.document).count(), 1)

    def test_several_clients_keep_parallel_follow_ups(self):  # 16
        other_company, other_contact = self.second_client()
        self.issue_and_send([(self.revision, "FOR_REVIEW")], company=other_company, contacts=[other_contact])
        other_issue = ClientIssue.objects.get(document_revision=self.revision, client_company=other_company)
        mine = self.ret(Category.COMMENTED)
        theirs = self.record(other_issue, category=Category.ACCEPTED)
        first = self.confirm(mine)
        second = self.confirm(theirs)
        self.assertEqual({first.client_company_id, second.client_company_id}, {self.client_company.pk, other_company.pk})
        self.assertEqual(ClientSurveillanceFollowUp.objects.filter(document=self.document, closed_at__isnull=True).count(), 2)
        self.assertEqual((first.expected_state, second.expected_state), ("PREL", "BPE"))

    def test_a_non_internal_or_closed_surveillance_is_refused_with_a_business_code(self):
        record = self.ret(Category.ACCEPTED)
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(status="CLOSED", result="REVIEWED")
        with self.assertRaises(ValidationError) as error:
            self.confirm(record)
        self.assertEqual(self.codes(error), "surveillance_closed")


class RecetteApiTests(Lot2Base):
    """Targeted recette through the HTTP views on the isolated test database: record -> retain -> file -> (validation)
    for each code, with the business refusals and their messages. No distribution, no closure (LOT 3)."""

    def record_api(self, category):
        # LOT C retains a unique return automatically by default: these recettes exercise the EXPLICIT confirmation path
        response = self.post(self.updater, {"category": category, "comment": "c" if category != "ACCEPTED" else "", "auto_retain": "false"})
        self.assertEqual(response.status_code, 201, response.data)
        return ClientSurveillanceReturn.objects.get(pk=response.data["id"])

    def row(self):
        return self.listing(self.updater).data["results"][0]

    def test_code_a_from_the_return_to_the_bpe_file(self):
        record = self.record_api("ACCEPTED")
        self.assertEqual((self.row()["followups"], self.row()["retainable_returns"]), ([], [record.pk]))  # nothing opened yet
        followup = self.confirm_api(self.updater).data  # one return: one action, no selection
        self.assertEqual((followup["next_step"], followup["expected_index"], followup["applicable_at"]), ("EN_ATTENTE_FICHIER_BPE", "A", None))
        refused = self.provide_api(self.updater, ClientSurveillanceFollowUp.objects.get(), confirm=False)
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data["code"][0], "bpe_confirmation_required")
        done = self.provide_api(self.updater, ClientSurveillanceFollowUp.objects.get())
        self.assertEqual(done.status_code, 201, done.data)
        self.assertEqual((done.data["next_step"], done.data["derived_revision"]["issue_state"], done.data["derived_revision"]["index_label"]), ("EN_ATTENTE_DIFFUSION", "BPE", "A"))
        self.assertIsNone(done.data["applicable_at"])
        self.assertEqual(self.confirm_api(self.updater, {"return": record.pk}).data["code"][0], "already_confirmed")

    def test_code_b_needs_validation_before_any_distribution(self):
        self.record_api("ACCEPTED_WITH_COMMENTS")
        followup = ClientSurveillanceFollowUp.objects.get(pk=self.confirm_api(self.updater).data["id"])
        self.assertEqual(followup.expected_index, "B")
        revision = fu_service.provide_file(followup, self.updater, self.file("b"))
        self.assertEqual(self.row()["followups"][0]["next_step"], "EN_ATTENTE_VALIDATION")
        revision = self.approve(revision)
        self.assertEqual((self.row()["followups"][0]["next_step"], revision.revision_label, revision.issue_state), ("EN_ATTENTE_DIFFUSION", "B", "BPE"))

    def test_code_c_prepares_a_prel_for_review(self):
        self.record_api("COMMENTED")
        followup = ClientSurveillanceFollowUp.objects.get(pk=self.confirm_api(self.updater).data["id"])
        revision = fu_service.provide_file(followup, self.updater, self.file("c"))
        self.assertEqual((followup.expected_state, revision.issue_state, revision.revision_label), ("PREL", "PREL", None))
        self.assertEqual(self.row()["followups"][0]["next_step"], "EN_ATTENTE_VALIDATION")
        fu_service.assert_purpose(revision, "FOR_REVIEW")
        with self.assertRaises(ValidationError):
            fu_service.assert_purpose(revision, "FOR_INFORMATION")
        self.assertEqual(self.provide_api(self.updater, followup, "c2", confirm=True).status_code, 201)  # replaceable until validated

    def test_code_d_prepares_the_same_prel_treatment(self):
        self.record_api("REJECTED")
        followup = ClientSurveillanceFollowUp.objects.get(pk=self.confirm_api(self.updater).data["id"])
        self.assertEqual((followup.expected_state, followup.expected_index), ("PREL", "B"))
        revision = fu_service.provide_file(followup, self.updater, self.file("d"))
        self.assertEqual((revision.issue_state, self.row()["followups"][0]["next_step"]), ("PREL", "EN_ATTENTE_VALIDATION"))

    def test_several_returns_force_an_explicit_selection_through_the_api(self):
        first, second = self.record_api("COMMENTED"), self.record_api("ACCEPTED_WITH_COMMENTS")
        refused = self.confirm_api(self.updater)
        self.assertEqual((refused.status_code, refused.data["code"][0]), (400, "selection_required"))
        self.assertEqual(self.confirm_api(self.updater, {"return": second.pk}).status_code, 201)
        self.assertEqual(ClientSurveillanceReturn.objects.filter(pk=first.pk).count(), 1)
