"""L3E: Supplier Surveillance Closure -- targeted tests only. No PDF/due-date/notifications/
Register/Supplier Portal/Client Review/new-revision-creation: those remain later lots,
deliberately not covered here.
"""

from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import SupplierSurveillance
from project_management.models.supplier_surveillance_review import SupplierSurveillanceEvent
from project_management.services.supplier_surveillance_review import (
    assign_reviewer,
    close_supplier_surveillance,
    complete_review,
    remove_reviewer,
    save_responses,
)
from project_management.test_supplier_surveillance_review import ReviewBase, Status
from project_management.views.supplier_surveillance_review import (
    SupplierSurveillanceReviewViewSet,
)

EventType = SupplierSurveillanceEvent.EventType
Result = SupplierSurveillance.Result
SurveillanceStatus = SupplierSurveillance.Status


class ClosureBase(ReviewBase):
    def make_ready_surveillance(self, *, reviewer_names=("closealice",)):
        """A Surveillance with one required CHECK item, N Reviewers each having completed their
        review -- i.e. already at READY_FOR_CLOSURE, exactly L3E's own prerequisite.

        Every Reviewer is ASSIGNED first, before any of them completes (mirrors
        test_multi_reviewer_full_scenario_matches_l3d_section_36's own ordering) -- completing
        one Reviewer immediately flips a single-assignment Surveillance to READY_FOR_CLOSURE
        (L3D section 17), which would then block assigning the next one.
        """
        surveillance = self.make_surveillance(with_two_items=True)
        item_ids = self.snapshot_item_ids(surveillance)
        required_item_id = item_ids["Document reference correct"]
        reviewers = [self.make_reviewer(name) for name in reviewer_names]
        assignments = [
            assign_reviewer(surveillance=surveillance, reviewer=reviewer, actor=self.creator)
            for reviewer in reviewers
        ]
        for reviewer, assignment in zip(reviewers, assignments):
            save_responses(
                assignment=assignment,
                actor=reviewer,
                answers=[{"item_snapshot_id": required_item_id, "check_value": "COMPLIANT"}],
            )
            complete_review(assignment=assignment, actor=reviewer)
        surveillance.refresh_from_db()
        return surveillance, reviewers


class ClosurePreconditionTests(ClosureBase):
    # 1/2/3
    def test_cannot_close_draft(self):
        surveillance = self.make_surveillance()
        self.assertEqual(surveillance.status, SurveillanceStatus.DRAFT)
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REVIEWED, closure_comment="",
            )

    def test_cannot_close_under_review(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("closealice2")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SurveillanceStatus.UNDER_REVIEW)
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REVIEWED, closure_comment="",
            )

    def test_can_close_ready_for_closure(self):
        surveillance, _reviewers = self.make_ready_surveillance()
        self.assertEqual(surveillance.status, SurveillanceStatus.READY_FOR_CLOSURE)
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        self.assertEqual(closed.status, SurveillanceStatus.CLOSED)

    # 4/5
    def test_ready_for_closure_still_reverifies_all_reviewers_completed(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice3", "closebob3")
        )
        # Force an inconsistent state (status flipped without a real re-check) to prove closure
        # never trusts the status flag alone (L3E section 8) -- flip one assignment back.
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[1])
        assignment.status = Status.IN_PROGRESS
        assignment.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REVIEWED, closure_comment="",
            )

    def test_zero_reviewer_closure_rejected(self):
        surveillance = self.make_surveillance()
        surveillance.status = SurveillanceStatus.READY_FOR_CLOSURE
        surveillance.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REVIEWED, closure_comment="",
            )

    # 6
    def test_invalid_result_rejected(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice4",)
        )
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result="APPROVED", closure_comment="",
            )


class ReviewedClosureTests(ClosureBase):
    # 7/8/9/10/11/12/13
    def test_reviewed_closes_successfully_with_full_field_set(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice5",)
        )
        before = timezone.now()
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        self.assertEqual(closed.result, Result.REVIEWED)
        self.assertFalse(closed.new_revision_required)
        self.assertEqual(closed.status, SurveillanceStatus.CLOSED)
        self.assertEqual(closed.closed_by_id, self.creator.id)
        self.assertIsNotNone(closed.closed_at)
        self.assertGreaterEqual(closed.closed_at, before)

    def test_reviewed_comment_optional(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice6",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="   ",
        )
        self.assertEqual(closed.closure_comment, "")


class CommentedClosureTests(ClosureBase):
    # 14/15/16/17
    def test_commented_blank_comment_rejected(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice7",)
        )
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.COMMENTED, closure_comment="   ",
            )

    def test_commented_non_empty_comment_accepted(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice8",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.COMMENTED, closure_comment="Please fix the comments.",
        )
        self.assertEqual(closed.result, Result.COMMENTED)
        self.assertTrue(closed.new_revision_required)
        self.assertEqual(closed.closure_comment, "Please fix the comments.")


class RejectedClosureTests(ClosureBase):
    # 18/19/20/21
    def test_rejected_blank_comment_rejected(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice9",)
        )
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REJECTED, closure_comment="",
            )

    def test_rejected_non_empty_comment_accepted(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice10",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REJECTED, closure_comment="Does not meet requirements.",
        )
        self.assertEqual(closed.result, Result.REJECTED)
        self.assertTrue(closed.new_revision_required)


ACCESS = "project_management.views.supplier_surveillance_review.get_register_document_or_404"
CAN_UPDATE = "project_management.views.supplier_surveillance_review.assert_can_update_document"


class ClosureSecurityTests(ClosureBase):
    def _close_request(self, document, surveillance_id, data, as_user, can_update_side_effect=None):
        factory = APIRequestFactory()
        request = factory.post("/x/", data, format="json")
        force_authenticate(request, user=as_user)
        view = SupplierSurveillanceReviewViewSet.as_view(
            {"post": "close_surveillance_action"}, permission_classes=[]
        )
        can_update_patch = (
            patch(CAN_UPDATE, side_effect=can_update_side_effect)
            if can_update_side_effect
            else patch(CAN_UPDATE)
        )
        with patch(ACCESS, return_value=document), can_update_patch:
            return view(request, surveillance_id=surveillance_id)

    # 22/23
    def test_closure_rejected_for_unauthorized_actor(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice11",)
        )
        document = surveillance.document_revision.document
        response = self._close_request(
            document, surveillance.id, {"result": "REVIEWED", "closure_comment": ""},
            as_user=self.creator, can_update_side_effect=PermissionDenied("no"),
        )
        self.assertEqual(response.status_code, 403)

    def test_closure_rejected_without_documents_update_permission(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice12",)
        )
        document = surveillance.document_revision.document
        stranger = self.make_reviewer("closestranger12", same_company=False)
        response = self._close_request(
            document, surveillance.id, {"result": "REVIEWED", "closure_comment": ""},
            as_user=stranger, can_update_side_effect=PermissionDenied("no"),
        )
        self.assertEqual(response.status_code, 403)


class DoubleClosureImmutabilityTests(ClosureBase):
    # 24/25/26/27/28
    def test_double_closure_rejected(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice13",)
        )
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=surveillance, actor=self.creator,
                result=Result.REVIEWED, closure_comment="",
            )

    def test_result_closure_comment_closed_by_closed_at_cannot_change_after_closure(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice14",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        original = (closed.result, closed.closure_comment, closed.closed_by_id, closed.closed_at)

        other_actor = self.make_reviewer("closebob14")
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(
                surveillance=closed, actor=other_actor,
                result=Result.REJECTED, closure_comment="Trying to change the decision.",
            )
        closed.refresh_from_db()
        self.assertEqual(
            (closed.result, closed.closure_comment, closed.closed_by_id, closed.closed_at),
            original,
        )

    # 29/30/31/32
    def test_reviewer_cannot_save_response_after_closed(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice15",)
        )
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[0])
        item_ids = self.snapshot_item_ids(surveillance)
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=assignment, actor=reviewers[0],
                answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "NON_COMPLIANT"}],
            )

    def test_reviewer_cannot_complete_after_closed(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice16",)
        )
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[0])
        with self.assertRaises(ValidationError):
            complete_review(assignment=assignment, actor=reviewers[0])

    def test_reviewer_cannot_be_added_after_closed(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice17",)
        )
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        bob = self.make_reviewer("closebob17")
        with self.assertRaises(ValidationError):
            assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)

    def test_reviewer_cannot_be_removed_after_closed(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice18",)
        )
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[0])
        with self.assertRaises(ValidationError):
            remove_reviewer(assignment=assignment, actor=self.creator)


class ClosureEvidenceTests(ClosureBase):
    # 33/34/35
    def test_existing_responses_unchanged_by_closure(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice19",)
        )
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[0])
        before = list(assignment.responses.values("item_snapshot_id", "check_value", "text_value"))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.COMMENTED, closure_comment="Needs rework.",
        )
        assignment.refresh_from_db()
        after = list(assignment.responses.values("item_snapshot_id", "check_value", "text_value"))
        self.assertEqual(before, after)

    def test_existing_assignments_unchanged_by_closure(self):
        surveillance, reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice20",)
        )
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[0])
        before = (assignment.status, assignment.completed_at)
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        assignment.refresh_from_db()
        self.assertEqual((assignment.status, assignment.completed_at), before)

    def test_template_snapshot_unchanged_by_closure(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice21",)
        )
        original_snapshot = surveillance.template_snapshot
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REJECTED, closure_comment="Resubmit please.",
        )
        self.assertEqual(closed.template_snapshot, original_snapshot)


class ClosureAuditTests(ClosureBase):
    # 36/37/38/39/40
    def test_surveillance_closed_event_created(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice22",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.COMMENTED, closure_comment="Fix please.",
        )
        self.assertTrue(
            closed.events.filter(event_type=EventType.SURVEILLANCE_CLOSED).exists()
        )

    def test_closure_event_actor_result_and_new_revision_required_correct(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice23",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REJECTED, closure_comment="Resubmit.",
        )
        event = closed.events.get(event_type=EventType.SURVEILLANCE_CLOSED)
        self.assertEqual(event.actor_id, self.creator.id)
        self.assertEqual(event.metadata["result"], Result.REJECTED)
        self.assertTrue(event.metadata["new_revision_required"])

    def test_closure_event_is_immutable(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice24",)
        )
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=Result.REVIEWED, closure_comment="",
        )
        with self.assertRaises(ValidationError):
            closed.events.filter(event_type=EventType.SURVEILLANCE_CLOSED).update(
                event_type=EventType.REVIEWER_REMOVED
            )
        with self.assertRaises(ValidationError):
            closed.events.filter(event_type=EventType.SURVEILLANCE_CLOSED).delete()


class ClosureApiTests(ClosureBase):
    def _request(self, document, surveillance_id, data, as_user=None):
        factory = APIRequestFactory()
        request = factory.post("/x/", data, format="json")
        force_authenticate(request, user=as_user or self.creator)
        view = SupplierSurveillanceReviewViewSet.as_view(
            {"post": "close_surveillance_action"}, permission_classes=[]
        )
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, surveillance_id=surveillance_id)

    def test_close_via_real_view_returns_updated_summary(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice25",)
        )
        document = surveillance.document_revision.document
        response = self._request(
            document, surveillance.id, {"result": "COMMENTED", "closure_comment": "Please revise."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        self.assertEqual(response.data["result"], "COMMENTED")
        self.assertTrue(response.data["new_revision_required"])
        self.assertEqual(response.data["closure_comment"], "Please revise.")
        self.assertIsNotNone(response.data["closed_by"])

    def test_close_via_real_view_blocked_without_comment_for_commented(self):
        surveillance, _reviewers = self.make_ready_surveillance(
            reviewer_names=("closealice26",)
        )
        document = surveillance.document_revision.document
        response = self._request(
            document, surveillance.id, {"result": "COMMENTED", "closure_comment": ""},
        )
        self.assertEqual(response.status_code, 400)
