"""LOT 3D-3C0C sections 6/7/8/10: the new OUT-only reference generation rule, IN's manual
external_reference, the register's direction-aware display_reference (with historical
fallback), and the duplicate-active-reply guard -- backend enforcement, not just the frontend
gating (NewCorrespondenceDrawer.tsx/CorrespondancesRegister/index.tsx cover that side).

Reuses CorrespondenceTestBase (serializer-level create/update) and
CorrespondancesRegisterTestBase (the register's own HTTP-level `call`) rather than a third,
independently-maintained fixture -- both are already proven sound by their own suites.
"""
from project_management.models import Correspondence
from project_management.serializers.correspondence import CorrespondenceSerializer
from project_management.services.correspondence_response import validate_parent_correspondence
from project_management.test_correspondence import CorrespondenceTestBase, grant_correspondence_permissions
from project_management.test_correspondances_register import CorrespondancesRegisterTestBase
from django.core.exceptions import ValidationError


class ReferenceGenerationRuleTests(CorrespondenceTestBase):
    """Section 6/7: only OUT allocates an internally generated reference."""

    def test_out_still_generates_a_reference(self):
        correspondence = self.create(direction="OUT")
        self.assertTrue(correspondence.reference)

    def test_in_never_generates_a_reference(self):
        correspondence = self.create(direction="IN", sender_company=self.client_company.id)
        self.assertEqual(correspondence.reference, "")

    def test_in_does_not_consume_the_sequence_counter(self):
        self.create(direction="IN", sender_company=self.client_company.id)
        self.create(direction="IN", sender_company=self.client_company.id)
        out_correspondence = self.create(direction="OUT")
        # Two prior INs must not have advanced the counter -- this OUT is still the first.
        self.assertTrue(out_correspondence.reference.endswith("-COR-00001"))

    def test_external_reference_is_writable_and_persists_for_in(self):
        correspondence = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="SUPPLIER-2026-LETTER-7",
        )
        correspondence.refresh_from_db()
        self.assertEqual(correspondence.external_reference, "SUPPLIER-2026-LETTER-7")
        self.assertEqual(correspondence.reference, "")

    def test_external_reference_is_now_required_for_a_new_in(self):
        # LOT 3D-3D1 pre-flight B revises section 7's original "optional" choice to required
        # for NEW IN correspondences going forward -- DB nullability is untouched (no
        # destructive migration; historical blank rows stay valid/editable), enforced only in
        # the serializer. Uses a raw serializer call (not self.create, which auto-fills it).
        serializer = CorrespondenceSerializer(
            data={
                "direction": "IN", "subject": "Test subject",
                "sender_company": self.client_company.id,
            },
            context={"user": self.owner},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("external_reference", serializer.errors)

    def test_out_external_reference_is_never_overwritten_by_generation(self):
        # Section 7: external_reference may keep another legitimate meaning for OUT -- the
        # generated `reference` must never be copied/duplicated into it.
        correspondence = self.create(direction="OUT", external_reference="some-other-meaning")
        self.assertEqual(correspondence.external_reference, "some-other-meaning")
        self.assertNotEqual(correspondence.external_reference, correspondence.reference)


class DisplayReferenceModelPropertyTests(CorrespondenceTestBase):
    """Section 8: Correspondence.display_reference, including the historical-compatibility
    fallback (section 8's own text: never rewrite historical records, never lose what they
    already display)."""

    def test_out_display_reference_is_its_own_generated_reference(self):
        correspondence = self.create(direction="OUT")
        self.assertEqual(correspondence.display_reference, correspondence.reference)

    def test_in_display_reference_is_the_manual_external_reference(self):
        correspondence = self.create(
            direction="IN", sender_company=self.client_company.id,
            external_reference="ACME-042",
        )
        self.assertEqual(correspondence.display_reference, "ACME-042")

    def test_in_display_reference_is_blank_when_neither_is_set(self):
        # LOT 3D-3D1 pre-flight B now requires external_reference for a NEW IN through the
        # normal serializer path -- a record with neither set can now only exist as a bypass
        # (direct model create), the same "historical/irregular row" pattern used below.
        correspondence = Correspondence.objects.create(
            direction="IN", status="ISSUED", subject="No reference at all",
            company_profile=self.company, created_by=self.owner,
        )
        self.assertEqual(correspondence.display_reference, "")

    def test_historical_in_falls_back_to_its_old_generated_reference(self):
        # Simulates a row created before this lot, back when IN also got a generated
        # `reference` -- never rewritten (section 7/8), and must not go blank on display.
        historical = Correspondence.objects.create(
            reference=f"{self.company.company_code}-COR-00099",
            direction="IN",
            status="ISSUED",
            subject="Pre-existing IN correspondence",
            company_profile=self.company,
            created_by=self.owner,
        )
        self.assertEqual(historical.display_reference, f"{self.company.company_code}-COR-00099")

    def test_manual_external_reference_wins_over_a_historical_generated_one(self):
        historical = Correspondence.objects.create(
            reference=f"{self.company.company_code}-COR-00099",
            external_reference="SENDER-REAL-REF",
            direction="IN",
            status="ISSUED",
            subject="Pre-existing IN correspondence, later annotated",
            company_profile=self.company,
            created_by=self.owner,
        )
        self.assertEqual(historical.display_reference, "SENDER-REAL-REF")


class ParentReferenceSerializerDisplayTests(CorrespondenceTestBase):
    """Section 8: parent/child display in the drawer's serializer fields uses
    display_reference, never the raw internally-generated `reference` alone, and never
    surfaces None/"" as a visible "undefined"."""

    def test_parent_reference_is_none_for_a_blank_in_parent(self):
        # LOT 3D-3D1 pre-flight B: a NEW IN now requires external_reference through the normal
        # create path (self.create auto-fills it) -- this "neither reference set" case is
        # simulated via a direct model create instead, same pattern used elsewhere in this file.
        from project_management.serializers.correspondence import CorrespondenceSerializer

        parent = Correspondence.objects.create(
            direction="IN", status="ISSUED", subject="No reference at all",
            company_profile=self.company, created_by=self.owner,
        )
        child = self.create(direction="OUT", parent_correspondence=parent.id)
        data = CorrespondenceSerializer(child).data
        self.assertIsNone(data["parent_reference"])

    def test_responses_reference_uses_display_reference_for_an_in_response(self):
        from project_management.serializers.correspondence import CorrespondenceSerializer

        parent = self.create(direction="OUT", response_required=True)
        child = self.create(
            direction="IN", sender_company=self.client_company.id, parent_correspondence=parent.id,
            external_reference="THEIR-REF-9",
        )
        data = CorrespondenceSerializer(parent).data
        self.assertEqual(data["responses"][0]["reference"], "THEIR-REF-9")


class RegisterDisplayReferenceTests(CorrespondancesRegisterTestBase):
    """Section 8, register-level: the register's ONE Reference column uses display_reference
    (direction-aware), not the raw stored reference."""

    def test_out_row_shows_its_generated_reference(self):
        self.grant_both()
        corr = self.create_correspondence(direction="OUT")
        response = self.call(self.owner, {"project": self.project.id})
        row = response.data["results"][0]
        self.assertEqual(row["reference"], corr.reference)

    def test_in_row_shows_the_manual_external_reference_not_a_generated_one(self):
        self.grant_both()
        corr = self.create_correspondence(
            direction="IN", sender_company=self.client_company.id,
            external_reference="CLIENT-LETTER-3",
        )
        self.assertEqual(corr.reference, "")
        response = self.call(self.owner, {"project": self.project.id})
        row = response.data["results"][0]
        self.assertEqual(row["reference"], "CLIENT-LETTER-3")

    def test_in_row_with_no_external_reference_shows_blank_not_none_or_crash(self):
        # LOT 3D-3D1 pre-flight B: a NEW IN now requires external_reference through the normal
        # create path -- this scenario (neither set) can now only exist as a pre-existing/
        # irregular row, simulated the same way as the historical-fallback tests above.
        self.grant_both()
        Correspondence.objects.create(
            direction="IN", status="ISSUED", subject="No reference at all",
            project=self.project, created_by=self.owner,
        )
        response = self.call(self.owner, {"project": self.project.id})
        row = response.data["results"][0]
        self.assertEqual(row["reference"], "")

    def test_search_by_external_reference_finds_the_in_row(self):
        self.grant_both()
        self.create_correspondence(
            direction="IN", sender_company=self.client_company.id,
            external_reference="UNIQUE-SENDER-CODE-88",
        )
        response = self.call(
            self.owner, {"project": self.project.id, "search": "UNIQUE-SENDER-CODE-88"},
        )
        self.assertEqual(response.data["count"], 1)


class HasActiveReplyRegisterFieldTests(CorrespondancesRegisterTestBase):
    """Section 10, register-level: has_active_reply backs the register's own Reply gating."""

    def test_no_response_yet_is_not_active(self):
        self.grant_both()
        self.create_correspondence(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        response = self.call(self.owner, {"project": self.project.id})
        self.assertFalse(response.data["results"][0]["has_active_reply"])

    def test_a_draft_response_marks_the_parent_as_having_an_active_reply(self):
        self.grant_both()
        parent = self.create_correspondence(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        self.create_correspondence(
            direction="OUT", status="DRAFT", parent_correspondence=parent.id,
        )
        response = self.call(self.owner, {"project": self.project.id})
        parent_row = next(r for r in response.data["results"] if r["id"] == parent.id)
        self.assertTrue(parent_row["has_active_reply"])

    def test_an_issued_response_is_not_active(self):
        self.grant_both()
        parent = self.create_correspondence(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        self.create_correspondence(
            direction="OUT", status="ISSUED", parent_correspondence=parent.id,
        )
        response = self.call(self.owner, {"project": self.project.id})
        parent_row = next(r for r in response.data["results"] if r["id"] == parent.id)
        self.assertFalse(parent_row["has_active_reply"])

    def test_transmittal_rows_are_always_false(self):
        self.grant_both()
        self.create_transmittal_out()
        response = self.call(self.owner, {"project": self.project.id})
        row = next(r for r in response.data["results"] if r["entity_type"] == "TRANSMITTAL")
        self.assertFalse(row["has_active_reply"])


class DuplicateActiveReplyBackendEnforcementTests(CorrespondenceTestBase):
    """Section 10: backend rejection, independent of the frontend gating -- a client that
    bypasses the UI must still be refused."""

    def test_a_second_reply_is_rejected_while_the_first_is_still_draft(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        with self.assertRaises(Exception):
            self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)

    def test_a_second_reply_is_allowed_once_the_first_is_issued(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        first = self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        self.update(first, status="ISSUED")
        # No longer in progress (DRAFT) -- a further reply is not blocked by this rule.
        second = self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        self.assertIsNotNone(second.id)

    def test_a_second_reply_is_allowed_once_the_first_draft_is_deleted(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from project_management.views.correspondence import CorrespondenceViewSet

        grant_correspondence_permissions(self.owner, self.company)
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        first = self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)

        factory = APIRequestFactory()
        request = factory.delete(f"/correspondence/{first.id}")
        force_authenticate(request, user=self.owner)
        view = CorrespondenceViewSet.as_view({"delete": "destroy"})
        delete_response = view(request, pk=first.id)
        self.assertEqual(delete_response.status_code, 204)

        second = self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        self.assertIsNotNone(second.id)

    def test_editing_the_existing_draft_reply_itself_is_never_blocked_by_its_own_presence(self):
        # exclude_child_id: saving the SAME draft reply again (e.g. changing its subject, still
        # DRAFT, parent_correspondence unchanged) must never be rejected as "a second reply".
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        reply = self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        updated = self.update(reply, subject="Edited subject", parent_correspondence=parent.id)
        self.assertEqual(updated.subject, "Edited subject")

    def test_validate_parent_correspondence_raises_directly_for_an_in_progress_parent(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        self.create(direction="OUT", status="DRAFT", parent_correspondence=parent.id)
        parent.refresh_from_db()
        with self.assertRaises(ValidationError):
            validate_parent_correspondence(
                parent, "OUT",
                {"project_id": None, "company_profile_id": self.company.id, "freelancer_id": None},
            )
