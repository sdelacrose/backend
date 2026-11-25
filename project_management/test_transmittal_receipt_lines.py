"""GED-WF.3.1 point 2: supplier documents received on an incoming draft are PREPARED (TransmittalReceiptLine)
and only created -- document, revision, item -- by the atomic receipt confirmation."""

from io import StringIO
from unittest import mock

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    Document, DocumentOrigin, DocumentRevision, Folder, Transmittal, TransmittalEvent, TransmittalItem,
    TransmittalReceiptLine,
)
from project_management.models.transmittal import PENDING_RECEIPT_PREFIX
from project_management.services import transmittal_receipt
from project_management.services.document_revision import add_document_revision as real_add_document_revision
from project_management.services.transmittal_receipt import ReceiptAnomalies, confirm_incoming_receipt
from project_management.test_transmittal import make_user
from project_management.test_transmittal_receipt import ReceiptBase
from project_management.views.transmittal import (
    TransmittalConfirmReceiptView, TransmittalIssueView, TransmittalReceiptLineViewSet, TransmittalViewSet,
)


class LinesBase(ReceiptBase):
    def setUp(self):
        super().setUp()
        Folder.objects.get_or_create(project=self.project, name=f"{self.project.pk}_root_folder")
        self.updater = self.user_with("l.updater@links-local.invalid", self.project, ("update", "view"))

    def draft(self, number="L-1"):
        return self.create_in_transmittal(transmittal_number=number)

    def prepare(self, transmittal, user, reference="NEW-001", content=b"pending-bytes", **extra):
        data = {"reference": reference, "file": SimpleUploadedFile(f"{reference}.pdf", content), **extra}
        request = APIRequestFactory().post("/x/", data, format="multipart")
        force_authenticate(request, user=user)
        return TransmittalReceiptLineViewSet.as_view({"post": "create"}, permission_classes=[])(
            request, transmittal_id=transmittal.pk,
        )

    def lines_of(self, transmittal, user):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user)
        return TransmittalReceiptLineViewSet.as_view({"get": "list"}, permission_classes=[])(
            request, transmittal_id=transmittal.pk,
        )

    def delete_line(self, transmittal, line_id, user):
        request = APIRequestFactory().delete("/x/")
        force_authenticate(request, user=user)
        return TransmittalReceiptLineViewSet.as_view({"delete": "destroy"}, permission_classes=[])(
            request, transmittal_id=transmittal.pk, line_id=line_id,
        )

    def counts(self):
        return (Document.objects.count(), DocumentRevision.objects.count(), TransmittalItem.objects.count())

    def confirm(self, transmittal, user):
        with self.captureOnCommitCallbacks(execute=True):
            return self.post(TransmittalConfirmReceiptView, transmittal.pk, user)


class PreparationTests(LinesBase):
    # 1, 2, 3, 4
    def test_an_unknown_document_is_prepared_without_creating_anything_canonical(self):
        transmittal = self.draft()
        before = self.counts()
        response = self.prepare(transmittal, self.updater, "NEW-001", external_revision="B")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.counts(), before)  # no Document, no DocumentRevision, no TransmittalItem
        line = TransmittalReceiptLine.objects.get()
        self.assertEqual((line.reference, line.external_revision, line.decision), ("NEW-001", "B", "NEW_DOCUMENT"))
        self.assertTrue(line.file.name.startswith(PENDING_RECEIPT_PREFIX + "/"))
        self.assertTrue(default_storage.exists(line.file.name))
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")
        self.assertNotIn("file", response.data)  # the pending file is never exposed
        self.assertEqual(response.data["issuer_company"]["id"], self.supplier_company.pk)
        listing = self.lines_of(transmittal, self.updater)
        self.assertEqual([row["reference"] for row in listing.data], ["NEW-001"])

    # 5
    def test_the_draft_stays_editable_and_a_line_can_be_removed(self):
        transmittal = self.draft("L-2")
        self.prepare(transmittal, self.updater, "NEW-002")
        request = APIRequestFactory().patch("/x/", {"subject": "Edited"}, format="json")
        force_authenticate(request, user=self.updater)
        patched = TransmittalViewSet.as_view({"patch": "partial_update"}, permission_classes=[])(request, pk=transmittal.pk)
        self.assertEqual(patched.status_code, 200, getattr(patched, "data", None))
        line = TransmittalReceiptLine.objects.get()
        name = line.file.name
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.delete_line(transmittal, line.pk, self.updater).status_code, 204)
        self.assertFalse(TransmittalReceiptLine.objects.exists())
        self.assertFalse(default_storage.exists(name))

    # 6
    def test_deleting_the_draft_leaves_no_document_and_no_pending_file(self):
        transmittal = self.draft("L-3")
        self.prepare(transmittal, self.updater, "NEW-003")
        name = TransmittalReceiptLine.objects.get().file.name
        before = self.counts()
        with self.captureOnCommitCallbacks(execute=True):
            transmittal.delete()
        self.assertEqual(self.counts(), before)
        self.assertFalse(TransmittalReceiptLine.objects.exists())
        self.assertFalse(default_storage.exists(name))

    # 18
    def test_a_duplicate_reference_needs_an_explicit_confirmation(self):
        transmittal = self.draft("L-4")
        self.supplier_doc("DWG-100")
        refused = self.prepare(transmittal, self.updater, "dwg 100")  # same reference, other formatting
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(refused.data["code"], "duplicate_supplier_reference")
        self.assertFalse(TransmittalReceiptLine.objects.exists())
        accepted = self.prepare(transmittal, self.updater, "dwg 100", allow_duplicate_reference="true")
        self.assertEqual(accepted.status_code, 201, accepted.data)

    # 19
    def test_a_document_of_another_issuer_cannot_be_selected(self):
        transmittal = self.draft("L-5")
        other_doc, _ = self.supplier_doc("OTH-1", company=self.other_supplier_company)
        response = self.prepare(transmittal, self.updater, "OTH-1", matched_document=other_doc.pk)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TransmittalReceiptLine.objects.exists())

    def test_the_same_document_and_revision_cannot_be_prepared_twice(self):
        transmittal = self.draft("L-6")
        self.assertEqual(self.prepare(transmittal, self.updater, "DUP-1", external_revision="A").status_code, 201)
        self.assertEqual(self.prepare(transmittal, self.updater, "dup 1", external_revision="a").status_code, 400)
        self.assertEqual(self.prepare(transmittal, self.updater, "DUP-1", external_revision="B").status_code, 201)

    # 22
    def test_an_outgoing_transmittal_takes_no_prepared_line(self):
        out = self.create_out_transmittal()
        response = self.prepare(out, self.updater, "OUT-1")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(TransmittalReceiptLine.objects.exists())

    def test_a_received_transmittal_takes_no_new_line(self):
        transmittal = self.draft("L-7")
        self.prepare(transmittal, self.updater, "NEW-007")
        self.assertEqual(self.confirm(transmittal, self.updater).status_code, 200)
        self.assertEqual(self.prepare(transmittal, self.updater, "NEW-007B").status_code, 400)


class ConfirmationTests(LinesBase):
    # 7, 9, 10, 11
    def test_the_confirmation_creates_document_revision_item_and_event(self):
        transmittal = self.draft("C-1")
        self.prepare(transmittal, self.updater, "NEW-101", content=b"final-bytes", external_revision="C")
        name = TransmittalReceiptLine.objects.get().file.name
        self.assertEqual(self.counts(), (0 + Document.objects.count(), DocumentRevision.objects.count(), 0))
        before = self.counts()
        response = self.confirm(transmittal, self.updater)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        stored = Transmittal.objects.get(pk=transmittal.pk)
        self.assertEqual(stored.status, "ISSUED")
        document = Document.objects.get(identification_number="NEW-101")
        self.assertEqual(document.document_origin, DocumentOrigin.SUPPLIER)
        self.assertEqual(document.external_issuer_company_id, self.supplier_company.pk)
        self.assertEqual(document.folder.project_id, self.project.pk)
        revision = document.revisions.get()
        self.assertEqual(revision.external_revision, "C")
        revision.file.open("rb")
        self.assertEqual(revision.file.read(), b"final-bytes")
        revision.file.close()
        item = stored.items.get()
        self.assertEqual(item.document_revision_id, revision.pk)
        self.assertEqual(item.evidence_snapshot["revision"]["external_revision"], "C")
        event = stored.events.get(event_type="RECEIVED")
        self.assertEqual(event.actor_id, self.updater.pk)
        self.assertFalse(TransmittalReceiptLine.objects.exists())
        self.assertFalse(default_storage.exists(name))  # pending file cleaned once committed
        self.assertEqual(self.counts(), (before[0] + 1, before[1] + 1, before[2] + 1))

    def test_a_new_revision_of_an_existing_document_is_created_at_confirmation_only(self):
        transmittal = self.draft("C-2")
        document, first = self.supplier_doc("DWG-200", revision_label="A")
        response = self.prepare(transmittal, self.updater, "DWG-200", content=b"rev-2", external_revision="B",
                                matched_document=document.pk)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(TransmittalReceiptLine.objects.get().decision, "NEW_REVISION")
        revisions_before = document.revisions.count()  # nothing is added by the preparation
        self.assertEqual(document.revisions.count(), revisions_before)
        self.assertEqual(self.confirm(transmittal, self.updater).status_code, 200)
        self.assertEqual(document.revisions.count(), revisions_before + 1)
        self.assertEqual(Document.objects.filter(identification_number="DWG-200").count(), 1)
        first.refresh_from_db()
        self.assertEqual(first.external_revision, "A")  # the existing revision is untouched

    # 8
    def test_the_confirmation_needs_transmittal_update(self):
        transmittal = self.draft("C-3")
        self.prepare(transmittal, self.updater, "NEW-103")
        before = self.counts()
        for email, actions in (("c.issue@links-local.invalid", ("issue",)), ("c.view@links-local.invalid", ("view",))):
            user = self.user_with(email, self.project, actions)
            self.assertEqual(self.confirm(transmittal, user).status_code, 403, email)
        self.assertEqual(self.counts(), before)
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")
        self.assertTrue(TransmittalReceiptLine.objects.exists())

    # 12
    def test_a_second_confirmation_is_refused_without_a_new_event_or_document(self):
        transmittal = self.draft("C-4")
        self.prepare(transmittal, self.updater, "NEW-104")
        self.assertEqual(self.confirm(transmittal, self.updater).status_code, 200)
        before = (self.counts(), TransmittalEvent.objects.filter(transmittal=transmittal).count())
        second = self.confirm(transmittal, self.updater)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data["anomalies"][0]["code"], "already_received")
        self.assertEqual((self.counts(), TransmittalEvent.objects.filter(transmittal=transmittal).count()), before)

    # 13
    def test_a_failure_creating_the_document_rolls_everything_back(self):
        transmittal = self.draft("C-5")
        self.prepare(transmittal, self.updater, "NEW-105")
        before = self.counts()
        with mock.patch("project_management.serializers.document.DocumentSerializer.save",
                        side_effect=RuntimeError("document creation failed")):
            with self.assertRaises(RuntimeError):
                confirm_incoming_receipt(transmittal, self.updater)
        self.assert_untouched(transmittal, before)

    # 14
    def test_a_failure_creating_the_revision_rolls_the_new_document_back(self):
        transmittal = self.draft("C-6")
        self.prepare(transmittal, self.updater, "NEW-106")
        before = self.counts()
        with mock.patch("project_management.services.document_revision.add_document_revision",
                        side_effect=RuntimeError("revision failed")):
            with self.assertRaises(RuntimeError):
                confirm_incoming_receipt(transmittal, self.updater)
        self.assert_untouched(transmittal, before)
        self.assertFalse(Document.objects.filter(identification_number="NEW-106").exists())

    # 15
    def test_a_failure_in_the_finalization_rolls_back_documents_revisions_and_files(self):
        transmittal = self.draft("C-7")
        self.prepare(transmittal, self.updater, "NEW-107")
        before = self.counts()
        written = []

        def recording(*args, **kwargs):
            revision = real_add_document_revision(*args, **kwargs)
            written.append(revision.file.name)
            return revision

        with mock.patch("project_management.services.document_revision.add_document_revision", side_effect=recording), \
                mock.patch.object(transmittal_receipt, "finalize_transmittal", side_effect=RuntimeError("finalize failed")):
            with self.assertRaises(RuntimeError):
                confirm_incoming_receipt(transmittal, self.updater)
        self.assert_untouched(transmittal, before)
        self.assertEqual(len(written), 1)
        self.assertFalse(default_storage.exists(written[0]))  # the definitive file written meanwhile is removed

    # 16
    def test_a_storage_failure_on_a_later_line_finalizes_nothing_and_removes_the_earlier_file(self):
        transmittal = self.draft("C-8")
        self.prepare(transmittal, self.updater, "NEW-108A")
        self.prepare(transmittal, self.updater, "NEW-108B")
        before = self.counts()
        written, calls = [], []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("storage unavailable")
            revision = real_add_document_revision(*args, **kwargs)
            written.append(revision.file.name)
            return revision

        with mock.patch("project_management.services.document_revision.add_document_revision", side_effect=flaky):
            with self.assertRaises(OSError):
                confirm_incoming_receipt(transmittal, self.updater)
        self.assert_untouched(transmittal, before)
        self.assertEqual(TransmittalReceiptLine.objects.filter(transmittal=transmittal).count(), 2)
        self.assertFalse(default_storage.exists(written[0]))
        for line in TransmittalReceiptLine.objects.filter(transmittal=transmittal):
            self.assertTrue(default_storage.exists(line.file.name))  # still there to retry

    def assert_untouched(self, transmittal, before):
        self.assertEqual(self.counts(), before)
        stored = Transmittal.objects.get(pk=transmittal.pk)
        self.assertEqual(stored.status, "DRAFT")
        self.assertIsNone(stored.received_at)
        self.assertFalse(stored.events.filter(event_type="RECEIVED").exists())
        self.assertTrue(TransmittalReceiptLine.objects.filter(transmittal=transmittal).exists())

    # 18 (revalidation at confirmation)
    def test_a_reference_that_appeared_meanwhile_blocks_the_confirmation(self):
        transmittal = self.draft("C-9")
        self.prepare(transmittal, self.updater, "NEW-109")
        self.supplier_doc("new 109")  # created by someone else after the preparation
        before = self.counts()
        response = self.confirm(transmittal, self.updater)
        self.assertEqual(response.status_code, 400)
        self.assertIn("duplicate_canonical_document", [a["code"] for a in response.data["anomalies"]])
        self.assertEqual(self.counts(), before)

    # 19 (revalidation at confirmation)
    def test_an_issuer_change_after_the_preparation_blocks_the_confirmation(self):
        transmittal = self.draft("C-10")
        document, _ = self.supplier_doc("DWG-210")
        self.prepare(transmittal, self.updater, "DWG-210", matched_document=document.pk)
        Document.objects.filter(pk=document.pk).update(external_issuer_company=self.other_supplier_company)
        response = self.confirm(transmittal, self.updater)
        self.assertEqual(response.status_code, 400)
        self.assertIn("issuer_mismatch", [a["code"] for a in response.data["anomalies"]])

    def test_a_missing_pending_file_blocks_the_confirmation(self):
        transmittal = self.draft("C-11")
        self.prepare(transmittal, self.updater, "NEW-111")
        default_storage.delete(TransmittalReceiptLine.objects.get().file.name)
        response = self.confirm(transmittal, self.updater)
        self.assertEqual(response.status_code, 400)
        self.assertIn("line_file_missing", [a["code"] for a in response.data["anomalies"]])

    # 20, 21
    def test_a_historical_draft_with_items_and_no_line_is_confirmed_as_before(self):
        document, revision = self.supplier_doc("HIST-1")
        transmittal = self.in_draft("C-12", revision)
        before = self.counts()
        response = self.confirm(transmittal, self.updater)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(self.counts(), before)  # nothing created: the document already existed
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "ISSUED")

    def test_items_and_prepared_lines_are_received_together(self):
        document, revision = self.supplier_doc("HIST-2")
        transmittal = self.in_draft("C-13", revision)
        self.prepare(transmittal, self.updater, "NEW-113")
        self.assertEqual(self.confirm(transmittal, self.updater).status_code, 200)
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).items.count(), 2)

    # 22
    def test_issue_still_refuses_an_incoming_draft_with_lines_and_creates_nothing(self):
        transmittal = self.draft("C-14")
        self.prepare(transmittal, self.updater, "NEW-114")
        issuer = self.user_with("c.issuer@links-local.invalid", self.project, ("issue", "update"))
        before = self.counts()
        self.assertEqual(self.post(TransmittalIssueView, transmittal.pk, issuer).status_code, 400)
        self.assertEqual(self.counts(), before)


class IsolationAndCleanupTests(LinesBase):
    # 23
    def test_lines_are_isolated_by_project_and_company(self):
        transmittal = self.draft("I-1")
        self.prepare(transmittal, self.updater, "NEW-201")
        line = TransmittalReceiptLine.objects.get()
        elsewhere = self.user_with("i.other.project@links-local.invalid", self.other_project, ("update", "view"))
        outsider = make_user("i.other.company@links-local.invalid")  # another company, no membership at all
        outsider.company = self.other_company
        outsider.save(update_fields=["company"])
        viewer = self.user_with("i.viewer@links-local.invalid", self.project, ("view",))
        for user in (elsewhere, outsider):
            # GED-SEC.2 (SEC-06): another project / another company -> the Transmittal is outside the scope: 404
            self.assertEqual(self.lines_of(transmittal, user).status_code, 404, user.email)
            self.assertEqual(self.prepare(transmittal, user, "NEW-202").status_code, 404)
            self.assertEqual(self.delete_line(transmittal, line.pk, user).status_code, 404)
        self.assertEqual(self.lines_of(transmittal, viewer).status_code, 200)  # view may read
        self.assertEqual(self.prepare(transmittal, viewer, "NEW-203").status_code, 403)
        self.assertEqual(self.delete_line(transmittal, line.pk, viewer).status_code, 403)
        self.assertEqual(TransmittalReceiptLine.objects.count(), 1)

    def test_a_line_id_of_another_transmittal_is_not_found(self):
        first, second = self.draft("I-2"), self.draft("I-3")
        self.prepare(first, self.updater, "NEW-204")
        line = TransmittalReceiptLine.objects.get()
        response = self.delete_line(second, line.pk, self.updater)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(TransmittalReceiptLine.objects.filter(pk=line.pk).exists())

    # 17
    def test_the_manual_cleanup_only_removes_unreferenced_pending_files(self):
        transmittal = self.draft("I-4")
        self.prepare(transmittal, self.updater, "NEW-205")
        kept = TransmittalReceiptLine.objects.get().file.name
        orphan = default_storage.save(f"{PENDING_RECEIPT_PREFIX}/orphan_test/abandoned.pdf", SimpleUploadedFile("a.pdf", b"x"))
        outside = default_storage.save("wf31_not_pending/keep.pdf", SimpleUploadedFile("k.pdf", b"x"))
        try:
            out = StringIO()
            call_command("cleanup_receipt_pending_files", "--older-than-days", "0", stdout=out)
            self.assertIn(orphan, out.getvalue())
            self.assertNotIn(kept, out.getvalue())
            self.assertTrue(default_storage.exists(orphan))  # dry run deletes nothing
            call_command("cleanup_receipt_pending_files", "--older-than-days", "0", "--apply", stdout=StringIO())
            self.assertFalse(default_storage.exists(orphan))
            self.assertTrue(default_storage.exists(kept))
            self.assertTrue(default_storage.exists(outside))
        finally:
            for name in (orphan, outside):
                if default_storage.exists(name):
                    default_storage.delete(name)
