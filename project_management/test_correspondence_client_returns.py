"""SURVEILLANCE IN LOT B: ONE proof (IN correspondence) for SEVERAL documents. One operation, one transaction, one proof kept
once (never copied), per-line permissions, no duplicate on resubmission, no silent partial treatment."""
from django.core.files.base import ContentFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    ClientSurveillanceEvent, ClientSurveillanceReturn, Correspondence, CorrespondenceAttachment, Document, DocumentRevision,
)
from project_management.test_client_proof_selector import ProofBase
from project_management.views.correspondence_client_returns import CorrespondenceClientReturnCandidatesView, CorrespondenceClientReturnsView


class Lot2Base(ProofBase):
    def setUp(self):
        super().setUp()
        self.user = self.reader("lotb.user@links-local.invalid")
        self.proof_row = self.proof(subject="Suv. CLIENT", external_reference="CLIENT-SURV-7")
        for name in ("message.eml", "sheet.pdf"):
            CorrespondenceAttachment.objects.create(correspondence=self.proof_row, file=ContentFile(b"x", name=name), file_name=name, file_size=1)

    def extra_issue(self, number):
        revision = self.review()
        Document.objects.filter(pk=revision.document.pk).update(full_document_number=number)
        self.issue_and_send([(revision, "FOR_REVIEW")])
        return revision.client_issues.get(client_company=self.client_company)

    def submit(self, lines, user=None, proof=None, **extra):
        request = APIRequestFactory().post("/x/", {"lines": lines, "source_channel": "EMAIL", **extra}, format="json")
        force_authenticate(request, user=user or self.user)
        return CorrespondenceClientReturnsView.as_view()(request, correspondence_id=(proof or self.proof_row).pk)

    def search(self, user=None, proof=None, **params):
        request = APIRequestFactory().get("/x/", params)
        force_authenticate(request, user=user or self.user)
        return CorrespondenceClientReturnCandidatesView.as_view()(request, correspondence_id=(proof or self.proof_row).pk)

    def line(self, issue, category="ACCEPTED", comment=""):
        return {"client_issue_id": issue.pk, "category": category, "comment": comment}

    def linked(self, proof=None):
        return ClientSurveillanceReturn.objects.filter(source_correspondence=proof or self.proof_row, supersedes__isnull=True)


class OneOperationTests(Lot2Base):
    def test_one_proof_one_document(self):
        response = self.submit([self.line(self.client_issue)])
        self.assertEqual(response.status_code, 201, response.data)
        record = self.linked().get()
        self.assertEqual((record.client_issue_id, record.category, record.source_correspondence_id), (self.client_issue.pk, "ACCEPTED", self.proof_row.pk))

    def test_one_proof_several_documents_with_different_codes_and_distinct_comments(self):
        b, c = self.extra_issue("DOC-B-001"), self.extra_issue("DOC-C-001")
        response = self.submit([self.line(self.client_issue, "ACCEPTED", "Fine"), self.line(b, "ACCEPTED_WITH_COMMENTS", "Fix the title block"),
                                self.line(c, "COMMENTED", "Resubmit with the new layout")])
        self.assertEqual((response.status_code, response.data["created"]), (201, 3), response.data)
        rows = {r.client_issue_id: r for r in self.linked()}
        self.assertEqual({k: (v.category, v.code_snapshot, v.comment) for k, v in rows.items()},
                         {self.client_issue.pk: ("ACCEPTED", "A", "Fine"), b.pk: ("ACCEPTED_WITH_COMMENTS", "B", "Fix the title block"),
                          c.pk: ("COMMENTED", "C", "Resubmit with the new layout")})
        self.assertEqual({r.source_correspondence_id for r in rows.values()}, {self.proof_row.pk})

    def test_the_single_proof_is_never_copied_nor_recreated(self):
        b = self.extra_issue("DOC-B-002")
        before = (Correspondence.objects.count(), CorrespondenceAttachment.objects.count(), Document.objects.count(), DocumentRevision.objects.count())
        names = sorted(self.proof_row.attachments.values_list("file_name", flat=True))
        self.assertEqual(self.submit([self.line(self.client_issue), self.line(b)]).status_code, 201)
        self.assertEqual(before, (Correspondence.objects.count(), CorrespondenceAttachment.objects.count(), Document.objects.count(), DocumentRevision.objects.count()))
        self.assertEqual(names, sorted(self.proof_row.attachments.values_list("file_name", flat=True)))
        self.assertEqual(ClientSurveillanceReturn.objects.filter(client_issue__in=[self.client_issue, b], files__isnull=False).count(), 0)  # no file copied onto the returns

    def test_a_second_submit_creates_no_duplicate_and_is_safe(self):
        b = self.extra_issue("DOC-B-003")
        lines = [self.line(self.client_issue), self.line(b, "REJECTED", "No")]
        self.assertEqual(self.submit(lines).status_code, 201)
        again = self.submit(lines)
        self.assertEqual((again.status_code, again.data["created"], [r["created"] for r in again.data["results"]]), (200, 0, [False, False]))
        self.assertEqual(self.linked().count(), 2)
        # another result for an already linked document is refused, never silently ignored or duplicated
        refused = self.submit([self.line(self.client_issue, "REJECTED", "changed my mind")])
        self.assertEqual((refused.status_code, refused.data["errors"][0]["code"]), (400, "already_linked"))
        self.assertEqual(self.linked().count(), 2)

    def test_history_of_each_document_carries_the_proof(self):
        b = self.extra_issue("DOC-B-004")
        self.submit([self.line(self.client_issue), self.line(b, "REJECTED", "No")])
        for issue in (self.client_issue, b):
            event = ClientSurveillanceEvent.objects.filter(surveillance__client_issue=issue, event_type="RETURN_RECORDED").latest("pk")
            self.assertEqual((event.data["proof_correspondence"], event.data["proof_tier"]), (self.proof_row.pk, 1))
            self.assertEqual(event.actor_id, self.user.pk)


class SearchTests(Lot2Base):
    def test_search_by_reference_and_by_index_and_nothing_is_chosen(self):
        a, b = self.extra_issue("PLAN-AAA-001"), self.extra_issue("PLAN-AAA-002")
        by_ref = self.search(reference="plan aaa 001").data["results"]
        self.assertEqual([r["client_issue_id"] for r in by_ref], [a.pk])
        index = by_ref[0]["source_revision"]
        by_index = self.search(reference="PLAN-AAA", revision=index).data["results"]
        self.assertIn(a.pk, [r["client_issue_id"] for r in by_index])
        self.assertTrue(all(index in (r["source_revision"], r["client_revision"]) for r in by_index))
        self.assertTrue(all(not r["already_linked"] for r in by_index))  # nothing preselected, nothing linked

    def test_an_ambiguous_search_returns_every_candidate(self):
        a, b = self.extra_issue("PLAN-AMB-001"), self.extra_issue("PLAN-AMB-002")
        rows = self.search(reference="PLAN-AMB").data["results"]
        self.assertEqual({r["client_issue_id"] for r in rows}, {a.pk, b.pk})

    def test_a_document_never_distributed_to_the_client_is_not_a_candidate_and_cannot_be_linked(self):
        revision = self.review()
        Document.objects.filter(pk=revision.document.pk).update(full_document_number="PLAN-NODIST-001")
        self.assertEqual(self.search(reference="PLAN-NODIST").data["results"], [])
        forged = self.submit([{"client_issue_id": 999999, "category": "ACCEPTED", "comment": ""}])
        self.assertEqual((forged.status_code, forged.data["errors"][0]["code"]), (400, "not_available"))
        self.assertFalse(self.linked().exists())

    def test_without_criteria_only_the_distributions_of_the_sender_company_are_listed(self):
        rows = self.search().data["results"]
        self.assertEqual([r["client_issue_id"] for r in rows], [self.client_issue.pk])
        self.assertTrue(rows[0]["same_company"] and rows[0]["can_process"])
        self.assertEqual(self.search().data["proof"]["has_original_email"], True)

    def test_the_search_never_lists_documents_the_user_cannot_read(self):
        outsider = self.grant("lotb.outsider@links-local.invalid", self.other_project, ["documents.view", "documents.update", "documents.correspondence_view"])
        self.assertEqual(self.search(user=outsider).status_code, 404)  # not even the correspondence is revealed


class SafetyTests(Lot2Base):
    def test_wrong_company_needs_an_explicit_confirmation_for_the_whole_submit_then_is_traced(self):
        proof = self.proof(sender_company=self.acme(), subject="From Acme")
        refused = self.submit([self.line(self.client_issue)], proof=proof)
        self.assertEqual((refused.status_code, refused.data["errors"][0]["code"]), (400, "other_company_confirmation_required"))
        self.assertFalse(self.linked(proof).exists())
        accepted = self.submit([self.line(self.client_issue)], proof=proof, confirm_other_company=True)
        self.assertEqual(accepted.status_code, 201, accepted.data)
        event = ClientSurveillanceEvent.objects.filter(surveillance__client_issue=self.client_issue, event_type="RETURN_RECORDED").latest("pk")
        self.assertEqual((event.data["proof_tier"], event.data["proof_other_company_confirmed"]), (2, True))

    def test_another_type_needs_its_own_confirmation(self):
        proof = self.proof(correspondence_type="Email")
        refused = self.submit([self.line(self.client_issue)], proof=proof, confirm_other_company=True)
        self.assertEqual(refused.data["errors"][0]["code"], "other_type_confirmation_required")
        self.assertEqual(self.submit([self.line(self.client_issue)], proof=proof, confirm_other_type=True).status_code, 201)

    def test_another_project_proof_is_never_linked(self):
        elsewhere = self.proof(project=self.other_project)
        both = self.grant("lotb.both@links-local.invalid", self.other_project, ["documents.view", "documents.update", "documents.correspondence_view"], user=self.user)
        response = self.submit([self.line(self.client_issue)], user=both, proof=elsewhere)
        self.assertEqual((response.status_code, response.data["errors"][0]["code"]), (400, "not_available"))
        self.assertFalse(self.linked(elsewhere).exists())

    def test_insufficient_permissions(self):
        view_only = self.grant("lotb.viewonly@links-local.invalid", self.project, ["documents.view", "documents.correspondence_view"])
        refused = self.submit([self.line(self.client_issue)], user=view_only)
        self.assertEqual((refused.status_code, refused.data["errors"][0]["code"]), (400, "permission_denied"))
        no_correspondence = self.grant("lotb.nocorr@links-local.invalid", self.project, ["documents.view", "documents.update"])
        self.assertEqual(self.submit([self.line(self.client_issue)], user=no_correspondence).status_code, 404)
        self.assertEqual(self.search(user=no_correspondence).status_code, 404)
        self.assertFalse(self.linked().exists())

    def test_an_invalid_line_rolls_the_whole_submit_back_and_names_the_document(self):
        b = self.extra_issue("DOC-B-005")
        events = ClientSurveillanceEvent.objects.filter(event_type="RETURN_RECORDED").count()
        response = self.submit([self.line(self.client_issue, "ACCEPTED"), self.line(b, "REJECTED", "")])  # D needs comments
        self.assertEqual(response.status_code, 400)
        error = response.data["errors"][0]
        self.assertEqual((error["code"], error["client_issue_id"], error["reference"]), ("comment_required", b.pk, "DOC-B-005"))
        self.assertEqual(len(response.data["errors"]), 1)  # the valid line is not reported as an error
        self.assertFalse(ClientSurveillanceReturn.objects.filter(source_correspondence=self.proof_row).exists())
        self.assertEqual(ClientSurveillanceEvent.objects.filter(event_type="RETURN_RECORDED").count(), events)
        # the user corrects the faulty line and submits again: everything is saved once
        fixed = self.submit([self.line(self.client_issue, "ACCEPTED"), self.line(b, "REJECTED", "Not acceptable")])
        self.assertEqual((fixed.status_code, fixed.data["created"]), (201, 2))

    def test_every_faulty_line_is_reported_at_once(self):
        b, c = self.extra_issue("DOC-B-006"), self.extra_issue("DOC-C-006")
        response = self.submit([self.line(b, "REJECTED", ""), self.line(c, "COMMENTED", ""), self.line(b, "ACCEPTED")])
        self.assertEqual(sorted(e["code"] for e in response.data["errors"]), ["comment_required", "comment_required", "duplicate_line"])
        self.assertFalse(self.linked().exists())

    def test_the_return_cannot_be_earlier_than_the_send(self):
        old = (self.now() - __import__("datetime").timedelta(days=30)).isoformat()
        response = self.submit([self.line(self.client_issue)], received_at=old)
        self.assertEqual((response.status_code, response.data["errors"][0]["code"]), (400, "invalid_received_at"))
        self.assertFalse(self.linked().exists())

    def test_an_empty_or_forged_selection_is_refused(self):
        self.assertEqual(self.submit([]).status_code, 400)
        draft = self.proof(status="DRAFT")
        self.assertEqual(self.submit([self.line(self.client_issue)], proof=draft).status_code, 400)
        outgoing = self.proof(direction="OUT")
        self.assertEqual(self.submit([self.line(self.client_issue)], proof=outgoing).status_code, 400)
