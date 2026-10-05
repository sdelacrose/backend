"""L3G-2: Supplier Surveillance Report PDF generation + frozen evidence storage -- targeted
tests only. No Supplier Portal/email/Microsoft 365/notifications/Client Review: those remain
later lots, deliberately not covered here.
"""

import hashlib
from unittest.mock import patch

from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import SupplierSurveillance
from project_management.models.supplier_surveillance_evidence import (
    SupplierSurveillanceEvidence,
)
from project_management.models.supplier_surveillance_review import SupplierSurveillanceEvent
from project_management.services.supplier_surveillance_evidence import (
    generate_supplier_surveillance_evidence,
    render_supplier_surveillance_report_html,
)
from project_management.test_supplier_surveillance_closure import ACCESS, CAN_UPDATE, ClosureBase
from project_management.test_supplier_surveillance_evidence import EvidenceBase
from project_management.views.supplier_surveillance_review import (
    SupplierSurveillanceReviewViewSet,
)

EventType = SupplierSurveillanceEvent.EventType
SurveillanceStatus = SupplierSurveillance.Status
Result = SupplierSurveillance.Result

GENERATE_PATCH_TARGET = (
    "project_management.views.supplier_surveillance_review.generate_supplier_surveillance_evidence"
)


# A/B/C/D/E -- Evidence creation, file, PDF signature, hash, event.
class EvidenceGenerationTests(EvidenceBase):
    def test_generate_creates_evidence_for_closed_surveillance(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertIsNotNone(evidence.pk)
        self.assertEqual(evidence.surveillance_id, closed.id)

    def test_evidence_file_exists_and_is_non_empty(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertTrue(evidence.file)
        self.assertGreater(evidence.file.size, 0)

    def test_pdf_begins_with_valid_signature(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        evidence.file.open("rb")
        try:
            header = evidence.file.read(5)
        finally:
            evidence.file.close()
        self.assertEqual(header, b"%PDF-")

    def test_sha256_matches_stored_file_bytes(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        evidence.file.open("rb")
        try:
            actual_hash = hashlib.sha256(evidence.file.read()).hexdigest()
        finally:
            evidence.file.close()
        self.assertEqual(evidence.file_sha256, actual_hash)

    def test_pdf_generated_event_created(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        event = closed.events.get(event_type=EventType.PDF_GENERATED)
        self.assertEqual(event.actor_id, self.creator.id)
        self.assertEqual(event.metadata["evidence_id"], evidence.id)
        self.assertEqual(event.metadata["file_sha256"], evidence.file_sha256)


# F/G/H -- idempotency: second call returns the same Evidence, no second event, no file swap.
class EvidenceIdempotencyTests(EvidenceBase):
    def test_second_generation_call_returns_same_evidence(self):
        closed, _reviewers = self.make_closed_surveillance()
        first = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        second = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertEqual(first.pk, second.pk)

    def test_second_generation_call_creates_no_second_event(self):
        closed, _reviewers = self.make_closed_surveillance()
        generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertEqual(
            closed.events.filter(event_type=EventType.PDF_GENERATED).count(), 1
        )

    def test_second_generation_call_does_not_replace_file(self):
        closed, _reviewers = self.make_closed_surveillance()
        first = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        first_name = first.file.name
        first_hash = first.file_sha256
        second = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertEqual(second.file.name, first_name)
        self.assertEqual(second.file_sha256, first_hash)
        self.assertEqual(SupplierSurveillanceEvidence.objects.filter(surveillance=closed).count(), 1)


# I -- non-CLOSED generation rejected.
class EvidenceNonClosedRejectionTests(EvidenceBase):
    def test_draft_rejected(self):
        surveillance = self.make_surveillance()
        with self.assertRaises(ValidationError):
            generate_supplier_surveillance_evidence(surveillance=surveillance, actor=self.creator)

    def test_ready_for_closure_rejected(self):
        surveillance, _reviewers = self.make_ready_surveillance(reviewer_names=("pdfalice1",))
        with self.assertRaises(ValidationError):
            generate_supplier_surveillance_evidence(surveillance=surveillance, actor=self.creator)


# J/K/L -- exact revision, dead live Template, live data drift all irrelevant to the frozen render.
class EvidenceFrozenRenderTests(EvidenceBase):
    def test_exact_revision_preserved_even_after_a_later_revision_exists(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        closed, _reviewers = self.make_closed_surveillance()
        document = closed.document_revision.document
        original_external_revision = closed.document_revision.external_revision
        response = self.revisions_api(
            "post", document,
            {
                "file": SimpleUploadedFile("v2.pdf", b"2"),
                "revision_type": "minor",
                "comment": "second supplier revision",
                "external_revision": "B",
            },
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", None))
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertEqual(
            evidence.evidence_metadata["supplier"]["supplier_revision"],
            original_external_revision,
        )

    def test_live_template_rename_after_generation_does_not_change_stored_metadata(self):
        from project_management.models import SupplierSurveillanceTemplate

        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        stored_template_name = evidence.evidence_metadata["template"]["template_name"]

        live_template = SupplierSurveillanceTemplate.objects.get(
            pk=evidence.evidence_metadata["template"]["template_id"]
        )
        live_template.name = "Renamed After Generation"
        live_template.save(update_fields=["name"])

        evidence.refresh_from_db()
        self.assertEqual(evidence.evidence_metadata["template"]["template_name"], stored_template_name)
        self.assertNotEqual(evidence.evidence_metadata["template"]["template_name"], live_template.name)

    def test_live_supplier_rename_after_generation_does_not_change_stored_metadata(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        stored_company_name = evidence.evidence_metadata["supplier"]["company_name"]

        self.supplier_company.company_name = "Renamed Supplier Co"
        self.supplier_company.save(update_fields=["company_name"])

        evidence.refresh_from_db()
        self.assertEqual(evidence.evidence_metadata["supplier"]["company_name"], stored_company_name)
        self.assertNotEqual(stored_company_name, "Renamed Supplier Co")


# Section 31 -- PDF content/rendering-context tests (HTML source, not pixel/layout).
class PdfContentTests(EvidenceBase):
    def _html(self, surveillance, *, extra_reviewer=None):
        from django.utils import timezone

        from project_management.services.supplier_surveillance_evidence import (
            build_evidence_metadata,
        )

        metadata = build_evidence_metadata(surveillance)
        return render_supplier_surveillance_report_html(
            surveillance, metadata, generated_at=timezone.now()
        )

    def test_content_contains_identity_and_closure_fields(self):
        closed, _reviewers = self.make_closed_surveillance(
            result=Result.COMMENTED, closure_comment="Please revise the drawing index."
        )
        html = self._html(closed)
        document = closed.document_revision.document
        self.assertIn(closed.reference, html)
        self.assertIn(self.project.project_name, html)
        self.assertIn(self.project.project_code, html)
        self.assertIn(self.supplier_company.company_name, html)
        self.assertIn(document.identification_number, html)
        self.assertIn(closed.document_revision.external_revision, html)
        self.assertIn(document.file_name, html)
        self.assertIn(document.document_type, html)
        self.assertIn(document.discipline, html)
        self.assertIn(closed.template_snapshot["template_name"], html)
        self.assertIn(str(closed.template_snapshot["template_version"]), html)
        self.assertIn("COMMENTED", html)
        self.assertIn("Please revise the drawing index.", html)
        # Closed By / Created By render the actor's display name (section 16), not email.
        self.assertIn(self.creator.first_name, html)
        self.assertIn(self.creator.last_name, html)

    def test_content_contains_multiple_reviewers_and_their_timestamps(self):
        closed, reviewers = self.make_closed_surveillance(
            reviewer_names=("pdfreva", "pdfrevb")
        )
        html = self._html(closed)
        for reviewer in reviewers:
            # Reviewer Summary table (section 12) shows each Reviewer's own email.
            self.assertIn(reviewer.email, html)
            # Detailed Review (section 13/15) shows each Reviewer's own display name, once per
            # row -- both reviewers answered the one required CHECK item, so both names appear.
            self.assertIn((reviewer.full_name or "").strip() or reviewer.email, html)

    def test_content_contains_check_response_label(self):
        closed, _reviewers = self.make_closed_surveillance()
        html = self._html(closed)
        self.assertIn("Compliant", html)

    def test_content_contains_text_response(self):
        from project_management.services.supplier_surveillance_review import (
            assign_reviewer,
            close_supplier_surveillance,
            complete_review,
            save_responses,
        )

        surveillance = self.make_surveillance(with_two_items=True)
        item_ids = self.snapshot_item_ids(surveillance)
        reviewer = self.make_reviewer("pdftextrev")
        assignment = assign_reviewer(surveillance=surveillance, reviewer=reviewer, actor=self.creator)
        save_responses(
            assignment=assignment, actor=reviewer,
            answers=[
                {"item_snapshot_id": item_ids["Document reference correct"], "check_value": "COMPLIANT"},
                {
                    "item_snapshot_id": item_ids["General observations"],
                    "text_value": "Looks fine, no issues observed.",
                },
            ],
        )
        complete_review(assignment=assignment, actor=reviewer)
        surveillance.refresh_from_db()
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator, result="REVIEWED", closure_comment="",
        )
        html = self._html(closed)
        self.assertIn("Looks fine, no issues observed.", html)

    def test_content_contains_section_titles(self):
        closed, _reviewers = self.make_closed_surveillance()
        html = self._html(closed)
        self.assertIn("Document Identification", html)


# Failure atomicity (section 5/33).
class EvidenceFailureTests(EvidenceBase):
    def test_rendering_failure_leaves_no_evidence_row_or_event(self):
        closed, _reviewers = self.make_closed_surveillance()
        with patch(
            "project_management.services.supplier_surveillance_evidence.HTML",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertFalse(SupplierSurveillanceEvidence.objects.filter(surveillance=closed).exists())
        self.assertFalse(closed.events.filter(event_type=EventType.PDF_GENERATED).exists())
        closed.refresh_from_db()
        self.assertEqual(closed.status, SurveillanceStatus.CLOSED)

    def test_retry_after_rendering_failure_succeeds(self):
        closed, _reviewers = self.make_closed_surveillance()
        with patch(
            "project_management.services.supplier_surveillance_evidence.HTML",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        evidence = generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertIsNotNone(evidence.pk)

    def test_storage_persistence_failure_leaves_no_orphaned_file_or_row(self):
        """Section 35: a DB failure AFTER the PDF bytes are already written to storage (here
        simulated by record_event raising inside the same atomic block as evidence.save()) must
        not leave an orphaned file behind, and must not leave a misleading Evidence/event."""
        closed, _reviewers = self.make_closed_surveillance()
        with patch(
            "project_management.services.supplier_surveillance_evidence.record_event",
            side_effect=RuntimeError("db boom"),
        ):
            with self.assertRaises(RuntimeError):
                generate_supplier_surveillance_evidence(surveillance=closed, actor=self.creator)
        self.assertFalse(SupplierSurveillanceEvidence.objects.filter(surveillance=closed).exists())
        self.assertFalse(closed.events.filter(event_type=EventType.PDF_GENERATED).exists())


# Close orchestration (section 3/22/34).
class CloseOrchestrationTests(ClosureBase):
    def _close_request(self, document, surveillance_id, data, as_user=None):
        factory = APIRequestFactory()
        request = factory.post("/x/", data, format="json")
        force_authenticate(request, user=as_user or self.creator)
        view = SupplierSurveillanceReviewViewSet.as_view(
            {"post": "close_surveillance_action"}, permission_classes=[]
        )
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, surveillance_id=surveillance_id)

    def test_close_auto_generates_evidence_and_exposes_it(self):
        surveillance, _reviewers = self.make_ready_surveillance(reviewer_names=("orcha1",))
        document = surveillance.document_revision.document
        response = self._close_request(
            document, surveillance.id, {"result": "REVIEWED", "closure_comment": ""}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        self.assertIsNotNone(response.data["evidence_pdf"])
        self.assertIn("url", response.data["evidence_pdf"])
        surveillance.refresh_from_db()
        self.assertTrue(SupplierSurveillanceEvidence.objects.filter(surveillance=surveillance).exists())

    def test_close_succeeds_even_when_evidence_generation_fails(self):
        surveillance, _reviewers = self.make_ready_surveillance(reviewer_names=("orchb1",))
        document = surveillance.document_revision.document
        with patch(GENERATE_PATCH_TARGET, side_effect=RuntimeError("weasyprint exploded")):
            response = self._close_request(
                document, surveillance.id, {"result": "REVIEWED", "closure_comment": ""}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CLOSED")
        self.assertIsNone(response.data["evidence_pdf"])
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status, SurveillanceStatus.CLOSED)
        self.assertFalse(
            SupplierSurveillanceEvidence.objects.filter(surveillance=surveillance).exists()
        )


# Manual retry endpoint (section 23/25).
class ManualRetryEndpointTests(EvidenceBase):
    def _retry_request(self, document, surveillance_id, as_user=None, can_update_side_effect=None):
        factory = APIRequestFactory()
        request = factory.post("/x/", {}, format="json")
        force_authenticate(request, user=as_user or self.creator)
        view = SupplierSurveillanceReviewViewSet.as_view(
            {"post": "generate_evidence_action"}, permission_classes=[]
        )
        can_update_patch = (
            patch(CAN_UPDATE, side_effect=can_update_side_effect)
            if can_update_side_effect
            else patch(CAN_UPDATE)
        )
        with patch(ACCESS, return_value=document), can_update_patch:
            return view(request, surveillance_id=surveillance_id)

    def test_retry_generates_evidence_for_closed_surveillance_with_none(self):
        closed, _reviewers = self.make_closed_surveillance(reviewer_names=("retrya1",))
        document = closed.document_revision.document
        response = self._retry_request(document, closed.id)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.data["evidence_pdf"])
        self.assertTrue(SupplierSurveillanceEvidence.objects.filter(surveillance=closed).exists())

    def test_retry_rejected_for_non_closed_surveillance(self):
        surveillance, _reviewers = self.make_ready_surveillance(reviewer_names=("retryb1",))
        document = surveillance.document_revision.document
        response = self._retry_request(document, surveillance.id)
        self.assertEqual(response.status_code, 400)

    def test_retry_idempotent_on_existing_evidence(self):
        closed, _reviewers = self.make_closed_surveillance(reviewer_names=("retryc1",))
        document = closed.document_revision.document
        self._retry_request(document, closed.id)
        first_evidence = SupplierSurveillanceEvidence.objects.get(surveillance=closed)
        self._retry_request(document, closed.id)
        self.assertEqual(SupplierSurveillanceEvidence.objects.filter(surveillance=closed).count(), 1)
        second_evidence = SupplierSurveillanceEvidence.objects.get(surveillance=closed)
        self.assertEqual(first_evidence.pk, second_evidence.pk)

    def test_retry_rejected_without_update_permission(self):
        from django.core.exceptions import PermissionDenied

        closed, _reviewers = self.make_closed_surveillance(reviewer_names=("retryd1",))
        document = closed.document_revision.document
        response = self._retry_request(
            document, closed.id, can_update_side_effect=PermissionDenied("no")
        )
        self.assertEqual(response.status_code, 403)
