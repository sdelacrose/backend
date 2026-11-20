"""LOT 2: a supplier review that is really finished always reaches READY_FOR_CLOSURE by itself
(no manual step), including the two cases the previous rule left waiting. Targeted tests only."""

from project_management.models import (
    SupplierSurveillance,
    SupplierSurveillanceTemplateItem,
    SupplierSurveillanceTemplateSection,
)
from project_management.models.supplier_surveillance_review import SupplierSurveillanceEvent
from project_management.services.supplier_surveillance import create_supplier_surveillance
from project_management.services.supplier_surveillance_review import (
    assign_reviewer,
    complete_review,
    remove_reviewer,
    save_responses,
)
from project_management.test_supplier_surveillance_review import ReviewBase

Status = SupplierSurveillance.Status
EventType = SupplierSurveillanceEvent.EventType


class FinishedReviewReachesReadyTests(ReviewBase):
    def optional_only_surveillance(self):
        revision = self.make_supplier_revision()
        template = self.make_template(with_structure=False)
        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="Notes", order=1)
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Free remarks", item_type="TEXTAREA", required=False, order=1,
        )
        return create_supplier_surveillance(document_revision=revision, actor=self.creator, template=template)

    def test_completing_without_any_saved_answer_still_reaches_ready_from_draft(self):
        surveillance = self.optional_only_surveillance()
        alice = self.make_reviewer("lot2alice")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.DRAFT)
        complete_review(assignment=assignment, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.READY_FOR_CLOSURE)
        self.assertEqual(surveillance.events.filter(event_type=EventType.READY_FOR_CLOSURE).count(), 1)

    def test_removing_the_only_untouched_reviewer_after_the_others_finished_reaches_ready(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("lot2alice2")
        bob = self.make_reviewer("lot2bob2")
        a_assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        b_assignment = assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=a_assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=a_assignment, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.UNDER_REVIEW)  # bob has not started
        remove_reviewer(assignment=b_assignment, actor=self.creator)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.READY_FOR_CLOSURE)
        self.assertEqual(surveillance.events.filter(event_type=EventType.READY_FOR_CLOSURE).count(), 1)

    def test_removing_a_reviewer_never_makes_a_review_ready_while_another_is_unfinished(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("lot2alice3")
        bob = self.make_reviewer("lot2bob3")
        carol = self.make_reviewer("lot2carol3")
        a_assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        c_assignment = assign_reviewer(surveillance=surveillance, reviewer=carol, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=a_assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=a_assignment, actor=alice)
        remove_reviewer(assignment=c_assignment, actor=self.creator)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.UNDER_REVIEW)  # bob is still assigned and unfinished

    def test_removing_the_last_reviewer_never_makes_a_review_without_reviewers_ready(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("lot2alice4")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        remove_reviewer(assignment=assignment, actor=self.creator)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, Status.DRAFT)
