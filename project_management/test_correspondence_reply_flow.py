"""LOT 3D-3C0B: Correspondence reply/response flow.

Direction reversal, scope validation and response_status derivation were ALREADY fully
implemented and tested (LOT 3B / LOT 3B stabilization -- see
CorrespondenceResponseLinkingTests in test_correspondence.py: test_in_to_in_rejected,
test_out_to_out_rejected, test_cross_project_parent_rejected,
test_in_answered_by_issued_out_response, test_out_answered_by_in_response, etc., all still
passing unchanged). This file covers only what LOT 3D-3C0B actually added:
- the response gets its own independent, normal COR reference (never derived by suffixing
  the parent's reference -- section 7);
- the two new read-only serializer fields (`responses`, `parent_reference`) backing the
  drawer's compact "Related Correspondences" area (section 12);
- a company-scope reply with reversed parties, end to end (section 5/6/21);
- parent deletion preserves response history (section 17: on_delete=SET_NULL, not CASCADE).
"""
from datetime import date

from project_management.models import Correspondence
from project_management.test_correspondence import CorrespondenceTestBase


class CorrespondenceReplyReferenceTests(CorrespondenceTestBase):
    def test_response_receives_its_own_independent_COR_reference(self):
        # LOT 3D-3C0C section 6: only OUT gets an internally generated COR reference -- the IN
        # parent here consumes no sequence at all (never rewritten later, either), so the OUT
        # child is the counter's very first allocation in this scope (00001, not 00002).
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, recipient_company=self.client_company.id,
        )
        self.assertEqual(parent.reference, "")
        self.assertTrue(child.reference.endswith("-COR-00001"))
        # Never derived by suffixing the parent's reference -- it simply has none to derive from
        # (an empty string is trivially "in" every string, so this only means anything once
        # parent.reference is confirmed blank above).

    def test_project_scope_response_shares_the_project_reference_code(self):
        # Same section 6 rule, project-scoped: the IN parent stays blank, the OUT child alone
        # allocates the project's first COR sequence number.
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, project=self.project.id,
            response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, project=self.project.id,
            recipient_company=self.client_company.id,
        )
        self.project.refresh_from_db()
        self.assertEqual(parent.reference, "")
        self.assertEqual(child.reference, f"{self.project.project_code}-COR-00001")


class CorrespondenceRelatedSerializerFieldTests(CorrespondenceTestBase):
    def test_responses_field_lists_linked_children(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, recipient_company=self.client_company.id,
        )
        from project_management.serializers.correspondence import CorrespondenceSerializer

        data = CorrespondenceSerializer(parent).data
        self.assertEqual(len(data["responses"]), 1)
        self.assertEqual(data["responses"][0]["id"], child.id)
        self.assertEqual(data["responses"][0]["reference"], child.reference)
        self.assertEqual(data["responses"][0]["direction"], "OUT")

    def test_parent_reference_field_on_the_child(self):
        # LOT 3D-3C0C section 6/8: an IN parent with neither a generated reference nor a
        # manually-entered external_reference has nothing to display -- parent_reference is
        # None (not ""), matching the frontend's existing `parentLabel || "#<id>"` fallback
        # (never "Response to undefined"). LOT 3D-3D1 pre-flight B now requires
        # external_reference for a NEW IN through the normal create path, so this "neither
        # set" case is simulated via a direct model create (a pre-existing/irregular row),
        # same pattern as the historical-fallback tests in test_correspondence_reference_
        # business_rule.py.
        from project_management.models import Correspondence

        parent = Correspondence.objects.create(
            direction="IN", status="ISSUED", subject="No reference at all",
            company_profile=self.company, response_required=True, created_by=self.owner,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, recipient_company=self.client_company.id,
        )
        from project_management.serializers.correspondence import CorrespondenceSerializer

        data = CorrespondenceSerializer(child).data
        self.assertIsNone(data["parent_reference"])

    def test_parent_reference_field_falls_back_to_the_manually_entered_external_reference(self):
        # The IN parent's own manually-entered sender reference is what shows once it has one.
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
            external_reference="ACME-2026-0042",
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, recipient_company=self.client_company.id,
        )
        from project_management.serializers.correspondence import CorrespondenceSerializer

        data = CorrespondenceSerializer(child).data
        self.assertEqual(data["parent_reference"], "ACME-2026-0042")

    def test_no_parent_no_responses_are_empty_not_missing(self):
        standalone = self.create(direction="OUT")
        from project_management.serializers.correspondence import CorrespondenceSerializer

        data = CorrespondenceSerializer(standalone).data
        self.assertIsNone(data["parent_reference"])
        self.assertEqual(data["responses"], [])


class CorrespondenceCompanyScopeReplyTests(CorrespondenceTestBase):
    """Mirrors exactly what the frontend's reply mode sends for a Company-scoped source (no
    project): reversedPartyForReply reverses sender_*/recipient_* into the response's own
    neutral field, direction flips, parent_correspondence links back -- this test proves the
    backend accepts and stores that payload correctly end to end."""

    def test_in_source_reply_reverses_sender_to_recipient_and_stays_company_scoped(self):
        source = self.create(
            direction="IN",
            sender_company=self.client_company.id,
            sender_contact=self.client_contact.id,
            response_required=True,
        )
        self.assertIsNone(source.project_id)
        self.assertEqual(source.company_profile_id, self.company.id)

        reply = self.create(
            direction="OUT",
            parent_correspondence=source.id,
            # LOT 3D-3C0B section 5: source sender -> response recipient.
            recipient_company=self.client_company.id,
            recipient_contact=self.client_contact.id,
        )
        self.assertEqual(reply.direction, "OUT")
        self.assertEqual(reply.parent_correspondence_id, source.id)
        self.assertEqual(reply.recipient_company_id, self.client_company.id)
        self.assertEqual(reply.recipient_contact_id, self.client_contact.id)
        # Stayed Company-scoped -- no Project was introduced.
        self.assertIsNone(reply.project_id)
        self.assertEqual(reply.company_profile_id, self.company.id)
        source.refresh_from_db()
        self.assertEqual(source.response_status, Correspondence.ResponseStatus.ANSWERED)


class CorrespondenceParentDeletionTests(CorrespondenceTestBase):
    def test_deleting_a_parent_preserves_its_responses_via_set_null(self):
        """Section 17: parent_correspondence is on_delete=SET_NULL (see
        models/correspondence.py) -- deleting a parent never cascades and destroys response
        history; the child survives with parent_correspondence cleared to None."""
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, recipient_company=self.client_company.id,
        )
        parent_id = parent.id
        Correspondence.objects.get(pk=parent_id).delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent_correspondence_id)
        # The child itself was never deleted.
        self.assertTrue(Correspondence.objects.filter(pk=child.id).exists())
