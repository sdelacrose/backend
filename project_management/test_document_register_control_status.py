"""L3F: Document Register Control Status -- Internal / Supplier / Client, each independent and
derived (never stored, L3F section 25/39). Targeted tests only: no PDF/notifications/Client
Review/applicability/supersession, deliberately not covered here.
"""

from django.core.files.uploadedfile import SimpleUploadedFile

from project_management.models import Document
from project_management.models.document_workflow import DocumentWorkflow
from project_management.models.transmittal import Transmittal
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_register_status import (
    ClientControlStatus,
    InternalControlStatus,
    SupplierControlStatus,
    annotate_register_control_status,
    compute_client_control_status,
    compute_internal_control_status,
    compute_supplier_control_status,
    filter_by_client_control_status,
    filter_by_internal_control_status,
    filter_by_supplier_control_status,
)
from project_management.services.document_revision import add_document_revision
from project_management.services.document_workflow import perform_action, start_workflow
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.supplier_surveillance_review import (
    assign_reviewer,
    close_supplier_surveillance,
    complete_review,
    save_responses,
)
from project_management.test_document_workflow import make_user
from project_management.test_supplier_surveillance_review import ReviewBase

TWO = DocumentWorkflow.WorkflowType.TWO_STEP
THREE = DocumentWorkflow.WorkflowType.THREE_STEP
WFStatus = DocumentWorkflow.Status


class ControlStatusBase(ReviewBase):
    """Reuses ReviewBase (SupplierSurveillance + reviewer execution + closure, L3B-E) for a
    real company/project/folder/creator already wired for every origin's eligibility checks."""

    def internal_revision_with_category(self, category=None):
        revision = self.make_internal_revision()
        if category is not None:
            revision.document.transmittal_category = category
            revision.document.save(update_fields=["transmittal_category"])
        return revision

    def supplier_revision(self, category="FOR_REVIEW", client_category=None, **extra):
        revision = self.make_supplier_revision(category=category, **extra)
        if client_category is not None:
            revision.document.transmittal_category = client_category
            revision.document.save(update_fields=["transmittal_category"])
        return revision

    def start_and_approve(self, revision, workflow_type=TWO):
        author = make_user(f"author{revision.pk}")
        approver = make_user(f"approver{revision.pk}")
        workflow = start_workflow(revision, workflow_type, author, None, approver, self.creator)
        perform_action(workflow, author, "SIGN", "signed")
        workflow = perform_action(workflow, approver, "APPROVE", "approved")
        revision.refresh_from_db()
        revision.document.refresh_from_db()
        return workflow

    def make_ready_surveillance(self, revision, *, reviewer_names=("ctlalice",)):
        surveillance = self._surveillance_for(revision)
        reviewers = [self.make_reviewer(name) for name in reviewer_names]
        assignments = [
            assign_reviewer(surveillance=surveillance, reviewer=reviewer, actor=self.creator)
            for reviewer in reviewers
        ]
        item_ids = self.snapshot_item_ids(surveillance)
        required_id = item_ids["Document reference correct"]
        for reviewer, assignment in zip(reviewers, assignments):
            save_responses(
                assignment=assignment, actor=reviewer,
                answers=[{"item_snapshot_id": required_id, "check_value": "COMPLIANT"}],
            )
            complete_review(assignment=assignment, actor=reviewer)
        surveillance.refresh_from_db()
        return surveillance

    def _surveillance_for(self, revision):
        from project_management.services.supplier_surveillance import create_supplier_surveillance

        template = self.make_template(with_structure=False)
        from project_management.models import (
            SupplierSurveillanceTemplateItem,
            SupplierSurveillanceTemplateSection,
        )

        section = SupplierSurveillanceTemplateSection.objects.create(
            template=template, title="S", order=1,
        )
        SupplierSurveillanceTemplateItem.objects.create(
            section=section, label="Document reference correct", item_type="CHECK",
            required=True, order=1,
        )
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

    # --- Client OUT-transmission evidence ------------------------------------------------

    def issue_out_transmittal(self, revision, purpose="FOR_REVIEW"):
        """A real, properly-finalized OUT transmittal -- L3F.1 extended
        services.transmittal_eligibility.validate_item_eligibility to also accept an eligible
        SUPPLIER-origin revision (its own exact-revision SupplierSurveillance CLOSED+REVIEWED,
        or FOR_INFORMATION's no-mandatory-Surveillance semantic), so this single helper now
        covers both origins through the real application flow -- no more ORM-only bypass."""
        transmittal = Transmittal.objects.create(
            project=self.project, direction=Transmittal.Direction.OUT,
            status=Transmittal.Status.DRAFT, external_company=self.client_company,
            created_by=self.creator,
        )
        add_transmittal_item(transmittal, revision, purpose, self.creator)
        finalize_transmittal(transmittal, self.creator)
        return transmittal

    def control_statuses(self, document, *, annotated=False):
        if annotated:
            document = annotate_register_control_status(
                Document.objects.filter(pk=document.pk)
            ).get()
        serializer = DocumentSerializer(
            document, context={"user": self.creator, "include_control": True}
        )
        data = serializer.data
        return (
            data["internal_control_status"],
            data["supplier_control_status"],
            data["client_control_status"],
        )


# --- Internal ------------------------------------------------------------------------------


class InternalControlStatusTests(ControlStatusBase):
    # 1
    def test_internal_no_main_file_is_draft(self):
        document = self.create_fileless(document_origin="INTERNAL")
        self.assertEqual(
            compute_internal_control_status(document), InternalControlStatus.DRAFT
        )

    # 2
    def test_internal_main_file_no_workflow_is_in_progress(self):
        revision = self.make_internal_revision()
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.IN_PROGRESS
        )

    # 3
    def test_internal_workflow_waiting_author_is_to_review(self):
        revision = self.make_internal_revision()
        start_workflow(revision, THREE, make_user("a1"), make_user("v1"), make_user("p1"), self.creator)
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.TO_REVIEW
        )

    # 4
    def test_internal_three_step_author_complete_is_to_verify(self):
        revision = self.make_internal_revision()
        author, verifier, approver = make_user("a2"), make_user("v2"), make_user("p2")
        workflow = start_workflow(revision, THREE, author, verifier, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.TO_VERIFY
        )

    # 5
    def test_internal_two_step_author_complete_is_to_approve(self):
        revision = self.make_internal_revision()
        author, approver = make_user("a3"), make_user("p3")
        workflow = start_workflow(revision, TWO, author, None, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.TO_APPROVE
        )

    # 6
    def test_internal_three_step_verifier_complete_is_to_approve(self):
        revision = self.make_internal_revision()
        author, verifier, approver = make_user("a4"), make_user("v4"), make_user("p4")
        workflow = start_workflow(revision, THREE, author, verifier, approver, self.creator)
        workflow = perform_action(workflow, author, "SIGN", "s")
        workflow = perform_action(workflow, verifier, "VALIDATE", "v")
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.TO_APPROVE
        )

    # 7
    def test_internal_final_approved(self):
        revision = self.make_internal_revision()
        self.start_and_approve(revision)
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.APPROVED
        )

    def test_internal_rejected(self):
        revision = self.make_internal_revision()
        author, approver = make_user("a5"), make_user("p5")
        workflow = start_workflow(revision, TWO, author, None, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        perform_action(workflow, approver, "REJECT", "no")
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.REJECTED
        )

    # 8/9: current revision only, older approved revision never overrides
    def test_internal_only_current_revision_counts(self):
        revision = self.make_internal_revision()
        self.start_and_approve(revision)
        # A newer revision with no workflow yet must show IN_PROGRESS, not APPROVED.

        add_document_revision(
            revision.document, SimpleUploadedFile("v2.pdf", b"v2"), self.creator
        )
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_internal_control_status(revision.document), InternalControlStatus.IN_PROGRESS
        )

    def test_non_internal_document_is_none(self):
        revision = self.make_supplier_revision()
        self.assertIsNone(compute_internal_control_status(revision.document))

    def test_internal_control_status_annotated_matches_unannotated(self):
        revision = self.make_internal_revision()
        author, verifier, approver = make_user("a6"), make_user("v6"), make_user("p6")
        workflow = start_workflow(revision, THREE, author, verifier, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        annotated = annotate_register_control_status(
            Document.objects.filter(pk=revision.document_id)
        ).get()
        self.assertEqual(
            compute_internal_control_status(annotated), InternalControlStatus.TO_VERIFY
        )


# --- Supplier --------------------------------------------------------------------------------


class SupplierControlStatusTests(ControlStatusBase):
    # 1
    def test_for_review_no_surveillance_is_to_monitor(self):
        revision = self.supplier_revision()
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.TO_MONITOR
        )

    # 2/3
    def test_surveillance_draft_is_in_progress(self):
        revision = self.supplier_revision()
        self._surveillance_for(revision)
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.IN_PROGRESS
        )

    def test_surveillance_under_review_is_in_progress(self):
        revision = self.supplier_revision()
        surveillance = self._surveillance_for(revision)
        alice = self.make_reviewer("ctlbob")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=alice, actor=self.creator)
        item_ids = self.snapshot_item_ids(surveillance)
        save_responses(
            assignment=assignment, actor=alice,
            answers=[{"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"}],
        )
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.IN_PROGRESS
        )

    # 4
    def test_surveillance_ready_for_closure_is_to_close(self):
        revision = self.supplier_revision()
        self.make_ready_surveillance(revision, reviewer_names=("ctlcarol",))
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.TO_CLOSE
        )

    # 5/6/7
    def test_closed_reviewed(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctldave",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.REVIEWED
        )

    def test_closed_commented(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctleve",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="COMMENTED",
            closure_comment="fix please",
        )
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.COMMENTED
        )

    def test_closed_rejected(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlfrank",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REJECTED",
            closure_comment="no good",
        )
        self.assertEqual(
            compute_supplier_control_status(revision.document), SupplierControlStatus.REJECTED
        )

    # 8
    def test_for_information_without_surveillance_is_none(self):
        revision = self.supplier_revision(category="FOR_INFORMATION")
        self.assertIsNone(compute_supplier_control_status(revision.document))

    # 9/10: current revision only
    def test_older_reviewed_surveillance_does_not_override_newer_revision(self):
        revision_a = self.make_supplier_revision(category="FOR_REVIEW")
        surveillance_a = self.make_ready_surveillance(revision_a, reviewer_names=("ctlgail",))
        close_supplier_surveillance(
            surveillance=surveillance_a, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        document = revision_a.document

        add_document_revision(document, SimpleUploadedFile("revB.pdf", b"revB"), self.creator)
        document.refresh_from_db()
        # Rev B has no Surveillance of its own yet, and the Document's own
        # supplier_transmittal_category remains FOR_REVIEW -> TO_MONITOR, never REVIEWED.
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.TO_MONITOR
        )

    # --- L3F.2 correction A: no physical main file at all --------------------------------------

    def test_supplier_no_main_file_is_draft(self):
        document = self.create_fileless(
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_REVIEW",
        )
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.DRAFT
        )

    def test_supplier_no_main_file_is_draft_even_when_for_information(self):
        # L3F.2 section 7: DRAFT regardless of category -- never "To Review" merely because
        # metadata was preconfigured before the physical file arrived.
        document = self.create_fileless(
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_INFORMATION",
        )
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.DRAFT
        )

    def test_supplier_becomes_to_monitor_once_main_file_attached(self):
        document = self.create_fileless(
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_REVIEW",
        )
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.DRAFT
        )
        self.upload_first_file(document, external_revision="A")
        document.refresh_from_db()
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.TO_MONITOR
        )


# --- Client ----------------------------------------------------------------------------


class ClientControlStatusInternalTests(ControlStatusBase):
    def test_not_approved_is_not_to_issue(self):
        revision = self.internal_revision_with_category("For review")
        self.assertIsNone(compute_client_control_status(revision.document))

    def test_approved_for_review_not_transmitted_is_to_issue(self):
        revision = self.internal_revision_with_category("For review")
        self.start_and_approve(revision)
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.TO_ISSUE
        )

    def test_approved_for_information_not_transmitted_is_to_issue(self):
        revision = self.internal_revision_with_category("For information")
        self.start_and_approve(revision)
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.TO_ISSUE
        )

    def test_approved_for_information_transmitted_is_issued(self):
        revision = self.internal_revision_with_category("For information")
        self.start_and_approve(revision)
        self.issue_out_transmittal(revision, purpose="FOR_INFORMATION")
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.ISSUED
        )

    def test_approved_for_review_transmitted_is_under_review(self):
        revision = self.internal_revision_with_category("For review")
        self.start_and_approve(revision)
        self.issue_out_transmittal(revision, purpose="FOR_REVIEW")
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.UNDER_REVIEW
        )


class ClientControlStatusSupplierTests(ControlStatusBase):
    def _assert_not_to_issue(self, revision):
        revision.document.transmittal_category = "For review"
        revision.document.save(update_fields=["transmittal_category"])
        self.assertIsNone(compute_client_control_status(revision.document))

    def test_to_monitor_is_not_to_issue(self):
        self._assert_not_to_issue(self.supplier_revision())

    def test_draft_no_main_file_is_not_to_issue_even_for_information(self):
        # L3F.2 section 8: not physically distributable yet -- blocks the Client gate
        # regardless of category, even FOR_INFORMATION's own no-mandatory-Surveillance gate.
        document = self.create_fileless(
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_INFORMATION",
            transmittal_category="For information",
        )
        self.assertEqual(
            compute_supplier_control_status(document), SupplierControlStatus.DRAFT
        )
        self.assertIsNone(compute_client_control_status(document))

    def test_in_progress_is_not_to_issue(self):
        revision = self.supplier_revision()
        self._surveillance_for(revision)
        self._assert_not_to_issue(revision)

    def test_to_close_is_not_to_issue(self):
        revision = self.supplier_revision()
        self.make_ready_surveillance(revision, reviewer_names=("ctlhank",))
        self._assert_not_to_issue(revision)

    def test_commented_is_not_to_issue(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlirene",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="COMMENTED", closure_comment="x",
        )
        self._assert_not_to_issue(revision)

    def test_rejected_is_not_to_issue(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctljack",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REJECTED", closure_comment="x",
        )
        self._assert_not_to_issue(revision)

    def test_reviewed_for_review_not_transmitted_is_to_issue(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlkate",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        revision.document.transmittal_category = "For review"
        revision.document.save(update_fields=["transmittal_category"])
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.TO_ISSUE
        )

    def test_reviewed_for_review_transmitted_is_under_review(self):
        # L3F.1: real Transmittal flow now accepts this eligible Supplier revision -- no more
        # ORM-only bypass (section 23).
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlleo",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        revision.document.transmittal_category = "For review"
        revision.document.save(update_fields=["transmittal_category"])
        self.issue_out_transmittal(revision, purpose="FOR_REVIEW")
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.UNDER_REVIEW
        )

    def test_reviewed_for_information_transmitted_is_issued(self):
        # L3F.1: real Transmittal flow now accepts this eligible Supplier revision -- no more
        # ORM-only bypass (section 23).
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlmona",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        revision.document.transmittal_category = "For information"
        revision.document.save(update_fields=["transmittal_category"])
        self.issue_out_transmittal(revision, purpose="FOR_INFORMATION")
        revision.document.refresh_from_db()
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.ISSUED
        )

    def test_for_information_gate_satisfied_without_surveillance(self):
        # L3F section 19: no mandatory Surveillance for Supplier FOR_INFORMATION -- the Client
        # gate is already considered satisfied by that existing semantic.
        revision = self.supplier_revision(category="FOR_INFORMATION", client_category="For review")
        self.assertEqual(
            compute_client_control_status(revision.document), ClientControlStatus.TO_ISSUE
        )


class ClientOriginTests(ControlStatusBase):
    def test_client_origin_is_always_none(self):
        revision = self.make_client_revision()
        revision.document.transmittal_category = "For review"
        revision.document.save(update_fields=["transmittal_category"])
        self.assertIsNone(compute_client_control_status(revision.document))


# --- Exact-revision transmission regression (L3F section 37) ---------------------------------


class ExactRevisionTransmissionTests(ControlStatusBase):
    def test_rev_b_not_considered_sent_merely_because_rev_a_was(self):
        revision_a = self.internal_revision_with_category("For review")
        self.start_and_approve(revision_a)
        self.issue_out_transmittal(revision_a, purpose="FOR_REVIEW")

        document = revision_a.document

        add_document_revision(document, SimpleUploadedFile("revB.pdf", b"revB"), self.creator)
        document.refresh_from_db()
        # Rev B has its own (not-yet-approved) workflow state, so the Internal gate alone already
        # fails -- but prove the TRANSMISSION evidence itself is also exact-revision, not
        # fabricated from Document.transmittal_category: force-approve Rev B too and confirm it
        # still shows TO_ISSUE (never ISSUED/UNDER_REVIEW) until it is itself transmitted.
        revision_b = document.revisions.order_by("-created_at", "-id").first()
        self.start_and_approve(revision_b)
        document.refresh_from_db()
        self.assertEqual(
            compute_client_control_status(document), ClientControlStatus.TO_ISSUE
        )


# --- Fileless Internal regression (L3F section 33) --------------------------------------------


class FilelessRegisterRegressionTests(ControlStatusBase):
    def test_fileless_internal_document_full_control_status_row(self):
        document = self.create_fileless(document_origin="INTERNAL")
        document.transmittal_category = "For review"
        document.save(update_fields=["transmittal_category"])
        internal, supplier, client = self.control_statuses(document, annotated=True)
        self.assertEqual(internal, InternalControlStatus.DRAFT)
        self.assertIsNone(supplier)
        self.assertIsNone(client)


# --- Filters (L3F section 29/38) ---------------------------------------------------------------


class ControlStatusFilterTests(ControlStatusBase):
    def test_internal_filter_accepts_comma_separated_values_as_an_or(self):
        # L3F: the MultiSelect filter bar sends several selections comma-joined (same
        # convention as document_type/discipline/status) -- matches ANY of them.
        to_review = self.make_internal_revision()
        start_workflow(to_review, THREE, make_user("mv1"), make_user("mv2"), make_user("mv3"), self.creator)
        to_approve = self.make_internal_revision()
        author, approver = make_user("mv4"), make_user("mv5")
        workflow = start_workflow(to_approve, TWO, author, None, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        draft = self.create_fileless(document_origin="INTERNAL")

        matches = filter_by_internal_control_status(
            Document.objects.all(), "TO_REVIEW,TO_APPROVE"
        )
        matched_ids = set(matches.values_list("id", flat=True))
        self.assertIn(to_review.document_id, matched_ids)
        self.assertIn(to_approve.document_id, matched_ids)
        self.assertNotIn(draft.id, matched_ids)

    def test_internal_filter_to_approve(self):
        revision = self.make_internal_revision()
        author, approver = make_user("fa1"), make_user("fp1")
        workflow = start_workflow(revision, TWO, author, None, approver, self.creator)
        perform_action(workflow, author, "SIGN", "s")
        other = self.make_internal_revision()

        matches = filter_by_internal_control_status(
            Document.objects.all(), InternalControlStatus.TO_APPROVE
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(other.document, list(matches))

    def test_supplier_filter_to_monitor(self):
        revision = self.supplier_revision()
        in_progress = self.supplier_revision(category="FOR_REVIEW")
        self._surveillance_for(in_progress)

        matches = filter_by_supplier_control_status(
            Document.objects.all(), SupplierControlStatus.TO_MONITOR
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(in_progress.document, list(matches))

    def test_supplier_filter_draft(self):
        fileless = self.create_fileless(
            document_origin="SUPPLIER",
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_REVIEW",
        )
        with_file = self.supplier_revision()

        matches = filter_by_supplier_control_status(
            Document.objects.all(), SupplierControlStatus.DRAFT
        )
        self.assertIn(fileless, list(matches))
        self.assertNotIn(with_file.document, list(matches))

    def test_supplier_filter_to_close(self):
        revision = self.supplier_revision()
        self.make_ready_surveillance(revision, reviewer_names=("ctlfilter1",))
        other = self.supplier_revision(category="FOR_REVIEW")

        matches = filter_by_supplier_control_status(
            Document.objects.all(), SupplierControlStatus.TO_CLOSE
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(other.document, list(matches))

    def test_supplier_filter_commented(self):
        revision = self.supplier_revision()
        surveillance = self.make_ready_surveillance(revision, reviewer_names=("ctlfilter2",))
        close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="COMMENTED", closure_comment="x",
        )
        other = self.supplier_revision(category="FOR_REVIEW")

        matches = filter_by_supplier_control_status(
            Document.objects.all(), SupplierControlStatus.COMMENTED
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(other.document, list(matches))

    def test_client_filter_to_issue(self):
        revision = self.internal_revision_with_category("For review")
        self.start_and_approve(revision)
        not_approved = self.internal_revision_with_category("For review")

        matches = filter_by_client_control_status(
            Document.objects.all(), ClientControlStatus.TO_ISSUE
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(not_approved.document, list(matches))

    def test_client_filter_under_review(self):
        revision = self.internal_revision_with_category("For review")
        self.start_and_approve(revision)
        self.issue_out_transmittal(revision, purpose="FOR_REVIEW")
        to_issue = self.internal_revision_with_category("For review")
        self.start_and_approve(to_issue)

        matches = filter_by_client_control_status(
            Document.objects.all(), ClientControlStatus.UNDER_REVIEW
        )
        self.assertIn(revision.document, list(matches))
        self.assertNotIn(to_issue.document, list(matches))

    def test_filters_do_not_crash_pagination_count(self):
        revision = self.make_internal_revision()
        other = self.make_internal_revision()
        matches = filter_by_internal_control_status(
            Document.objects.all(), InternalControlStatus.IN_PROGRESS
        )
        matched_ids = set(matches.values_list("id", flat=True))
        # Base fixtures (WorkflowTestBase) already create one unrelated INTERNAL document of
        # their own -- assert OUR two are both present rather than asserting a brittle raw total.
        self.assertTrue({revision.document_id, other.document_id} <= matched_ids)
        self.assertEqual(matches.count(), len(matched_ids))
