import hashlib
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models as django_models
from rest_framework.test import APIRequestFactory

from project_management.models import (
    Document,
    DocumentRevision,
    DocumentWorkflow,
    DocumentWorkflowEvent,
)
from project_management.services.document_revision import (
    add_document_revision,
    sha256_of_file,
)
from project_management.services.document_workflow import (
    perform_action,
    start_workflow,
)
from project_management.test_document_control_revision_workflow import ProjectRevisionBase
from project_management.test_document_workflow import THREE, TWO
from project_management.views.document_revision import serialize_revision

REVISION_SERVICE = "project_management.services.document_revision"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clear_hash(revision):
    """Simulate a legacy Version (no hash) bypassing the write-once guard, test setup only."""
    django_models.QuerySet.update(
        DocumentRevision.objects.filter(pk=revision.pk), file_sha256=None
    )
    revision.refresh_from_db()


class ApprovalEvidenceBase(ProjectRevisionBase):
    def setUp(self):
        super().setUp()
        self.project_document()
        self.project.refresh_from_db()
        Document.objects.filter(pk=self.doc.pk).update(
            description="Initial approved design",
            tags=["design", "phase-1"],
            status="Issued",
        )
        self.doc.refresh_from_db()

    def fresh(self, revision):
        return DocumentRevision.objects.get(pk=revision.pk)

    def workflow_of(self, revision):
        return DocumentWorkflow.objects.get(revision=revision)


class FileHashTests(ApprovalEvidenceBase):
    def test_a_new_version_gets_a_sha256_of_its_exact_bytes(self):
        revision = self.new_version(b"abc")
        self.assertEqual(revision.file_sha256, sha(b"abc"))
        stored = self.fresh(revision)
        self.assertEqual(stored.file_sha256, sha(b"abc"))
        self.assertEqual(len(stored.file_sha256), 64)
        self.assertRegex(stored.file_sha256, r"^[0-9a-f]{64}$")

    def test_the_stored_file_is_intact_after_hashing(self):
        revision = self.new_version(b"content that must survive the hashing")
        with revision.file.open("rb") as handle:
            self.assertEqual(handle.read(), b"content that must survive the hashing")

    def test_identical_bytes_give_the_same_hash_and_different_bytes_a_different_one(self):
        first = self.new_version(b"same")
        second = self.new_version(b"same")
        third = self.new_version(b"different")
        self.assertEqual(first.file_sha256, second.file_sha256)
        self.assertNotEqual(first.file_sha256, third.file_sha256)
        # each Version keeps its own, independently calculated hash
        self.assertEqual(
            {self.fresh(v).file_sha256 for v in (first, second, third)},
            {sha(b"same"), sha(b"different")},
        )

    def test_the_helper_hashes_uploads_and_rewinds_them(self):
        upload = ContentFile(b"raw bytes", name="a.bin")
        self.assertEqual(sha256_of_file(upload), sha(b"raw bytes"))
        self.assertEqual(upload.read(), b"raw bytes")

    def test_a_stored_hash_cannot_be_rewritten_normally(self):
        revision = self.new_version(b"abc")
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(file_sha256="0" * 64)
        with self.assertRaises(ValidationError):
            revision.file_sha256 = "1" * 64
            revision.save()
        self.assertEqual(self.fresh(revision).file_sha256, sha(b"abc"))

    def test_the_hash_can_only_be_written_from_null(self):
        revision = self.new_version(b"abc")
        clear_hash(revision)
        DocumentRevision.objects.filter(pk=revision.pk).update(file_sha256=sha(b"abc"))
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=revision.pk).update(file_sha256=sha(b"x"))


class LegacyHashTests(ApprovalEvidenceBase):
    def legacy_first_version(self):
        # the document file snapshotted as Version 1 (no hash: created before it existed)
        self.new_version(b"later")
        legacy = DocumentRevision.objects.get(document=self.doc, version_number=1)
        self.assertIsNone(legacy.file_sha256)
        return legacy

    def approve_revision(self, revision):
        wf = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        return self.fresh(revision)

    def test_a_legacy_version_gets_its_hash_when_approved(self):
        legacy = self.legacy_first_version()
        approved = self.approve_revision(legacy)
        with approved.file.open("rb") as handle:
            expected = sha(handle.read())
        self.assertEqual(approved.file_sha256, expected)
        self.assertEqual(approved.approval_snapshot["file"]["sha256"], expected)

    def test_an_unreadable_legacy_file_fails_the_approval_cleanly(self):
        legacy = self.legacy_first_version()
        wf = start_workflow(legacy, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        events = DocumentWorkflowEvent.objects.filter(workflow=wf).count()
        with patch(
            "django.db.models.fields.files.FieldFile.open", side_effect=OSError("gone")
        ):
            with self.assertRaises(ValidationError):
                perform_action(wf, self.carol, "APPROVE", "a")
        wf.refresh_from_db()
        stored = self.fresh(legacy)
        self.assertEqual(wf.status, DocumentWorkflow.Status.APPROVE)
        self.assertIsNone(wf.completed_at)
        self.assertIsNone(stored.revision_label)
        self.assertIsNone(stored.file_sha256)
        self.assertIsNone(stored.approval_snapshot)
        self.assertEqual(DocumentWorkflowEvent.objects.filter(workflow=wf).count(), events)


class ApprovalSnapshotTests(ApprovalEvidenceBase):
    def test_an_unapproved_version_has_no_snapshot(self):
        self.assertIsNone(self.new_version(b"v").approval_snapshot)
        self.assertIsNone(self.fresh(self.new_version(b"w")).approval_snapshot)

    def test_approval_writes_the_snapshot_with_the_real_values(self):
        approved = self.approve()
        wf = self.workflow_of(approved)
        snap = self.fresh(approved).approval_snapshot
        document = Document.objects.get(pk=self.doc.pk)
        self.assertEqual(snap["schema_version"], 1)
        # official revision, identity and reference
        self.assertEqual(snap["version"]["revision_label"], "A")
        self.assertEqual(snap["version"]["number"], approved.version_number)
        self.assertEqual(snap["document"]["id"], document.pk)
        self.assertTrue(document.full_document_number)
        self.assertEqual(snap["document"]["reference"], document.full_document_number)
        # metadata
        self.assertEqual(snap["document"]["title"], document.file_name)
        self.assertEqual(snap["document"]["document_type"], document.document_type)
        self.assertEqual(snap["document"]["discipline"]["label"], document.discipline)
        self.assertEqual(snap["document"]["discipline"]["code"], document.discipline_code)
        self.assertEqual(snap["document"]["origin"], {"value": "INTERNAL", "label": "Internal"})
        self.assertIsNone(snap["document"]["external_issuer"])
        self.assertEqual(snap["document"]["description"], "Initial approved design")
        self.assertEqual(snap["document"]["tags"], ["design", "phase-1"])
        # context
        self.assertEqual(
            snap["project"],
            {
                "id": self.project.pk,
                "name": self.project.project_name,
                "code": self.project.project_code or None,
            },
        )
        self.assertEqual(snap["document"]["business_status"], "Issued")
        # file evidence
        stored = self.fresh(approved)
        self.assertEqual(snap["file"]["name"], stored.file_name)
        self.assertEqual(snap["file"]["sha256"], stored.file_sha256)
        self.assertEqual(snap["file"]["path"], stored.file.name)
        self.assertRegex(snap["file"]["sha256"], r"^[0-9a-f]{64}$")
        # bound to the approval, without duplicating the workflow events
        self.assertEqual(snap["workflow"]["id"], wf.pk)
        self.assertEqual(snap["workflow"]["type"], "TWO_STEP")
        self.assertEqual(snap["workflow"]["approved_at"], wf.completed_at.isoformat())
        self.assertNotIn("events", snap["workflow"])
        self.assertNotIn("comments", str(snap.keys()))

    def test_the_snapshot_never_holds_signed_urls_or_credentials(self):
        snap = self.fresh(self.approve()).approval_snapshot
        text = str(snap).lower()
        for forbidden in ("http", "signature", "token", "secret", "expires"):
            self.assertNotIn(forbidden, text)

    def test_the_snapshot_and_the_external_issuer_shape_for_a_company_document(self):
        # a company (non-project) document: project is null
        document = self.document
        revision = self.revision2
        Document.objects.filter(pk=document.pk).update(description="Company doc")
        wf = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        snap = self.fresh(revision).approval_snapshot
        self.assertIsNone(snap["project"])
        self.assertEqual(snap["document"]["description"], "Company doc")

    def test_the_snapshot_cannot_be_rewritten(self):
        approved = self.approve()
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=approved.pk).update(approval_snapshot={})
        with self.assertRaises(ValidationError):
            approved = self.fresh(approved)
            approved.approval_snapshot = {"x": 1}
            approved.save()
        self.assertEqual(self.fresh(approved).approval_snapshot["version"]["revision_label"], "A")

    def test_a_rejection_creates_no_snapshot(self):
        rejected = self.reject()
        stored = self.fresh(rejected)
        self.assertIsNone(stored.revision_label)
        self.assertIsNone(stored.approval_snapshot)
        self.assertIsNotNone(stored.file_sha256)  # the hash is version evidence, not approval

    def test_the_two_step_and_three_step_approvals_both_record_evidence(self):
        two = self.approve()
        self.assertEqual(self.fresh(two).approval_snapshot["workflow"]["type"], "TWO_STEP")
        revision = self.new_version(b"three step")
        wf = start_workflow(revision, THREE, self.alice, self.bob, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.bob, "VALIDATE", "v")
        perform_action(wf, self.carol, "APPROVE", "a")
        snap = self.fresh(revision).approval_snapshot
        self.assertEqual(snap["workflow"]["type"], "THREE_STEP")
        self.assertEqual(snap["version"]["revision_label"], "B")


class ApprovalAtomicityTests(ApprovalEvidenceBase):
    def prepare(self, content=b"atomic"):
        revision = self.new_version(content)
        wf = start_workflow(revision, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        return revision, wf

    def assert_not_approved(self, revision, wf, events):
        wf.refresh_from_db()
        stored = self.fresh(revision)
        self.assertEqual(wf.status, DocumentWorkflow.Status.APPROVE)
        self.assertIsNone(wf.completed_at)
        self.assertIsNone(stored.revision_label)
        self.assertIsNone(stored.approval_snapshot)
        self.assertEqual(DocumentWorkflowEvent.objects.filter(workflow=wf).count(), events)

    def test_a_snapshot_failure_rolls_the_whole_approval_back(self):
        revision, wf = self.prepare()
        events = DocumentWorkflowEvent.objects.filter(workflow=wf).count()
        with patch(f"{REVISION_SERVICE}.build_approval_snapshot", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                perform_action(wf, self.carol, "APPROVE", "a")
        self.assert_not_approved(revision, wf, events)

    def test_a_hash_failure_rolls_the_whole_approval_back(self):
        revision, wf = self.prepare()
        clear_hash(revision)
        events = DocumentWorkflowEvent.objects.filter(workflow=wf).count()
        with patch(f"{REVISION_SERVICE}.sha256_of_file", side_effect=RuntimeError("boom")):
            with self.assertRaises(ValidationError):
                perform_action(wf, self.carol, "APPROVE", "a")
        self.assert_not_approved(revision, wf, events)
        self.assertIsNone(self.fresh(revision).file_sha256)

    def test_after_a_failed_approval_a_retry_succeeds_with_the_right_label(self):
        revision, wf = self.prepare()
        with patch(f"{REVISION_SERVICE}.build_approval_snapshot", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                perform_action(wf, self.carol, "APPROVE", "a")
        perform_action(wf, self.carol, "APPROVE", "a")
        stored = self.fresh(revision)
        self.assertEqual(stored.revision_label, "A")
        self.assertEqual(stored.approval_snapshot["version"]["revision_label"], "A")
        self.assertEqual(stored.approval_snapshot["file"]["sha256"], stored.file_sha256)


class LiveVersusSnapshotTests(ApprovalEvidenceBase):
    def test_later_live_changes_never_alter_an_approved_snapshot(self):
        revision_a = self.approve()
        before = self.fresh(revision_a).approval_snapshot
        Document.objects.filter(pk=self.doc.pk).update(
            description="Updated project context", tags=["changed"], status="Superseded"
        )
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.description, "Updated project context")
        after = self.fresh(revision_a).approval_snapshot
        self.assertEqual(after, before)
        self.assertEqual(after["document"]["description"], "Initial approved design")
        self.assertEqual(after["document"]["tags"], ["design", "phase-1"])
        self.assertEqual(after["document"]["business_status"], "Issued")

    def test_the_next_version_has_its_own_hash_and_snapshot_and_leaves_a_untouched(self):
        revision_a = self.approve()
        a_before = self.fresh(revision_a)
        a_snapshot, a_hash = a_before.approval_snapshot, a_before.file_sha256
        Document.objects.filter(pk=self.doc.pk).update(description="Rev B context")
        revision_b = self.new_version(b"a different file for revision B")
        self.assertIsNone(self.fresh(revision_b).approval_snapshot)
        self.assertNotEqual(revision_b.file_sha256, a_hash)
        wf = start_workflow(revision_b, TWO, self.alice, None, self.carol, self.creator)
        perform_action(wf, self.alice, "SIGN", "s")
        perform_action(wf, self.carol, "APPROVE", "a")
        b = self.fresh(revision_b)
        self.assertEqual(b.revision_label, "B")
        self.assertEqual(b.approval_snapshot["version"]["revision_label"], "B")
        self.assertEqual(b.approval_snapshot["document"]["description"], "Rev B context")
        self.assertEqual(b.approval_snapshot["file"]["sha256"], sha(b"a different file for revision B"))
        a_after = self.fresh(revision_a)
        self.assertEqual(a_after.approval_snapshot, a_snapshot)
        self.assertEqual(a_after.file_sha256, a_hash)
        self.assertEqual(a_after.revision_label, "A")


class EvidenceApiTests(ApprovalEvidenceBase):
    def test_the_revision_payload_exposes_the_evidence_read_only(self):
        approved = self.fresh(self.approve())
        request = APIRequestFactory().get("/x/")
        payload = serialize_revision(approved, request, self.doc)
        self.assertEqual(payload["file_sha256"], approved.file_sha256)
        self.assertEqual(payload["approval_snapshot"], approved.approval_snapshot)
        draft = serialize_revision(self.new_version(b"draft"), request, self.doc)
        self.assertIsNone(draft["approval_snapshot"])
        self.assertRegex(draft["file_sha256"], r"^[0-9a-f]{64}$")
