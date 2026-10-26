"""L3D: Supplier Surveillance Reviewers & Execution -- targeted tests only. No closure/result/
PDF/due-date/notifications: those remain later lots, deliberately not covered here.
"""

from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import SupplierSurveillance
from project_management.models.supplier_surveillance_review import (
    SupplierSurveillanceEvent,
    SupplierSurveillanceReviewerAssignment,
    SupplierSurveillanceResponse,
)
from project_management.services.supplier_surveillance import create_supplier_surveillance
from project_management.services.supplier_surveillance_review import (
    assign_reviewer,
    complete_review,
    compute_progress,
    missing_required_item_labels,
    remove_reviewer,
    save_responses,
)
from project_management.test_document_workflow import make_user
from project_management.test_supplier_surveillance import SupplierSurveillanceBase
from project_management.views.supplier_surveillance_review import (
    SupplierSurveillanceReviewViewSet,
)

Status = SupplierSurveillanceReviewerAssignment.Status
EventType = SupplierSurveillanceEvent.EventType
CheckValue = SupplierSurveillanceResponse.CheckValue


class ReviewBase(SupplierSurveillanceBase):
    def setUp(self):
        super().setUp()
        # The reviewer-managing actor must belong to a real company for
        # get_allowed_owner_queryset to resolve a useful eligible set. OriginBase's own
        # supplier_company/client_company were created BEFORE this (under the "freelancer-like,
        # user=self.creator" ownership _issuer_in_scope uses when company_id is still None) --
        # re-point them to the same company now so SUPPLIER/CLIENT document creation in this
        # test class keeps passing the issuer-in-scope check.
        company = self.make_company()
        self.creator.company = company
        self.creator.save(update_fields=["company"])
        for issuer in (self.supplier_company, self.client_company):
            issuer.company_profile = company
            issuer.user = None
            issuer.save(update_fields=["company_profile", "user"])

    def make_reviewer(self, name="reviewer", same_company=True):
        user = make_user(name)
        if same_company:
            user.company = self.make_company()
            user.save(update_fields=["company"])
        return user

    def make_surveillance(self, *, with_two_items=False, **kwargs):
        revision = self.make_supplier_revision(**kwargs)
        if with_two_items:
            template = self.make_template(with_structure=False)
            from project_management.models import (
                SupplierSurveillanceTemplateItem,
                SupplierSurveillanceTemplateSection,
            )

            section = SupplierSurveillanceTemplateSection.objects.create(
                template=template, title="Document Identification", order=1,
            )
            SupplierSurveillanceTemplateItem.objects.create(
                section=section, label="Document reference correct", item_type="CHECK",
                required=True, order=1,
            )
            SupplierSurveillanceTemplateItem.objects.create(
                section=section, label="General observations", item_type="TEXTAREA",
                required=False, order=2,
            )
        else:
            template = self.make_template()
        return create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )

    def snapshot_item_ids(self, surveillance):
        items = [
            item
            for section in surveillance.template_snapshot["sections"]
            for item in section["items"]
        ]
        return {item["label"]: item["id"] for item in items}


class ReviewerAssignmentTests(ReviewBase):
    # 1/2/3/4/5/6
    def test_assign_one_reviewer(self):
        surveillance = self.make_surveillance()
        reviewer = self.make_reviewer("alice1")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=reviewer, actor=self.creator)
        self.assertEqual(assignment.status, Status.ASSIGNED)

    def test_assign_multiple_reviewers(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice2")
        bob = self.make_reviewer("bob2")
        assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        self.assertEqual(surveillance.reviewer_assignments.count(), 2)

    def test_duplicate_reviewer_rejected(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice3")
        assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        with self.assertRaises(ValidationError):
            assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)

    def test_invalid_out_of_context_reviewer_rejected(self):
        surveillance = self.make_surveillance()
        stranger = self.make_reviewer("stranger", same_company=False)
        with self.assertRaises(ValidationError):
            assign_reviewer(surveillance=surveillance, reviewer=stranger, actor=self.creator)

    def test_remove_untouched_assigned_reviewer(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice4")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        remove_reviewer(assignment=assignment, actor=self.creator)
        self.assertEqual(surveillance.reviewer_assignments.count(), 0)

    def test_removal_blocked_after_work_starts(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice5")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        with self.assertRaises(ValidationError):
            remove_reviewer(assignment=assignment, actor=self.creator)


class NullSnapshotTests(ReviewBase):
    # 7
    def test_null_snapshot_surveillance_cannot_be_assigned_a_reviewer(self):
        revision = self.make_supplier_revision()
        surveillance = SupplierSurveillance.objects.create(
            project=revision.document.folder.project,
            document_revision=revision,
            reference="SR-LEGACY-00002",
            created_by=self.creator,
        )
        alice = self.make_reviewer("alice6")
        with self.assertRaises(ValidationError):
            assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)


class ReviewerAccessSecurityTests(ReviewBase):
    # 8/9/10
    def test_reviewer_can_access_own_assignment(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice7")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Status.IN_PROGRESS)

    def test_non_reviewer_cannot_edit_another_review(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice8")
        bob = self.make_reviewer("bob8")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        with self.assertRaises(PermissionDenied):
            save_responses(
                assignment=assignment, actor=bob,
                answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
            )

    def test_documents_update_user_cannot_answer_for_another_reviewer(self):
        # self.creator has documents.update-equivalent authority (it manages reviewers) but
        # must NOT be able to answer on alice's behalf.
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice9")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        with self.assertRaises(PermissionDenied):
            save_responses(
                assignment=assignment, actor=self.creator,
                answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
            )


class ResponseSaveTests(ReviewBase):
    # 11-20
    def setUp(self):
        super().setUp()
        self.surveillance = self.make_surveillance(with_two_items=True)
        self.alice = self.make_reviewer("alice10")
        self.assignment = assign_reviewer(
            surveillance=self.surveillance, reviewer=self.alice, actor=self.creator
        )
        self.item_ids = self.snapshot_item_ids(self.surveillance)

    def test_valid_check_compliant(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        response = self.assignment.responses.get(item_snapshot_id=self.item_ids["Document reference correct"])
        self.assertEqual(response.check_value, "COMPLIANT")

    def test_valid_check_non_compliant(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "NON_COMPLIANT"}],
        )
        response = self.assignment.responses.get(item_snapshot_id=self.item_ids["Document reference correct"])
        self.assertEqual(response.check_value, "NON_COMPLIANT")

    def test_valid_check_not_applicable(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "NOT_APPLICABLE"}],
        )
        response = self.assignment.responses.get(item_snapshot_id=self.item_ids["Document reference correct"])
        self.assertEqual(response.check_value, "NOT_APPLICABLE")

    def test_invalid_check_value_rejected(self):
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=self.assignment, actor=self.alice,
                answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "MAYBE"}],
            )

    def test_text_save(self):
        # TEXT isn't in the default two-item fixture; build a minimal one inline.
        template = self.make_template(with_structure=False)
        from project_management.models import (
            SupplierSurveillanceTemplateItem,
            SupplierSurveillanceTemplateSection,
        )

        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXT", order=1,
        )
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )
        assignment = assign_reviewer(surveillance=surveillance, reviewer=self.alice, actor=self.creator)
        item_id = surveillance.template_snapshot["sections"][0]["items"][0]["id"]
        save_responses(
            assignment=assignment, actor=self.alice,
            answers=[{"item_snapshot_id": item_id, "text_value": "ok"}],
        )
        self.assertEqual(assignment.responses.get(item_snapshot_id=item_id).text_value, "ok")

    def test_textarea_save(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["General observations"], "text_value": "Looks fine"}],
        )
        response = self.assignment.responses.get(item_snapshot_id=self.item_ids["General observations"])
        self.assertEqual(response.text_value, "Looks fine")

    def test_unknown_snapshot_item_rejected(self):
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=self.assignment, actor=self.alice,
                answers=[{"item_snapshot_id": 999999, "check_value": "COMPLIANT"}],
            )

    def test_mismatched_item_type_rejected(self):
        # The CHECK item does not accept a text_value.
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=self.assignment, actor=self.alice,
                answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "text_value": "nope"}],
            )
        # The TEXTAREA item does not accept a check_value.
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=self.assignment, actor=self.alice,
                answers=[{"item_snapshot_id": self.item_ids["General observations"], "check_value": "COMPLIANT"}],
            )

    def test_one_response_per_assignment_item(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        self.assertEqual(
            self.assignment.responses.filter(
                item_snapshot_id=self.item_ids["Document reference correct"]
            ).count(),
            1,
        )

    def test_updating_response_does_not_duplicate_row(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "NON_COMPLIANT"}],
        )
        self.assertEqual(
            self.assignment.responses.filter(
                item_snapshot_id=self.item_ids["Document reference correct"]
            ).count(),
            1,
        )
        self.assertEqual(
            self.assignment.responses.get(
                item_snapshot_id=self.item_ids["Document reference correct"]
            ).check_value,
            "NON_COMPLIANT",
        )


class StatusTransitionTests(ReviewBase):
    # 21/22/32/33/36
    def test_first_response_moves_assignment_to_in_progress(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice11")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Status.IN_PROGRESS)

    def test_first_response_moves_surveillance_to_under_review(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice12")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.DRAFT)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.UNDER_REVIEW)

    def test_one_completed_reviewer_among_multiple_does_not_trigger_ready_for_closure(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice13")
        bob = self.make_reviewer("bob13")
        a_assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=a_assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=a_assignment, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.UNDER_REVIEW)

    def test_all_reviewers_completed_triggers_ready_for_closure(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice14")
        bob = self.make_reviewer("bob14")
        a_assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        b_assignment = assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        for assignment, actor in ((a_assignment, alice), (b_assignment, bob)):
            save_responses(
                assignment=assignment, actor=actor,
                answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
            )
            complete_review(assignment=assignment, actor=actor)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.READY_FOR_CLOSURE)

    def test_multi_reviewer_full_scenario_matches_l3d_section_36(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice15")
        bob = self.make_reviewer("bob15")
        a = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        b = assign_reviewer(surveillance=surveillance, reviewer=bob, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)

        save_responses(
            assignment=a, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        a.refresh_from_db()
        surveillance.refresh_from_db()
        self.assertEqual(a.status, Status.IN_PROGRESS)
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.UNDER_REVIEW)

        complete_review(assignment=a, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.UNDER_REVIEW)

        save_responses(
            assignment=b, actor=bob,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "NON_COMPLIANT"}],
        )
        b.refresh_from_db()
        self.assertEqual(b.status, Status.IN_PROGRESS)

        complete_review(assignment=b, actor=bob)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.READY_FOR_CLOSURE)


class CompletionValidationTests(ReviewBase):
    # 23-31, 34-36
    def setUp(self):
        super().setUp()
        self.surveillance = self.make_surveillance(with_two_items=True)
        self.alice = self.make_reviewer("alice16")
        self.assignment = assign_reviewer(
            surveillance=self.surveillance, reviewer=self.alice, actor=self.creator
        )
        self.item_ids = self.snapshot_item_ids(self.surveillance)

    def test_required_check_blocks_completion_if_missing(self):
        with self.assertRaises(ValidationError):
            complete_review(assignment=self.assignment, actor=self.alice)

    def test_required_text_blocks_completion_if_blank(self):
        template = self.make_template(with_structure=False)
        from project_management.models import (
            SupplierSurveillanceTemplateItem,
            SupplierSurveillanceTemplateSection,
        )

        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXT", required=True, order=1,
        )
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )
        assignment = assign_reviewer(surveillance=surveillance, reviewer=self.alice, actor=self.creator)
        item_id = surveillance.template_snapshot["sections"][0]["items"][0]["id"]
        save_responses(assignment=assignment, actor=self.alice, answers=[{"item_snapshot_id": item_id, "text_value": "   "}])
        with self.assertRaises(ValidationError):
            complete_review(assignment=assignment, actor=self.alice)

    def test_required_textarea_blocks_completion_if_blank(self):
        template = self.make_template(with_structure=False)
        from project_management.models import (
            SupplierSurveillanceTemplateItem,
            SupplierSurveillanceTemplateSection,
        )

        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S")
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="A", item_type="TEXTAREA", required=True, order=1,
        )
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )
        assignment = assign_reviewer(surveillance=surveillance, reviewer=self.alice, actor=self.creator)
        with self.assertRaises(ValidationError):
            complete_review(assignment=assignment, actor=self.alice)

    def test_optional_item_may_remain_blank(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        # "General observations" (TEXTAREA, optional) is never answered.
        complete_review(assignment=self.assignment, actor=self.alice)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Status.COMPLETED)

    def test_explicit_completion_is_required_not_automatic(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Status.IN_PROGRESS)

    def test_completion_sets_completed_status_and_timestamp(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=self.assignment, actor=self.alice)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, Status.COMPLETED)
        self.assertIsNotNone(self.assignment.completed_at)

    def test_completed_response_becomes_read_only(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=self.assignment, actor=self.alice)
        with self.assertRaises(ValidationError):
            save_responses(
                assignment=self.assignment, actor=self.alice,
                answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "NON_COMPLIANT"}],
            )

    def test_completed_assignment_cannot_be_removed(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=self.assignment, actor=self.alice)
        with self.assertRaises(ValidationError):
            remove_reviewer(assignment=self.assignment, actor=self.creator)

    def test_result_new_revision_required_closed_at_remain_null_after_ready_for_closure(self):
        save_responses(
            assignment=self.assignment, actor=self.alice,
            answers=[{"item_snapshot_id": self.item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=self.assignment, actor=self.alice)
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.status, SupplierSurveillance.Status.READY_FOR_CLOSURE)
        self.assertIsNone(self.surveillance.result)
        self.assertIsNone(self.surveillance.new_revision_required)
        self.assertIsNone(self.surveillance.closed_at)


class AuditEventTests(ReviewBase):
    # 37-42
    def test_reviewer_assigned_event(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice17")
        assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        self.assertTrue(
            surveillance.events.filter(event_type=EventType.REVIEWER_ASSIGNED).exists()
        )

    def test_reviewer_removed_event(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice18")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        remove_reviewer(assignment=assignment, actor=self.creator)
        self.assertTrue(
            surveillance.events.filter(event_type=EventType.REVIEWER_REMOVED).exists()
        )

    def test_review_started_event(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice19")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        self.assertTrue(surveillance.events.filter(event_type=EventType.REVIEW_STARTED).exists())

    def test_response_updated_event(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice20")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        self.assertTrue(surveillance.events.filter(event_type=EventType.RESPONSE_UPDATED).exists())

    def test_review_completed_event(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice21")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=assignment, actor=alice)
        self.assertTrue(surveillance.events.filter(event_type=EventType.REVIEW_COMPLETED).exists())

    def test_ready_for_closure_event(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice22")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=assignment, actor=alice)
        self.assertTrue(
            surveillance.events.filter(event_type=EventType.READY_FOR_CLOSURE).exists()
        )

    def test_surveillance_created_event_recorded_for_new_surveillances(self):
        surveillance = self.make_surveillance()
        self.assertTrue(
            surveillance.events.filter(event_type=EventType.SURVEILLANCE_CREATED).exists()
        )

    def test_events_are_immutable(self):
        surveillance = self.make_surveillance()
        with self.assertRaises(ValidationError):
            SupplierSurveillanceEvent.objects.filter(surveillance=surveillance).update(
                event_type=EventType.REVIEWER_REMOVED
            )
        with self.assertRaises(ValidationError):
            SupplierSurveillanceEvent.objects.filter(surveillance=surveillance).delete()


class SnapshotIndependenceTests(ReviewBase):
    # 43/44
    def test_snapshot_unchanged_throughout_execution(self):
        surveillance = self.make_surveillance(with_two_items=True)
        original_snapshot = surveillance.template_snapshot
        alice = self.make_reviewer("alice23")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        complete_review(assignment=assignment, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.template_snapshot, original_snapshot)

    def test_live_template_edits_remain_irrelevant_during_execution(self):
        revision = self.make_supplier_revision()
        template = self.make_template(with_structure=False)
        from project_management.models import (
            SupplierSurveillanceTemplateItem,
            SupplierSurveillanceTemplateSection,
        )

        section = SupplierSurveillanceTemplateSection.objects.create(template=template, title="S", order=1)
        item = SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Original label", item_type="CHECK", required=True, order=1,
        )
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template
        )
        item.label = "Renamed after snapshot"
        item.save(update_fields=["label"])

        alice = self.make_reviewer("alice24")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_id = surveillance.template_snapshot["sections"][0]["items"][0]["id"]
        save_responses(assignment=assignment, actor=alice, answers=[{"item_snapshot_id": item_id, "check_value": "COMPLIANT"}])
        complete_review(assignment=assignment, actor=alice)
        surveillance.refresh_from_db()
        self.assertEqual(
            surveillance.template_snapshot["sections"][0]["items"][0]["label"], "Original label",
        )


ACCESS = "project_management.views.supplier_surveillance_review.get_register_document_or_404"
CAN_UPDATE = "project_management.views.supplier_surveillance_review.assert_can_update_document"


class ReviewApiTests(ReviewBase):
    def _request(self, document, surveillance_id, action, method="get", data=None, assignment_id=None, as_user=None):
        factory = APIRequestFactory()
        request = (
            factory.post("/x/", data or {}, format="json")
            if method == "post"
            else factory.delete("/x/") if method == "delete"
            else factory.get("/x/")
        )
        force_authenticate(request, user=as_user or self.creator)
        methods = {"post": action} if method == "post" else {"delete": action} if method == "delete" else {"get": action}
        view = SupplierSurveillanceReviewViewSet.as_view(methods, permission_classes=[])
        kwargs = {"surveillance_id": surveillance_id}
        if assignment_id is not None:
            kwargs["assignment_id"] = assignment_id
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, **kwargs)

    def test_assign_via_real_view_then_list(self):
        surveillance = self.make_surveillance()
        alice = self.make_reviewer("alice25")
        document = surveillance.document_revision.document
        response = self._request(
            document, surveillance.id, "assign_reviewer_action", method="post",
            data={"reviewer": alice.id},
        )
        self.assertEqual(response.status_code, 201)
        listed = self._request(document, surveillance.id, "list_reviewers")
        self.assertEqual(len(listed.data["reviewers"]), 1)

    def test_my_review_and_save_and_complete_via_real_view(self):
        surveillance = self.make_surveillance(with_two_items=True)
        alice = self.make_reviewer("alice26")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        document = surveillance.document_revision.document
        item_ids = self.snapshot_item_ids(surveillance)

        my_review = self._request(document, surveillance.id, "my_review", as_user=alice)
        self.assertIsNotNone(my_review.data["assignment"])

        saved = self._request(
            document, surveillance.id, "save_my_responses", method="post", as_user=alice,
            data={"answers": [{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}]},
        )
        self.assertEqual(saved.status_code, 200)

        completed = self._request(
            document, surveillance.id, "complete_my_review", method="post", as_user=alice,
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.data["status"], "COMPLETED")

    def test_events_via_real_view(self):
        surveillance = self.make_surveillance()
        document = surveillance.document_revision.document
        response = self._request(document, surveillance.id, "events")
        self.assertTrue(len(response.data["events"]) >= 1)
