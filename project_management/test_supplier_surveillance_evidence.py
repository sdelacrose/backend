"""L3G-1: Supplier Surveillance final evidence foundation -- targeted tests only.

Covers ONLY the SupplierSurveillanceEvidence model + the metadata-snapshot builder (services.
supplier_surveillance_evidence). No PDF rendering, no file write by the application, no actual
row creation flow: those remain L3G-2, deliberately not covered here (evidence rows below are
built directly with a stub file purely to exercise the model's own immutability guards).
"""

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from project_management.models import SupplierSurveillance
from project_management.models.supplier_surveillance_evidence import (
    SupplierSurveillanceEvidence,
)
from project_management.services.supplier_surveillance_evidence import (
    assert_surveillance_closed,
    build_evidence_filename,
    build_evidence_metadata,
)
from project_management.services.supplier_surveillance_review import close_supplier_surveillance
from project_management.test_document_workflow import make_user
from project_management.test_supplier_surveillance_closure import ClosureBase

Result = SupplierSurveillance.Result
SurveillanceStatus = SupplierSurveillance.Status


class EvidenceBase(ClosureBase):
    def make_closed_surveillance(self, *, reviewer_names=("evidalice",), result=Result.REVIEWED,
                                  closure_comment=""):
        surveillance, reviewers = self.make_ready_surveillance(reviewer_names=reviewer_names)
        closed = close_supplier_surveillance(
            surveillance=surveillance, actor=self.creator,
            result=result, closure_comment=closure_comment,
        )
        return closed, reviewers

    def make_evidence(self, surveillance, *, metadata=None):
        return SupplierSurveillanceEvidence.objects.create(
            surveillance=surveillance,
            file=ContentFile(b"%PDF-1.4 stub", name="evidence.pdf"),
            generated_by=self.creator,
            evidence_metadata=metadata if metadata is not None else build_evidence_metadata(surveillance),
        )


# A -- model is OneToOne with Surveillance.
class EvidenceModelTests(EvidenceBase):
    def test_evidence_is_one_to_one_with_surveillance(self):
        closed, _reviewers = self.make_closed_surveillance()
        self.make_evidence(closed)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                self.make_evidence(closed)

    def test_surveillance_evidence_reverse_accessor(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = self.make_evidence(closed)
        closed.refresh_from_db()
        self.assertEqual(closed.evidence, evidence)


# B -- metadata builder rejects non-CLOSED Surveillance.
class EvidenceClosedPreconditionTests(EvidenceBase):
    def test_rejects_draft(self):
        surveillance = self.make_surveillance()
        self.assertEqual(surveillance.status, SurveillanceStatus.DRAFT)
        with self.assertRaises(ValidationError):
            build_evidence_metadata(surveillance)

    def test_rejects_under_review(self):
        surveillance, reviewers = self.make_ready_surveillance(reviewer_names=("evidalice2", "evidbob2"))
        # Flip one reviewer back so the Surveillance itself is UNDER_REVIEW, not READY_FOR_CLOSURE.
        assignment = surveillance.reviewer_assignments.get(reviewer=reviewers[1])
        from project_management.models.supplier_surveillance_review import (
            SupplierSurveillanceReviewerAssignment as Assignment,
        )

        assignment.status = Assignment.Status.IN_PROGRESS
        assignment.save(update_fields=["status"])
        surveillance.status = SurveillanceStatus.UNDER_REVIEW
        surveillance.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            build_evidence_metadata(surveillance)

    def test_rejects_ready_for_closure(self):
        surveillance, _reviewers = self.make_ready_surveillance(reviewer_names=("evidalice3",))
        self.assertEqual(surveillance.status, SurveillanceStatus.READY_FOR_CLOSURE)
        with self.assertRaises(ValidationError):
            build_evidence_metadata(surveillance)

    def test_assert_surveillance_closed_helper_matches_builder(self):
        surveillance = self.make_surveillance()
        with self.assertRaises(ValidationError):
            assert_surveillance_closed(surveillance)


# C -- metadata snapshot uses the exact DocumentRevision, never "latest".
class EvidenceExactRevisionTests(EvidenceBase):
    def test_metadata_references_surveillance_own_revision_not_a_later_one(self):
        closed, _reviewers = self.make_closed_surveillance()
        original_revision_id = closed.document_revision_id
        document = closed.document_revision.document
        # A later revision is uploaded on the SAME document after closure -- the evidence
        # metadata must still describe the exact revision the Surveillance was created against.
        from django.core.files.uploadedfile import SimpleUploadedFile

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
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["document"]["document_revision_id"], original_revision_id)


# D -- metadata freezes Project/Supplier/Document identity + Supplier Ref/Revision.
class EvidenceIdentitySnapshotTests(EvidenceBase):
    def test_project_identity_snapshotted(self):
        closed, _reviewers = self.make_closed_surveillance()
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["project"]["id"], self.project.id)
        self.assertEqual(metadata["project"]["name"], self.project.project_name)
        self.assertEqual(metadata["project"]["code"], self.project.project_code)

    def test_document_identity_snapshotted(self):
        closed, _reviewers = self.make_closed_surveillance()
        document = closed.document_revision.document
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["document"]["id"], document.id)
        self.assertEqual(metadata["document"]["title"], document.file_name)
        self.assertEqual(metadata["document"]["document_type"], document.document_type)
        self.assertEqual(metadata["document"]["discipline"], document.discipline)

    def test_supplier_identity_and_reference_snapshotted(self):
        closed, _reviewers = self.make_closed_surveillance()
        document = closed.document_revision.document
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["supplier"]["company_id"], self.supplier_company.id)
        self.assertEqual(metadata["supplier"]["company_name"], self.supplier_company.company_name)
        self.assertEqual(metadata["supplier"]["supplier_reference"], document.identification_number)
        self.assertEqual(metadata["supplier"]["supplier_revision"], closed.document_revision.external_revision)


# E -- template name/version come from template_snapshot, NEVER the live Template.
class EvidenceTemplateSnapshotTests(EvidenceBase):
    def test_template_metadata_survives_live_template_rename(self):
        closed, _reviewers = self.make_closed_surveillance()
        snapshot_name = closed.template_snapshot["template_name"]
        snapshot_version = closed.template_snapshot["template_version"]

        from project_management.models import SupplierSurveillanceTemplate

        live_template = SupplierSurveillanceTemplate.objects.get(pk=closed.template_snapshot["template_id"])
        live_template.name = "Renamed After Closure"
        live_template.version = live_template.version + 1
        live_template.save(update_fields=["name", "version"])

        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["template"]["template_name"], snapshot_name)
        self.assertEqual(metadata["template"]["template_version"], snapshot_version)
        self.assertNotEqual(metadata["template"]["template_name"], live_template.name)

    def test_template_metadata_survives_live_template_deletion(self):
        closed, _reviewers = self.make_closed_surveillance()
        snapshot_name = closed.template_snapshot["template_name"]

        from project_management.models import SupplierSurveillanceTemplate

        SupplierSurveillanceTemplate.objects.filter(pk=closed.template_snapshot["template_id"]).delete()

        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["template"]["template_name"], snapshot_name)


# F -- multiple reviewers (1..N) are snapshotted.
class EvidenceReviewerCountTests(EvidenceBase):
    def test_single_reviewer_snapshotted(self):
        closed, reviewers = self.make_closed_surveillance(reviewer_names=("evidsingle4",))
        metadata = build_evidence_metadata(closed)
        self.assertEqual(len(metadata["actors"]["reviewers"]), 1)

    def test_multiple_reviewers_all_snapshotted(self):
        closed, reviewers = self.make_closed_surveillance(
            reviewer_names=("evidmulti5a", "evidmulti5b", "evidmulti5c")
        )
        metadata = build_evidence_metadata(closed)
        self.assertEqual(len(metadata["actors"]["reviewers"]), 3)
        snapshotted_ids = {r["reviewer"]["id"] for r in metadata["actors"]["reviewers"]}
        self.assertEqual(snapshotted_ids, {reviewer.id for reviewer in reviewers})


# G -- reviewer identity: id/name/email.
class EvidenceReviewerIdentityTests(EvidenceBase):
    def test_reviewer_identity_fields_match(self):
        closed, reviewers = self.make_closed_surveillance(reviewer_names=("evididentity6",))
        reviewer = reviewers[0]
        metadata = build_evidence_metadata(closed)
        snapshotted = metadata["actors"]["reviewers"][0]["reviewer"]
        self.assertEqual(snapshotted["id"], reviewer.id)
        self.assertEqual(snapshotted["email"], reviewer.email)
        self.assertEqual(snapshotted["name"], (reviewer.full_name or "").strip() or reviewer.email)


# H -- reviewer timestamps: assigned/started/completed.
class EvidenceReviewerTimestampTests(EvidenceBase):
    def test_reviewer_timestamps_present_and_match_assignment(self):
        closed, reviewers = self.make_closed_surveillance(reviewer_names=("evidtime7",))
        assignment = closed.reviewer_assignments.get(reviewer=reviewers[0])
        metadata = build_evidence_metadata(closed)
        snapshotted = metadata["actors"]["reviewers"][0]
        self.assertEqual(snapshotted["assignment_id"], assignment.id)
        self.assertEqual(snapshotted["assigned_at"], assignment.assigned_at.isoformat())
        self.assertEqual(
            snapshotted["started_at"],
            assignment.started_at.isoformat() if assignment.started_at else None,
        )
        self.assertEqual(snapshotted["completed_at"], assignment.completed_at.isoformat())
        self.assertIsNotNone(snapshotted["completed_at"])


# I -- closer identity/timestamp.
class EvidenceCloserTests(EvidenceBase):
    def test_closer_identity_matches_actor_and_closed_at_present(self):
        closed, _reviewers = self.make_closed_surveillance()
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["actors"]["closer"]["id"], self.creator.id)
        self.assertEqual(metadata["actors"]["closer"]["email"], self.creator.email)
        self.assertIsNotNone(metadata["surveillance"]["closed_at"])
        self.assertEqual(metadata["surveillance"]["closed_at"], closed.closed_at.isoformat())


# J -- creator identity.
class EvidenceCreatorTests(EvidenceBase):
    def test_creator_identity_matches_surveillance_created_by(self):
        closed, _reviewers = self.make_closed_surveillance()
        metadata = build_evidence_metadata(closed)
        self.assertEqual(metadata["actors"]["creator"]["id"], closed.created_by_id)
        self.assertEqual(metadata["actors"]["creator"]["email"], closed.created_by.email)


# K -- null optional actor (SET_NULL) handled safely.
class EvidenceNullActorTests(EvidenceBase):
    def test_null_closed_by_does_not_crash(self):
        closed, _reviewers = self.make_closed_surveillance()
        closed.closed_by = None
        closed.save(update_fields=["closed_by"])
        metadata = build_evidence_metadata(closed)
        self.assertIsNone(metadata["actors"]["closer"])

    def test_null_created_by_does_not_crash(self):
        closed, _reviewers = self.make_closed_surveillance()
        closed.created_by = None
        closed.save(update_fields=["created_by"])
        metadata = build_evidence_metadata(closed)
        self.assertIsNone(metadata["actors"]["creator"])

    def test_null_assignment_assigned_by_does_not_crash(self):
        closed, reviewers = self.make_closed_surveillance(reviewer_names=("evidnull10",))
        assignment = closed.reviewer_assignments.get(reviewer=reviewers[0])
        assignment.assigned_by = None
        assignment.save(update_fields=["assigned_by"])
        # assigned_by is not part of the reviewer snapshot shape at all (section 7/11) -- this
        # simply proves the builder tolerates a null optional actor elsewhere on the row without
        # raising, since _reviewer_snapshot never reads assigned_by.
        metadata = build_evidence_metadata(closed)
        self.assertEqual(len(metadata["actors"]["reviewers"]), 1)


# L -- immutability: QuerySet-level AND instance-level guards.
class EvidenceImmutabilityTests(EvidenceBase):
    def test_queryset_update_rejected(self):
        closed, _reviewers = self.make_closed_surveillance()
        self.make_evidence(closed)
        with self.assertRaises(ValidationError):
            SupplierSurveillanceEvidence.objects.filter(surveillance=closed).update(file_sha256="x")

    def test_queryset_delete_rejected(self):
        closed, _reviewers = self.make_closed_surveillance()
        self.make_evidence(closed)
        with self.assertRaises(ValidationError):
            SupplierSurveillanceEvidence.objects.filter(surveillance=closed).delete()

    def test_instance_save_after_creation_rejected(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = self.make_evidence(closed)
        evidence.file_sha256 = "deadbeef"
        with self.assertRaises(ValidationError):
            evidence.save()

    def test_instance_delete_rejected(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = self.make_evidence(closed)
        with self.assertRaises(ValidationError):
            evidence.delete()
        self.assertTrue(SupplierSurveillanceEvidence.objects.filter(pk=evidence.pk).exists())

    def test_first_save_still_allowed(self):
        closed, _reviewers = self.make_closed_surveillance()
        evidence = self.make_evidence(closed)
        self.assertIsNotNone(evidence.pk)


# M -- filename sanitization.
class EvidenceFilenameTests(EvidenceBase):
    def test_filename_has_no_path_separators(self):
        closed, _reviewers = self.make_closed_surveillance()
        document = closed.document_revision.document
        document.identification_number = "VEN/4578/DWG-002"
        document.save(update_fields=["identification_number"])
        filename = build_evidence_filename(closed)
        self.assertNotIn("/", filename)
        self.assertNotIn("\\", filename)
        self.assertTrue(filename.endswith(".pdf"))

    def test_filename_contains_surveillance_reference(self):
        closed, _reviewers = self.make_closed_surveillance()
        filename = build_evidence_filename(closed)
        self.assertIn(closed.reference, filename)
