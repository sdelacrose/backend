"""LOT 3D-3C0C: recompute a parent Correspondence's response_status after one of its linked
responses (children) is DELETED.

apply_response_link (create/update) only ever moves a still-pending parent FORWARD and never
rewrites an already-ANSWERED one -- by design (see correspondence_response.py). Deleting the
exact response that had advanced the parent needs the opposite: recompute from whichever
responses still remain, which can move the status BACKWARDS. This is
recompute_parent_response_status_from_responses, wired into
CorrespondenceViewSet.perform_destroy -- reusing the same per-child status mapping
(_candidate_status_for_child) and the same "no response yet" rule (initial_response_status)
apply_response_link and serializer.create() already use, never a duplicated rule set.

Uses the real DELETE endpoint (APIRequestFactory + CorrespondenceViewSet.as_view), not a raw
model .delete(), since the recompute is wired into the ViewSet's perform_destroy.
"""
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Correspondence
from project_management.services.correspondence_response import apply_response_link
from project_management.test_correspondence import (
    CorrespondenceTestBase,
    grant_correspondence_permissions,
)
from project_management.views.correspondence import CorrespondenceViewSet


class ResponseDeleteRecomputeTestBase(CorrespondenceTestBase):
    def setUp(self):
        super().setUp()
        grant_correspondence_permissions(self.owner, self.company)

    def delete_via_api(self, correspondence):
        factory = APIRequestFactory()
        request = factory.delete(f"/correspondence/{correspondence.id}")
        force_authenticate(request, user=self.owner)
        view = CorrespondenceViewSet.as_view({"delete": "destroy"})
        response = view(request, pk=correspondence.id)
        self.assertEqual(response.status_code, 204, getattr(response, "data", None))

    def create_second_response_bypassing_active_reply_check(self, parent, **data):
        """LOT 3D-3C0C section 10 now rejects a second response through the normal serializer
        create path while an earlier one is still DRAFT (validate_parent_correspondence) -- this
        recompute suite still needs a fixture with two responses ALREADY linked (to prove
        recompute_parent_response_status_from_responses picks the right one among several once
        one is deleted), so that one fixture-setup step goes around the serializer via a direct
        model create, exactly like an already-existing historical record would. apply_response_link
        is called manually since the serializer's create() would normally have done it."""
        payload = {
            "direction": "OUT", "subject": "Second response (bypasses active-reply check)",
            "parent_correspondence": parent, "company_profile": self.company, "created_by": self.owner,
            **data,
        }
        child = Correspondence.objects.create(**payload)
        apply_response_link(child)
        return child


class TestA_INParentDraftResponseDeleted(ResponseDeleteRecomputeTestBase):
    def test_in_parent_to_answer_after_draft_response_deleted(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.TO_ANSWER)
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, status=Correspondence.Status.DRAFT,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        self.delete_via_api(child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.TO_ANSWER)


class TestB_INParentIssuedResponseDeleted(ResponseDeleteRecomputeTestBase):
    def test_in_parent_to_answer_after_issued_response_deleted(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, status=Correspondence.Status.ISSUED,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.ANSWERED)

        self.delete_via_api(child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.TO_ANSWER)


class TestC_OUTParentResponseDeleted(ResponseDeleteRecomputeTestBase):
    def test_out_parent_awaiting_reply_after_response_deleted(self):
        parent = self.create(direction="OUT", response_required=True)
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.AWAITING_REPLY)
        child = self.create(
            direction="IN", sender_company=self.client_company.id, parent_correspondence=parent.id,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.ANSWERED)

        self.delete_via_api(child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.AWAITING_REPLY)


class TestD_MultipleChildrenOneDeleted(ResponseDeleteRecomputeTestBase):
    def test_remaining_child_still_determines_status_when_one_of_two_is_deleted(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        draft_child = self.create(
            direction="OUT", parent_correspondence=parent.id, status=Correspondence.Status.DRAFT,
        )
        # Section 10 would reject a second response through the normal create path while
        # draft_child is still DRAFT -- see create_second_response_bypassing_active_reply_check.
        issued_child = self.create_second_response_bypassing_active_reply_check(
            parent, status=Correspondence.Status.ISSUED,
        )
        parent.refresh_from_db()
        # The more advanced remaining response (ISSUED) wins over the DRAFT one.
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.ANSWERED)

        # Delete the ISSUED (answering) response -- the DRAFT one remains, so the parent must
        # fall back to IN_PREPARATION, NOT reset all the way to TO_ANSWER (section 3: do not
        # simply reset whenever any child is deleted -- derive from what's left).
        self.delete_via_api(issued_child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.IN_PREPARATION)

        # Now delete the only remaining response -- back to the true "no response" state.
        self.delete_via_api(draft_child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.TO_ANSWER)


class TestE_ResponseNotRequiredUnaffected(ResponseDeleteRecomputeTestBase):
    def test_response_not_required_parent_is_never_touched(self):
        parent = self.create(direction="OUT", response_required=False)
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.NOT_REQUIRED)
        # Linking a response to a parent that never required one does not change its status
        # (apply_response_link's own existing guard) -- confirmed unaffected before deleting.
        child = self.create(
            direction="IN", sender_company=self.client_company.id, parent_correspondence=parent.id,
        )
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.NOT_REQUIRED)

        self.delete_via_api(child)
        parent.refresh_from_db()
        self.assertEqual(parent.response_status, Correspondence.ResponseStatus.NOT_REQUIRED)


class TestF_DeletingParentStillPreservesChildren(ResponseDeleteRecomputeTestBase):
    """Existing regression (LOT 3D-3C0B, SET_NULL) must remain passing -- this hotfix only
    concerns child deletion affecting its surviving parent, never the reverse."""

    def test_deleting_a_parent_still_preserves_its_child_via_set_null(self):
        parent = self.create(
            direction="IN", sender_company=self.client_company.id, response_required=True,
        )
        child = self.create(
            direction="OUT", parent_correspondence=parent.id, status=Correspondence.Status.ISSUED,
        )
        self.delete_via_api(parent)
        child.refresh_from_db()
        self.assertIsNone(child.parent_correspondence_id)
        self.assertTrue(Correspondence.objects.filter(pk=child.id).exists())
