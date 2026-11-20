"""SURVEILLANCE IN LOT A: the source-proof selector. Ranked candidates, explicit confirmations, no silent exclusion by company/type,
one proof for several documents, no duplicate on the same return, project / permission limits, trace in the history."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile

from project_management.models import ContactCompany, Correspondence, CorrespondenceAttachment
from project_management.services.client_surveillance_return import eligible_surveillance_correspondences
from project_management.test_client_surveillance_return import ReturnBase


class ProofBase(ReturnBase):
    def proof(self, **overrides):
        values = dict(project=self.project, direction="IN", status="ISSUED", correspondence_type="Surveillance", sender_company=self.client_company,
                      subject="Client review sheet", reference="")
        values.update(overrides)
        return Correspondence.objects.create(**values)

    def acme(self, name="Acme Test Ltd"):
        return ContactCompany.objects.create(company_profile=self.company, company_name=name, company_role="Client", contact_email="a@x.invalid")

    def code(self, error):
        return error.exception.message_dict.get("code", [None])[0]

    def reader(self, email="proof.user@links-local.invalid"):
        return self.grant(email, self.project, ["documents.view", "documents.update", "documents.correspondence_view"])

    def sources(self, user, issue=None):
        document = issue.document_revision.document if issue is not None else self.document
        rows = self.listing(user, document=document).data["results"]
        row = next(r for r in rows if r["client_issue_id"] == (issue or self.client_issue).pk)
        return row["surveillance_correspondences"]


class RankingTests(ProofBase):
    def test_candidates_are_ranked_surveillance_of_the_client_then_other_surveillance_then_other_in(self):
        other = self.acme()
        third = self.proof(subject="newsletter", correspondence_type="Email")
        second = self.proof(subject="from Acme", sender_company=other)
        first = self.proof(subject="from the client")
        untyped = self.proof(subject="untyped", correspondence_type=None, sender_company=other)
        ranked = list(eligible_surveillance_correspondences(self.client_issue))
        self.assertEqual([c.pk for c in ranked][:1], [first.pk])
        self.assertEqual({c.pk: c.proof_tier for c in ranked}, {first.pk: 1, second.pk: 2, third.pk: 3, untyped.pk: 3})
        self.assertEqual(ranked[1].pk, second.pk)

    def test_project_direction_and_status_stay_hard_limits(self):
        for overrides in (dict(project=self.other_project), dict(direction="OUT"), dict(status="DRAFT")):
            bad = self.proof(**overrides)
            self.assertFalse(eligible_surveillance_correspondences(self.client_issue).filter(pk=bad.pk).exists(), overrides)
            with self.assertRaises(ValidationError) as error:
                self.record(source_correspondence_id=bad.pk, confirm_other_company=True, confirm_other_type=True)
            self.assertEqual(self.code(error), "unknown_proof")

    def test_the_user_documents_59_and_60_case_a_surveillance_from_another_company_is_listed_with_its_flags(self):
        acme = self.acme()
        proof = self.proof(subject="Suv. CLIENT LINKS", sender_company=acme, external_reference="CLIENT-SURV-0001")
        newsletter = self.proof(subject="Access India's Rapidly Expanding Energy Market")  # same company, unrelated: identified, never auto-selected
        user = self.reader()
        rows = {r["id"]: r for r in self.sources(user)}
        self.assertEqual((rows[proof.pk]["tier"], rows[proof.pk]["same_company"], rows[proof.pk]["sender_company_name"], rows[proof.pk]["is_surveillance"]),
                         (2, False, "Acme Test Ltd", True))
        self.assertEqual(rows[newsletter.pk]["tier"], 1)
        self.assertEqual(rows[proof.pk]["reference"], "CLIENT-SURV-0001")


class ConfirmationTests(ProofBase):
    def test_another_company_needs_an_explicit_confirmation_and_is_traced(self):
        other = self.acme()
        proof = self.proof(sender_company=other)
        with self.assertRaises(ValidationError) as error:
            self.record(source_correspondence_id=proof.pk)
        self.assertEqual(self.code(error), "other_company_confirmation_required")
        self.assertFalse(proof.client_surveillance_returns.exists())
        record = self.record(source_correspondence_id=proof.pk, confirm_other_company=True)
        self.assertEqual(record.source_correspondence_id, proof.pk)
        event = self.surveillance.events.filter(event_type="RETURN_RECORDED").latest("pk")
        self.assertEqual((event.data["proof_correspondence"], event.data["proof_tier"], event.data["proof_sender_company"], event.data["proof_client_company"]),
                         (proof.pk, 2, other.pk, self.client_company.pk))
        self.assertEqual((event.data["proof_other_company_confirmed"], event.data["proof_other_type_confirmed"]), (True, False))

    def test_another_type_is_selected_explicitly_and_never_considered_a_surveillance(self):
        proof = self.proof(correspondence_type="Email")
        with self.assertRaises(ValidationError) as error:
            self.record(source_correspondence_id=proof.pk)
        self.assertEqual(self.code(error), "other_type_confirmation_required")
        self.record(source_correspondence_id=proof.pk, confirm_other_type=True)
        event = self.surveillance.events.filter(event_type="RETURN_RECORDED").latest("pk")
        self.assertEqual((event.data["proof_tier"], event.data["proof_other_type_confirmed"], event.data["proof_other_company_confirmed"]), (3, True, False))
        proof.refresh_from_db()
        self.assertEqual(proof.correspondence_type, "Email")  # the correspondence itself is never retyped

    def test_both_differences_need_both_confirmations(self):
        proof = self.proof(correspondence_type="Letter", sender_company=self.acme())
        with self.assertRaises(ValidationError) as error:
            self.record(source_correspondence_id=proof.pk, confirm_other_company=True)
        self.assertEqual(self.code(error), "other_type_confirmation_required")
        with self.assertRaises(ValidationError) as error:
            self.record(source_correspondence_id=proof.pk, confirm_other_type=True)
        self.assertEqual(self.code(error), "other_company_confirmation_required")
        self.record(source_correspondence_id=proof.pk, confirm_other_company=True, confirm_other_type=True)

    def test_a_confirmation_is_never_traced_when_nothing_differs(self):
        proof = self.proof()
        self.record(source_correspondence_id=proof.pk, confirm_other_company=True, confirm_other_type=True)
        event = self.surveillance.events.filter(event_type="RETURN_RECORDED").latest("pk")
        self.assertEqual((event.data["proof_tier"], event.data["proof_other_company_confirmed"], event.data["proof_other_type_confirmed"]), (1, False, False))

    def test_the_api_takes_the_confirmations_and_answers_a_business_code(self):
        proof = self.proof(sender_company=self.acme())
        user = self.reader()
        refused = self.post(user, {"source_correspondence": proof.pk})
        self.assertEqual((refused.status_code, refused.data["code"][0]), (400, "other_company_confirmation_required"))
        accepted = self.post(user, {"source_correspondence": proof.pk, "confirm_other_company": "true"})
        self.assertEqual(accepted.status_code, 201, accepted.data)
        self.assertEqual(accepted.data["source_correspondence"]["id"], proof.pk)


class SeveralDocumentsTests(ProofBase):
    def test_one_proof_stays_selectable_for_every_document_and_only_the_same_return_is_protected(self):
        other = self.acme()
        proof = self.proof(sender_company=other)
        second_revision = self.review()
        self.issue_and_send([(second_revision, "FOR_REVIEW")])
        second_issue = second_revision.client_issues.get(client_company=self.client_company)
        user = self.reader()
        self.assertIn(proof.pk, [s["id"] for s in self.sources(user)])
        self.assertIn(proof.pk, [s["id"] for s in self.sources(user, second_issue)])
        self.record(source_correspondence_id=proof.pk, confirm_other_company=True)
        # another document: still listed, now flagged as already linked elsewhere, and can be linked again
        again = {s["id"]: s for s in self.sources(user, second_issue)}
        self.assertEqual(again[proof.pk]["linked_documents"], 1)
        self.record(issue=second_issue, source_correspondence_id=proof.pk, confirm_other_company=True)
        self.assertEqual(proof.client_surveillance_returns.count(), 2)
        # the same return is never duplicated and the proof disappears from this document's list
        with self.assertRaises(ValidationError):
            self.record(source_correspondence_id=proof.pk, confirm_other_company=True)
        self.assertNotIn(proof.pk, [s["id"] for s in self.sources(user)])
        self.assertEqual(proof.client_surveillance_returns.count(), 2)  # no duplicate


class PermissionAndDisplayTests(ProofBase):
    def test_a_correspondence_the_user_cannot_read_is_neither_listed_nor_linkable(self):
        proof = self.proof(sender_company=self.acme())
        no_read = self.grant("proof.noread@links-local.invalid", self.project, ["documents.view", "documents.update"])
        self.assertNotIn(proof.pk, [s["id"] for s in self.sources(no_read)])
        response = self.post(no_read, {"source_correspondence": proof.pk, "confirm_other_company": "true"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(proof.client_surveillance_returns.exists())

    def test_without_the_right_to_process_the_document_nothing_is_offered(self):
        proof = self.proof()
        reader = self.grant("proof.viewonly@links-local.invalid", self.project, ["documents.view", "documents.correspondence_view"])
        row = self.listing(reader).data["results"][0]
        self.assertNotIn("surveillance_correspondences", row)
        self.assertEqual(self.post(reader, {"source_correspondence": proof.pk}).status_code, 403)

    def test_another_project_is_never_opened(self):
        elsewhere = self.proof(project=self.other_project, sender_company=self.acme())
        user = self.reader()
        self.assertNotIn(elsewhere.pk, [s["id"] for s in self.sources(user)])
        self.assertEqual(self.post(user, {"source_correspondence": elsewhere.pk, "confirm_other_company": "true"}).status_code, 400)

    def test_the_options_show_what_is_needed_to_recognise_the_proof(self):
        proof = self.proof(sender_company=self.acme(), subject="Fiche")
        CorrespondenceAttachment.objects.create(correspondence=proof, file=ContentFile(b"eml", name="message.eml"), file_name="message.eml", file_size=3)
        for index in (1, 2):
            CorrespondenceAttachment.objects.create(correspondence=proof, file=ContentFile(b"pdf", name=f"a{index}.pdf"), file_name=f"a{index}.pdf", file_size=3)
        row = {s["id"]: s for s in self.sources(self.reader())}[proof.pk]
        self.assertEqual((row["has_original_email"], row["attachment_count"], row["correspondence_type"], row["sender_company_name"], row["subject"]),
                         (True, 2, "Surveillance", "Acme Test Ltd", "Fiche"))
