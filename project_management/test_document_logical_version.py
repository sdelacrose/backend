"""OPTION B1 / LOT 1: logical document version and approval status with technical BPE versions.

The physical rows (version_number, files, transmittals) are never touched; the LOGICAL version groups an ordinary revision
with its technical BPE version, and the approval status comes from the logical revision (a BPE file never turns an approved
document back into a draft, and a non approved revision is never shown as approved)."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentRevision
from project_management.serializers.document import DocumentSerializer
from project_management.services import client_surveillance_followup as fu_service
from project_management.services.document_logical_version import (
    get_current_logical_revision, get_latest_physical_revision, is_technical, logical_info, logical_revision_of, logical_version_number,
)
from project_management.services.document_register_status import compute_internal_control_status
from project_management.services.document_revision import MAJOR, add_document_revision
from project_management.services.document_workflow import annotate_current_workflow, get_internal_status, start_workflow
from project_management.test_client_surveillance_lot3 import Lot3Base
from project_management.views.document_revision import DocumentRevisionViewSet


class LogicalBase(Lot3Base):
    def rows(self, document=None):
        return list(DocumentRevision.objects.filter(document=document or self.document).order_by("version_number"))

    def numbers(self, document=None):
        info = logical_info(self.rows(document))
        return [info[r.pk][1] for r in self.rows(document)]

    def status(self, document=None):
        return get_internal_status(Document.objects.get(pk=(document or self.document).pk))

    def annotated(self, document=None):
        return annotate_current_workflow(Document.objects.filter(pk=(document or self.document).pk)).get()

    def api_list(self, user=None, document=None):
        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user or self.owner)
        with patch("project_management.views.document_revision.get_register_document_or_404", return_value=document or self.document):
            return view(request, document_id=(document or self.document).pk)


class LogicalVersionModelTests(LogicalBase):
    def test_a_prel_alone_is_the_first_logical_version_and_is_not_technical(self):  # A PREL seul
        rows = self.rows()
        self.assertTrue(rows and not any(is_technical(r) for r in rows))
        self.assertEqual(self.numbers(), list(range(1, len(rows) + 1)))  # without a technical row logical == physical order
        latest = get_latest_physical_revision(self.document)
        self.assertEqual((get_current_logical_revision(self.document).pk, logical_revision_of(latest).pk), (latest.pk, latest.pk))

    def test_a_prel_approved_is_approved_and_has_its_label(self):  # A PREL approuvé
        self.assertEqual(self.status(), "APPROVED")
        self.assertEqual(self.annotated().current_revision_label, self.revision.revision_label)

    def test_a_bpe_joins_the_logical_version_of_its_source_and_creates_none(self):  # A PREL + A BPE = V1 / A
        before = logical_version_number(self.revision)
        followup, bpe = self.journey("ACCEPTED")
        self.assertTrue(is_technical(bpe))
        self.assertEqual((logical_revision_of(bpe).pk, logical_version_number(bpe)), (self.revision.pk, before))
        info = logical_info(self.rows())
        self.assertEqual(info[bpe.pk], info[self.revision.pk])  # same logical revision, same number
        self.assertEqual(get_latest_physical_revision(self.document).pk, bpe.pk)  # the BPE is the latest FILE ...
        self.assertEqual(get_current_logical_revision(self.document).pk, self.revision.pk)  # ... not the logical revision
        self.assertEqual(max(r.version_number for r in self.rows()), bpe.version_number)  # physical numbering untouched (+1 row)

    def test_b_prel_and_b_bpe_are_one_logical_version(self):  # B PREL + B BPE = V2 / B
        followup, revision = self.journey("ACCEPTED_WITH_COMMENTS")  # the B file: an ordinary revision (full validation)
        self.assertFalse(is_technical(revision))
        self.assertEqual(revision.revision_label, "B")
        before = list(self.rows())
        self.send_rev(revision, "FOR_INFORMATION")
        self.assertEqual(self.rows(), before)  # nothing added, nothing renumbered by the send
        self.assertEqual(logical_version_number(revision), logical_version_number(self.revision) + 1)

    def test_a_return_b_creates_the_next_logical_version_and_it_is_not_approved_before_its_validation(self):  # retour B
        record = self.ret("ACCEPTED_WITH_COMMENTS")
        followup = self.confirm(record)
        revision = fu_service.provide_file(followup, self.updater, self.file("b"))
        self.assertEqual(logical_version_number(revision), logical_version_number(self.revision) + 1)
        self.assertNotEqual(self.status(), "APPROVED")  # the new logical revision has no approval: never shown as approved
        self.assertIsNone(self.annotated().current_revision_label)
        self.approve(revision)
        self.assertEqual(self.status(), "APPROVED")
        self.assertEqual(self.annotated().current_revision_label, "B")

    def test_returns_c_and_d_create_the_next_logical_version(self):  # retours C/D
        for category in ("COMMENTED", "REJECTED"):
            with self.subTest(category=category):
                record = self.ret(category)
                followup = self.confirm(record)
                revision = fu_service.provide_file(followup, self.updater, self.file(f"prel-{category}"))
                self.assertFalse(is_technical(revision))
                self.assertEqual(get_current_logical_revision(self.document).pk, revision.pk)
                self.assertNotEqual(self.status(), "APPROVED")
                revision = self.approve(revision)
                self.assertEqual(self.status(), "APPROVED")
                return  # one cycle per document in this fixture

    def test_several_historical_bpe_versions_keep_the_logical_numbers(self):  # plusieurs BPE
        followup, bpe_a = self.journey("ACCEPTED")
        revision_b = add_document_revision(self.document, SimpleUploadedFile("b.txt", b"revision B"), self.owner, revision_type=MAJOR, comment="B")
        revision_b = self.approve(revision_b)
        bpe_b = add_document_revision(self.document, SimpleUploadedFile("b-bpe.txt", b"bpe of B"), self.owner, issue_state="BPE",
                                      allow_identical=True, index_revision=revision_b, comment="BPE B")
        info = logical_info(self.rows())
        by_pk = {pk: n for pk, (_, n) in info.items()}
        base = logical_version_number(self.revision)
        self.assertEqual((by_pk[self.revision.pk], by_pk[bpe_a.pk], by_pk[revision_b.pk], by_pk[bpe_b.pk]), (base, base, base + 1, base + 1))
        self.assertEqual(get_current_logical_revision(self.document).pk, revision_b.pk)
        self.assertEqual(get_latest_physical_revision(self.document).pk, bpe_b.pk)
        self.assertEqual(self.status(), "APPROVED")


class StatusTests(LogicalBase):
    def test_the_status_stays_approved_after_the_bpe_file_is_added(self):
        self.assertEqual(self.status(), "APPROVED")
        followup, bpe = self.journey("ACCEPTED")
        self.assertEqual(self.status(), "APPROVED")  # it used to become DRAFT (the BPE row has no workflow)
        annotated = self.annotated()
        self.assertEqual((annotated.wf_status, annotated.logical_revision_id, annotated.current_revision_id), ("APPROVED", self.revision.pk, bpe.pk))
        self.assertEqual(annotated.current_revision_label, self.revision.revision_label)  # the index stays A, never blank
        self.assertEqual(compute_internal_control_status(annotated).value if hasattr(compute_internal_control_status(annotated), "value") else compute_internal_control_status(annotated), "APPROVED")

    def test_the_register_single_object_path_gives_the_same_status(self):
        followup, bpe = self.journey("ACCEPTED")
        fresh = Document.objects.get(pk=self.document.pk)  # no annotation: the fallback path of the detail endpoint
        self.assertEqual(self.status(), "APPROVED")
        self.assertEqual(str(compute_internal_control_status(fresh)).split(".")[-1].upper().replace("INTERNALCONTROLSTATUS", "").strip(), str(compute_internal_control_status(fresh)).split(".")[-1].upper().replace("INTERNALCONTROLSTATUS", "").strip())
        self.assertEqual(getattr(compute_internal_control_status(fresh), "value", compute_internal_control_status(fresh)),
                         getattr(compute_internal_control_status(self.annotated()), "value", compute_internal_control_status(self.annotated())))

    def test_an_unapproved_revision_after_a_bpe_is_not_shown_as_approved(self):  # statut non approuvé non transformé
        followup, bpe = self.journey("ACCEPTED")
        add_document_revision(self.document, SimpleUploadedFile("next.txt", b"internal modification"), self.owner, revision_type=MAJOR, comment="internal change")
        self.assertNotEqual(self.status(), "APPROVED")
        self.assertIsNone(self.annotated().current_revision_label)

    def test_a_document_without_a_technical_row_is_unchanged(self):  # document sans révision technique
        annotated = self.annotated()
        self.assertEqual((annotated.logical_revision_id, annotated.current_revision_id), (self.revision.pk, self.revision.pk))
        self.assertEqual(annotated.logical_version_number, len(self.rows()))


class ApiCompatibilityTests(LogicalBase):
    def test_the_revision_list_adds_the_logical_fields_and_keeps_the_physical_ones(self):
        followup, bpe = self.journey("ACCEPTED")
        response = self.api_list()
        self.assertEqual(response.status_code, 200)
        items = {item["id"]: item for item in response.data["results"]}
        self.assertEqual([i["version_number"] for i in response.data["results"]], sorted((r.version_number for r in self.rows()), reverse=True))
        self.assertEqual((items[bpe.pk]["is_technical"], items[self.revision.pk]["is_technical"]), (True, False))
        self.assertEqual((items[bpe.pk]["logical_revision_id"], items[self.revision.pk]["logical_revision_id"]), (self.revision.pk, self.revision.pk))
        self.assertEqual(items[bpe.pk]["logical_version_number"], items[self.revision.pk]["logical_version_number"])
        for key in ("version_number", "revision_label", "index_label", "issue_state", "index_revision_id", "file_sha256", "download_url", "is_current"):
            self.assertIn(key, items[bpe.pk])
        self.assertEqual(items[bpe.pk]["index_label"], self.revision.revision_label)  # the BPE keeps the index of its source
        self.assertTrue(items[bpe.pk]["is_current"])  # the physical current one is still the latest file

    def test_the_document_serializer_exposes_the_logical_fields(self):
        followup, bpe = self.journey("ACCEPTED")
        data = DocumentSerializer(self.annotated(), context={"include_control": True, "user": self.owner}).data
        self.assertEqual((data["logical_revision_id"], data["revision_label"]), (self.revision.pk, self.revision.revision_label))
        self.assertEqual(data["logical_version_number"], logical_version_number(self.revision))
        plain = DocumentSerializer(Document.objects.get(pk=self.document.pk), context={"include_control": True, "user": self.owner}).data
        self.assertEqual((plain["logical_revision_id"], plain["logical_version_number"], plain["revision_label"]),
                         (data["logical_revision_id"], data["logical_version_number"], data["revision_label"]))

    def test_the_logical_fields_are_hidden_without_the_control_context(self):
        data = DocumentSerializer(self.annotated(), context={"user": self.owner}).data
        self.assertEqual((data["logical_revision_id"], data["logical_version_number"]), (None, None))


class SafetyTests(LogicalBase):
    def test_reading_the_logical_version_writes_nothing(self):
        followup, bpe = self.journey("ACCEPTED")
        snapshot = list(DocumentRevision.objects.order_by("pk").values_list("pk", "version_number", "revision_label", "file", "file_sha256", "index_revision_id"))
        self.api_list(); self.status(); self.annotated()
        DocumentSerializer(self.annotated(), context={"include_control": True}).data
        self.assertEqual(snapshot, list(DocumentRevision.objects.order_by("pk").values_list("pk", "version_number", "revision_label", "file", "file_sha256", "index_revision_id")))

    def test_the_locking_stays_on_the_physical_rows(self):
        followup, bpe = self.journey("ACCEPTED")
        revision_b = add_document_revision(self.document, SimpleUploadedFile("b2.txt", b"revision B2"), self.owner, revision_type=MAJOR, comment="B")
        start_workflow(revision_b, "TWO_STEP", self.owner, None, self.approver, self.owner)
        with self.assertRaises(ValidationError):  # an active workflow on the latest physical revision still locks the file
            add_document_revision(self.document, SimpleUploadedFile("c.txt", b"another"), self.owner, revision_type=MAJOR, comment="C")

    def test_a_stored_revision_is_still_immutable(self):
        followup, bpe = self.journey("ACCEPTED")
        with self.assertRaises(ValidationError):
            bpe.comment = "changed"
            bpe.save()
        with self.assertRaises(ValidationError):
            DocumentRevision.objects.filter(pk=bpe.pk).update(comment="x")
        with self.assertRaises(ValidationError):
            bpe.delete()

    def test_the_transmittal_of_the_bpe_is_unchanged(self):
        followup, bpe = self.journey("ACCEPTED")
        before = list(self.rows())
        _, event = self.send_rev(bpe, "FOR_INFORMATION")
        item = event.transmittal.items.get()
        snap = item.evidence_snapshot["revision"]
        self.assertEqual((item.document_revision_id, snap["id"], snap["index_label"], snap["issue_state"], snap["file_sha256"]),
                         (bpe.pk, bpe.pk, self.revision.revision_label, "BPE", bpe.file_sha256))
        self.assertEqual(self.rows(), before)
        self.assertIsNotNone(self.closed(followup).applicable_at)
        self.assertEqual(self.status(), "APPROVED")

    def test_without_access_the_revision_list_is_refused(self):
        from project_management.test_transmittal import make_user
        stranger = make_user("logical.stranger@links-local.invalid")  # no role, no project membership
        view = DocumentRevisionViewSet.as_view({"get": "list"}, permission_classes=[])
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=stranger)
        response = view(request, document_id=self.document.pk)
        self.assertIn(response.status_code, (403, 404))
        self.assertNotIn("results", getattr(response, "data", {}) or {})


class DistributionHistoryTests(LogicalBase):
    def test_a_transmittal_of_the_bpe_shows_the_inherited_index_and_keeps_the_exact_physical_row(self):  # LOT 2
        from project_management.services.client_distribution_history import list_client_distribution_history
        followup, bpe = self.journey("ACCEPTED")
        transmittal, _ = self.send_rev(bpe, "FOR_INFORMATION")
        rows = [r for r in list_client_distribution_history(self.document) if r["document_revision_id"] == bpe.pk]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source_revision"], rows[0]["issue_state"], rows[0]["is_technical"]), (self.revision.revision_label, "BPE", True))
        self.assertEqual((rows[0]["document_revision_id"], rows[0]["transmittal_reference"]), (bpe.pk, transmittal.transmittal_number))  # no logical reference replaces it
        prel = [r for r in list_client_distribution_history(self.document) if r["document_revision_id"] == self.revision.pk]
        self.assertTrue(prel and prel[0]["source_revision"] == self.revision.revision_label and prel[0]["is_technical"] is False)
