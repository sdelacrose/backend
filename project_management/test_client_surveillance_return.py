"""GED-WF.4.3: client returns (A/B/C/D), annotated files, corrections, controlled closure, late returns.
Real project roles, real send service, real EvidenceBlob storage: no permission stub besides the licence check."""
import io
import zipfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn, ClientSurveillanceReturnFile,
    DocumentRevision, EvidenceBlob, Transmittal, TransmittalEvent,
)
from project_management.services.client_surveillance_code import save_codes
from project_management.services.client_surveillance_return import detect_file_type, record_client_return
from project_management.test_client_surveillance_opening import OpeningBase
from project_management.views.client_surveillance import (
    ClientSurveillanceReturnFileView, ClientSurveillanceReturnView, ClientSurveillanceView,
)

MANAGE = "documents.client_surveillance_manage"
Category = ClientSurveillanceReturn.Category


def pdf(tag="a"):
    return SimpleUploadedFile(f"{tag}.pdf", b"%PDF-1.4\n% " + tag.encode() * 20, content_type="application/pdf")


def png(tag="p"):
    return SimpleUploadedFile(f"{tag}.png", b"\x89PNG\r\n\x1a\n" + tag.encode() * 20, content_type="image/png")


def office(tag, inner, ext):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(inner, tag)
    return SimpleUploadedFile(f"{tag}.{ext}", buffer.getvalue())


class ReturnBase(OpeningBase):
    def setUp(self):
        super().setUp()
        self.revision = self.review()
        self.document = self.revision.document
        self.transmittal, self.event = self.issue_and_send([(self.revision, "FOR_REVIEW")])
        self.surveillance = self.surveillances().get()
        self.client_issue = self.surveillance.client_issue
        self.manager = self.grant("wf43.manager@links-local.invalid", self.project, ["documents.view", MANAGE, "documents.update"])  # LOT 2: documents.update processes returns
        self.blobs0 = EvidenceBlob.objects.count()  # the send proofs already keep their own blobs

    def now(self):
        return timezone.now()

    def record(self, issue=None, **kw):
        values = dict(category=Category.ACCEPTED, received_at=self.now(), source_channel="EMAIL", comment="")
        values.update(kw)
        return record_client_return(issue or self.client_issue, self.manager, **values)

    def post(self, user, data=None, files=(), issue_id=None, document=None, kinds=()):
        body = {"category": "ACCEPTED", "received_at": self.now().isoformat(), "source_channel": "EMAIL"}
        body.update(data or {})
        if files:
            body["files"] = list(files)
        if kinds:
            body["file_kinds"] = list(kinds)
        request = APIRequestFactory().post("/x/", body, format="multipart")
        force_authenticate(request, user=user)
        return ClientSurveillanceReturnView.as_view()(
            request, document_id=(document or self.document).pk, issue_id=issue_id or self.client_issue.pk)

    def listing(self, user, document=None):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user)
        return ClientSurveillanceView.as_view()(request, document_id=(document or self.document).pk)

    def download(self, user, record, stored, document=None, issue_id=None):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=user)
        return ClientSurveillanceReturnFileView.as_view()(
            request, document_id=(document or self.document).pk, issue_id=issue_id or self.client_issue.pk,
            return_id=record.pk, file_id=stored.pk)

    def patch(self, user, body, issue_id=None):
        request = APIRequestFactory().patch("/x/", body, format="json")
        force_authenticate(request, user=user)
        return ClientSurveillanceView.as_view()(request, document_id=self.document.pk, issue_id=issue_id or self.client_issue.pk)

    def full_user(self, email="wf43.full@links-local.invalid"):
        return self.grant(email, self.project, ["documents.view", "documents.update", MANAGE])

    def incoming(self, **kw):
        values = dict(project=self.project, direction="IN", status="ISSUED", transmittal_number="CLIENT-IN-1",
                      external_company=self.client_company, received_at=self.now(), created_by=self.owner)
        values.update(kw)
        return Transmittal.objects.create(**values)

    def second_send(self):
        """The same revision sent again to the same client: a second SEND_SUCCEEDED on the SAME ClientIssue."""
        return self.issue_and_send([(self.revision, "FOR_REVIEW")])


class CategoriesAndSnapshotTests(ReturnBase):
    def test_each_category_is_recorded_with_its_default_code_and_the_required_comment(self):
        for category, code in (("ACCEPTED", "A"), ("ACCEPTED_WITH_COMMENTS", "B"), ("COMMENTED", "C"), ("REJECTED", "D")):
            record = self.record(category=category, comment="Please check" if category != "ACCEPTED" else "")
            self.assertEqual((record.category, record.code_snapshot), (category, code))
            self.assertEqual(record.recorded_by_id, self.manager.id)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 4)

    def test_a_comment_is_required_for_b_c_d_and_optional_for_a(self):
        for category in ("ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED"):
            with self.assertRaises(ValidationError):
                self.record(category=category, comment="  ")
        self.assertEqual(self.record(category="ACCEPTED").comment, "")
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_custom_project_code_is_frozen_and_survives_a_later_rename(self):
        save_codes(self.project, [{"category": "COMMENTED", "code": "OBS", "label_fr": "Observé", "label_en": "Observed"}], self.owner)
        record = self.record(category="COMMENTED", comment="x")
        self.assertEqual((record.code_snapshot, record.label_fr_snapshot, record.label_en_snapshot, record.label_de_snapshot),
                         ("OBS", "Observé", "Observed", None))
        save_codes(self.project, [{"category": "COMMENTED", "code": "ZZ", "label_fr": "Autre", "label_en": "Other"}], self.owner)
        record.refresh_from_db()
        self.assertEqual((record.code_snapshot, record.label_fr_snapshot), ("OBS", "Observé"))
        row = self.listing(self.manager).data["results"][0]
        self.assertEqual(row["returns"][0]["code"], "OBS")  # history shows the frozen code
        self.assertEqual(next(c["code"] for c in row["codes"] if c["category"] == "COMMENTED"), "ZZ")  # the form shows the current one

    def test_legacy_reviewed_is_kept_distinct_and_the_longest_category_fits(self):
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(status="CLOSED", result="REVIEWED")
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.result, "REVIEWED")
        self.assertNotEqual(self.surveillance.result, "ACCEPTED")
        row = self.listing(self.manager).data["results"][0]
        self.assertEqual(row["surveillance"]["result"], "REVIEWED")
        self.assertEqual(row["surveillance"]["retained_return_id"], None)
        ClientSurveillance.objects.filter(pk=self.surveillance.pk).update(result="ACCEPTED_WITH_COMMENTS")  # 22 characters
        self.surveillance.refresh_from_db()
        self.assertEqual(self.surveillance.result, "ACCEPTED_WITH_COMMENTS")

    def test_the_date_must_be_aware_not_future_and_not_before_the_send(self):
        with self.assertRaises(ValidationError):
            self.record(received_at=self.now() + timedelta(hours=1))
        with self.assertRaises(ValidationError):
            self.record(received_at=timezone.make_naive(self.now()))
        with self.assertRaises(ValidationError):
            self.record(received_at=self.event.created_at - timedelta(days=1))
        with self.assertRaises(ValidationError):
            self.record(source_channel="FAX")
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)

    def test_channels_are_a_closed_list(self):
        for channel in ("EMAIL", "PORTAL", "POSTAL_MAIL", "OTHER"):
            self.assertEqual(self.record(source_channel=channel).source_channel, channel)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillanceReturn.objects.create(client_issue=self.client_issue, received_at=self.now(), source_channel="SMS", category="ACCEPTED")


class ManualAndIncomingTests(ReturnBase):
    def test_manual_return_advances_status_and_preserves_other_data(self):
        revisions = DocumentRevision.objects.count()
        status_before = ("READY_FOR_CLOSURE", self.surveillance.due_at, self.surveillance.result)
        document_before = (self.document.status, self.document.document_origin)
        response = self.post(self.manager, {"category": "COMMENTED", "comment": "see markup", "source_reference": "CLI-77"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual((response.data["code"], response.data["source_reference"], response.data["late"]), ("C", "CLI-77", False))
        self.assertEqual(response.data["send_event_id"], self.event.pk)  # a single send is preselected
        self.assertIsNone(response.data["in_transmittal"])
        self.surveillance.refresh_from_db(); self.document.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.due_at, self.surveillance.result), status_before)
        self.assertEqual((self.document.status, self.document.document_origin), document_before)
        self.assertEqual(DocumentRevision.objects.count(), revisions)  # no automatic revision, nothing Applicable
        self.assertEqual(self.surveillance.events.filter(event_type="RETURN_RECORDED").count(), 1)

    def test_a_confirmed_incoming_transmittal_can_be_linked_and_serves_several_returns(self):
        incoming = self.incoming()
        second_revision = self.review()
        second_transmittal, _ = self.issue_and_send([(second_revision, "FOR_REVIEW")])
        second_issue = second_transmittal.items.get().client_issue
        first = self.record(in_transmittal_id=incoming.pk)
        other = record_client_return(second_issue, self.manager, category="ACCEPTED", received_at=self.now(),
                                     source_channel="POSTAL_MAIL", in_transmittal_id=incoming.pk)
        self.assertEqual((first.in_transmittal_id, other.in_transmittal_id), (incoming.pk, incoming.pk))
        self.assertEqual(Transmittal.objects.get(pk=incoming.pk).status, "ISSUED")  # the IN itself is never touched

    def test_unsuitable_incoming_transmittals_are_refused(self):
        refused = {
            "other project": self.incoming(project=self.other_project, transmittal_number="IN-P"),
            "other company": self.incoming(external_company=self.supplier_company, transmittal_number="IN-C"),
            "not confirmed (draft)": self.incoming(status="DRAFT", received_at=None, transmittal_number="IN-D"),
            "no reception date": self.incoming(received_at=None, transmittal_number="IN-N"),
            "outgoing": self.incoming(direction="OUT", transmittal_number="OUT-X"),
        }
        for label, transmittal in refused.items():
            with self.assertRaises(ValidationError, msg=label):
                self.record(in_transmittal_id=transmittal.pk)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)

    def test_the_wrong_revision_or_document_is_not_reachable(self):
        other_revision = self.review()
        other_transmittal, _ = self.issue_and_send([(other_revision, "FOR_REVIEW")])
        other_issue = other_transmittal.items.get().client_issue
        response = self.post(self.manager, issue_id=other_issue.pk)  # issue of ANOTHER document through this document
        self.assertEqual(response.status_code, 404)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)

    def test_a_send_of_another_client_issue_is_refused_and_several_sends_need_an_explicit_choice(self):
        other_revision = self.review()
        _, other_event = self.issue_and_send([(other_revision, "FOR_REVIEW")])
        with self.assertRaises(ValidationError):
            self.record(send_event_id=other_event.pk)
        _, second = self.second_send()
        with self.assertRaises(ValidationError):
            self.record()  # two sends: never guessed
        chosen = self.record(send_event_id=self.event.pk)
        self.assertEqual(chosen.send_event_id, self.event.pk)
        self.assertEqual(self.record(send_event_id=second.pk).send_event_id, second.pk)
        row = self.listing(self.manager).data["results"][0]
        self.assertEqual([e["id"] for e in row["send_events"]], [self.event.pk, second.pk])


class ForInformationTests(ReturnBase):
    def setUp(self):
        super().setUp()
        self.info_revision = self.review()
        self.info_transmittal, self.info_event = self.issue_and_send([(self.info_revision, "FOR_INFORMATION")])
        self.info_issue = self.info_transmittal.items.get().client_issue

    def test_spontaneous_return_without_result_is_kept_on_the_issue_without_any_surveillance(self):
        record = record_client_return(self.info_issue, self.manager, category=None, received_at=self.now(),
                                      source_channel="EMAIL", comment="Thank you, noted")
        self.assertIsNone(record.category)
        self.assertEqual(record.code_snapshot, "")
        self.assertFalse(ClientSurveillance.objects.filter(client_issue=self.info_issue).exists())
        item = self.info_transmittal.items.get(); item.refresh_from_db()
        self.assertEqual(item.distribution_purpose, "FOR_INFORMATION")  # the historical purpose is untouched
        row = next(r for r in self.listing(self.manager, self.info_revision.document).data["results"])
        self.assertEqual((row["surveillance"], len(row["returns"]), row["requires_category"]), (None, 1, False))

    def test_a_formal_result_communicated_on_for_information_is_recorded(self):
        record = record_client_return(self.info_issue, self.manager, category="COMMENTED", received_at=self.now(),
                                      source_channel="OTHER", comment="we have remarks")
        self.assertEqual(record.category, "COMMENTED")
        self.assertFalse(ClientSurveillance.objects.filter(client_issue=self.info_issue).exists())

    def test_an_empty_return_is_refused_and_for_review_always_needs_a_result(self):
        with self.assertRaises(ValidationError):
            record_client_return(self.info_issue, self.manager, category=None, received_at=self.now(), source_channel="EMAIL")
        with self.assertRaises(ValidationError):
            self.record(category=None, comment="no result given")
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)


class FilesTests(ReturnBase):
    def test_several_files_are_kept_and_identical_bytes_share_one_blob(self):
        first = self.record(uploads=[pdf("one"), png("two"), office("three", "word/document.xml", "docx"),
                                     office("four", "xl/workbook.xml", "xlsx")], kinds=["annotated", "comment_sheet", "other", "annotated"])
        self.assertEqual(first.files.count(), 4)
        self.assertEqual(sorted(first.files.values_list("kind", flat=True)), ["annotated", "annotated", "comment_sheet", "other"])
        self.assertEqual(EvidenceBlob.objects.count(), self.blobs0 + 4)
        second = self.record(category="REJECTED", comment="again", uploads=[pdf("one")])
        self.assertEqual(EvidenceBlob.objects.count(), self.blobs0 + 4)  # same company + SHA-256: no second copy
        self.assertEqual(second.files.get().blob_id, first.files.get(file_name="one.pdf").blob_id)
        stored = first.files.get(file_name="one.pdf")
        self.assertEqual(len(stored.sha256), 64)
        self.assertEqual(stored.file_size, stored.blob.file_size)
        self.assertEqual(stored.content_type, "application/pdf")  # canonical type, not the declared one

    def test_rows_and_files_are_immutable(self):
        record = self.record(uploads=[pdf("imm")])
        stored = record.files.get()
        for action in (lambda: setattr(record, "comment", "x") or record.save(), record.delete,
                       lambda: ClientSurveillanceReturn.objects.filter(pk=record.pk).update(comment="x"),
                       lambda: ClientSurveillanceReturn.objects.filter(pk=record.pk).delete(),
                       lambda: setattr(stored, "file_name", "x") or stored.save(), stored.delete,
                       lambda: ClientSurveillanceReturnFile.objects.filter(pk=stored.pk).update(file_name="x"),
                       lambda: ClientSurveillanceReturnFile.objects.filter(pk=stored.pk).delete(), stored.blob.delete):
            with self.assertRaises(ValidationError):
                action()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ClientSurveillanceReturnFile.objects.create(client_return=record, blob=stored.blob, file_name="dup.pdf",
                                                        content_type="application/pdf", file_size=stored.file_size, sha256=stored.sha256)

    @override_settings(TRANSMITTAL_PROOF_MAX_BYTES=100)
    def test_the_size_limit_of_the_send_proofs_applies(self):
        big = SimpleUploadedFile("big.pdf", b"%PDF-1.4\n" + b"x" * 500)
        with self.assertRaises(ValidationError):
            self.record(uploads=[big])
        self.assertEqual((ClientSurveillanceReturn.objects.count(), EvidenceBlob.objects.count()), (0, self.blobs0))

    def test_only_real_pdf_docx_xlsx_png_jpeg_are_accepted(self):
        bad = {
            "executable": SimpleUploadedFile("tool.exe", b"MZ\x90\x00 binary"),
            "text renamed pdf": SimpleUploadedFile("note.pdf", b"just text"),
            "pdf renamed png": SimpleUploadedFile("fake.png", b"%PDF-1.4\n data"),
            "zip renamed docx": SimpleUploadedFile("z.docx", b"PK\x03\x04" + b"0" * 30),
            "plain zip": SimpleUploadedFile("p.docx", office("t", "other.xml", "docx").read()),
            "empty": SimpleUploadedFile("empty.pdf", b""),
            "no extension": SimpleUploadedFile("noext", b"%PDF-1.4\n data"),
        }
        for label, upload in bad.items():
            with self.assertRaises(ValidationError, msg=label):
                self.record(uploads=[upload])
        self.assertEqual(detect_file_type(b"\xff\xd8\xff\xe0 jpeg"), "jpeg")
        jpeg = SimpleUploadedFile("photo.JPEG", b"\xff\xd8\xff\xe0 jpeg data")
        self.assertEqual(self.record(uploads=[jpeg]).files.get().content_type, "image/jpeg")
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_a_storage_failure_leaves_no_return_no_row_and_no_orphan_file(self):
        written, original_save = [], FileSystemStorage.save

        def failing_save(storage, name, content, max_length=None):
            if len(written) == 1:
                raise OSError("disk full")
            result = original_save(storage, name, content, max_length)
            written.append((storage, result))
            return result

        with patch.object(FileSystemStorage, "save", failing_save), self.assertRaises(OSError):
            self.record(uploads=[pdf("first"), png("second")])
        self.assertEqual((ClientSurveillanceReturn.objects.count(), ClientSurveillanceReturnFile.objects.count(),
                          EvidenceBlob.objects.count()), (0, 0, self.blobs0))
        self.assertEqual(len(written), 1)
        self.assertFalse(written[0][0].exists(written[0][1]))  # the file written for the failed attempt is removed
        self.assertEqual(self.surveillance.events.filter(event_type__startswith="RETURN").count(), 0)

    def test_a_failure_after_the_files_rolls_everything_back_but_never_deletes_a_shared_blob(self):
        shared = self.record(uploads=[pdf("shared")]).files.get().blob
        shared_path = shared.file.name
        written, original_save = [], FileSystemStorage.save

        def recording_save(storage, name, content, max_length=None):
            result = original_save(storage, name, content, max_length)
            written.append((storage, result))
            return result

        with patch.object(FileSystemStorage, "save", recording_save),                 patch.object(ClientSurveillanceEvent.objects, "create", side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            self.record(category="REJECTED", comment="x", uploads=[pdf("shared"), png("new")])
        self.assertEqual((ClientSurveillanceReturn.objects.count(), ClientSurveillanceReturnFile.objects.count()), (1, 1))
        self.assertEqual(EvidenceBlob.objects.count(), self.blobs0 + 1)
        self.assertTrue(shared.file.storage.exists(shared_path))  # the shared blob is untouched
        for storage, name in written:  # whatever this attempt wrote is either referenced or gone, never orphaned
            self.assertEqual(storage.exists(name), EvidenceBlob.objects.filter(file=name).exists())
        self.assertGreaterEqual(len(written), 1)

    def test_secure_download_is_a_forced_attachment_with_a_neutral_header(self):
        record = self.record(uploads=[pdf("dl")]); stored = record.files.get()
        response = self.download(self.manager, record, stored)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual((response["Content-Type"], response["X-Content-Type-Options"]), ("application/pdf", "nosniff"))
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF"))
        listed = self.listing(self.manager).data["results"][0]["returns"][0]["files"][0]
        self.assertNotIn("media", listed["download_url"])  # an API route, never a public storage link
        self.assertNotIn("file_url", listed)


class CorrectionAndClosureTests(ReturnBase):
    def setUp(self):
        super().setUp()
        # LOT 1: these cases cover the HISTORICAL manual path (a surveillance with no automated follow-up, e.g. returns
        # recorded before the follow-up existed). The automated path is covered by test_client_surveillance_followup.
        patcher = patch("project_management.services.client_surveillance_followup.register_return_safely")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_correction_keeps_the_previous_return_and_needs_a_reason(self):
        original = self.record(category="REJECTED", comment="wrong drawing", uploads=[pdf("orig")])
        with self.assertRaises(ValidationError):
            self.record(category="ACCEPTED", supersedes_id=original.pk)  # no reason
        correction = self.record(category="ACCEPTED", supersedes_id=original.pk, correction_reason="typed the wrong code")
        self.assertEqual(correction.supersedes_id, original.pk)
        original.refresh_from_db()
        self.assertEqual((original.category, original.comment, original.files.count()), ("REJECTED", "wrong drawing", 1))
        row = self.listing(self.manager).data["results"][0]
        links = {r["id"]: (r["supersedes_id"], r["superseded_by_id"]) for r in row["returns"]}
        self.assertEqual(links, {original.pk: (None, correction.pk), correction.pk: (original.pk, None)})
        self.assertEqual(self.surveillance.events.filter(event_type="RETURN_CORRECTED").count(), 1)

    def test_a_return_is_corrected_once_only_and_never_across_issues(self):
        original = self.record(category="COMMENTED", comment="c")
        self.record(category="ACCEPTED", supersedes_id=original.pk, correction_reason="mistake")
        with self.assertRaises(ValidationError):
            self.record(category="REJECTED", comment="r", supersedes_id=original.pk, correction_reason="again")
        with self.assertRaises(IntegrityError), transaction.atomic():  # the database refuses it as well
            ClientSurveillanceReturn.objects.create(client_issue=self.client_issue, received_at=self.now(), source_channel="EMAIL",
                                                    category="REJECTED", supersedes=original, correction_reason="race")
        other_revision = self.review()
        other_transmittal, _ = self.issue_and_send([(other_revision, "FOR_REVIEW")])
        with self.assertRaises(ValidationError):
            record_client_return(other_transmittal.items.get().client_issue, self.manager, category="ACCEPTED", received_at=self.now(),
                                 source_channel="EMAIL", supersedes_id=original.pk, correction_reason="foreign")
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 2)

    def test_contradictory_returns_are_all_kept_and_nothing_is_retained_automatically(self):
        self.record(category="ACCEPTED")
        self.record(category="REJECTED", comment="we changed our mind", received_at=self.now())
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("READY_FOR_CLOSURE", None))
        self.assertEqual(self.listing(self.manager).data["results"][0]["surveillance"]["retained_return_id"], None)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 2)

    def advance(self, user):
        self.assertEqual(self.patch(user, {"status": "READY_FOR_CLOSURE"}).status_code, 200)

    def test_closure_needs_an_explicit_valid_retained_return_and_records_it(self):
        user = self.full_user()
        first = self.record(category="ACCEPTED_WITH_COMMENTS", comment="minor remarks")
        later = self.record(category="REJECTED", comment="contradiction")
        self.advance(user)
        self.assertEqual(self.patch(user, {"status": "CLOSED"}).status_code, 400)  # nothing chosen
        self.assertEqual(self.patch(user, {"status": "CLOSED", "result": "ACCEPTED"}).status_code, 400)  # no manual result
        self.assertEqual(self.patch(user, {"status": "CLOSED", "retained_return": 999999}).status_code, 400)
        self.assertEqual(self.surveillance.__class__.objects.get(pk=self.surveillance.pk).status, "READY_FOR_CLOSURE")
        response = self.patch(user, {"status": "CLOSED", "retained_return": first.pk})  # not the latest: chosen, not inferred
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["status"], response.data["result"], response.data["retained_return_id"]),
                         ("CLOSED", "ACCEPTED_WITH_COMMENTS", first.pk))
        self.assertEqual(response.data["comment"], "minor remarks")
        closing = self.surveillance.events.filter(status="CLOSED").get()
        self.assertEqual(closing.data, {"retained_return": first.pk, "category": "ACCEPTED_WITH_COMMENTS", "code": "B"})
        self.assertEqual(closing.result, "ACCEPTED_WITH_COMMENTS")
        self.assertNotEqual(later.pk, first.pk)

    def test_a_replaced_return_foreign_return_or_uncategorised_return_cannot_be_retained(self):
        user = self.full_user()
        original = self.record(category="COMMENTED", comment="c")
        correction = self.record(category="ACCEPTED", supersedes_id=original.pk, correction_reason="typo")
        other_revision = self.review()
        other_transmittal, _ = self.issue_and_send([(other_revision, "FOR_REVIEW")])
        foreign = record_client_return(other_transmittal.items.get().client_issue, self.manager, category="ACCEPTED",
                                       received_at=self.now(), source_channel="EMAIL")
        self.advance(user)
        for label, candidate in (("replaced", original.pk), ("other issue", foreign.pk)):
            self.assertEqual(self.patch(user, {"status": "CLOSED", "retained_return": candidate}).status_code, 400, label)
        self.assertEqual(self.patch(user, {"status": "CLOSED", "retained_return": correction.pk}).status_code, 200)

    def test_closing_with_documents_update_alone_is_refused_but_intermediate_steps_still_work(self):
        updater = self.grant("wf43.updater@links-local.invalid", self.project, ["documents.view", "documents.update"])
        record = self.record(category="ACCEPTED")
        self.advance(updater)
        response = self.patch(updater, {"status": "CLOSED", "retained_return": record.pk})
        self.assertEqual(response.status_code, 403)
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result), ("READY_FOR_CLOSURE", None))
        manager_only = self.grant("wf43.mgr2@links-local.invalid", self.project, ["documents.view", MANAGE])
        self.assertEqual(self.patch(manager_only, {"status": "CLOSED", "retained_return": record.pk}).status_code, 200)

    def test_a_late_return_is_information_only_and_changes_nothing(self):
        user = self.full_user()
        retained = self.record(category="COMMENTED", comment="c")
        self.advance(user)
        self.assertEqual(self.patch(user, {"status": "CLOSED", "retained_return": retained.pk}).status_code, 200)
        self.surveillance.refresh_from_db()
        frozen = (self.surveillance.status, self.surveillance.result, self.surveillance.due_at, self.surveillance.closed_at,
                  self.surveillance.comment)
        revisions = DocumentRevision.objects.count()
        response = self.post(user, {"category": "ACCEPTED", "comment": "late news"})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["late"])
        self.surveillance.refresh_from_db()
        self.assertEqual((self.surveillance.status, self.surveillance.result, self.surveillance.due_at, self.surveillance.closed_at,
                          self.surveillance.comment), frozen)
        self.assertEqual(DocumentRevision.objects.count(), revisions)
        self.assertEqual(self.surveillance.events.filter(event_type="RETURN_LATE").count(), 1)
        row = self.listing(user).data["results"][0]
        self.assertEqual(row["surveillance"]["retained_return_id"], retained.pk)
        self.assertEqual([r["late"] for r in row["returns"]], [False, True])
        self.assertEqual(self.patch(user, {"status": "UNDER_REVIEW"}).status_code, 400)  # never reopened


class PermissionAndIsolationTests(ReturnBase):
    def test_reader_consults_the_summary_but_cannot_download_or_record(self):
        record = self.record(uploads=[pdf("perm")])
        reader = self.grant("wf43.reader@links-local.invalid", self.project, ["documents.view"])
        row = self.listing(reader).data["results"][0]
        # GED-SEC.2 (SEC-02/03): documents.view alone gives the summary (result, code) -- no file, no download
        self.assertEqual((row["can_manage"], len(row["returns"]), len(row["returns"][0]["files"])), (False, 1, 0))
        self.assertEqual(row["returns"][0]["category"], record.category)
        self.assertNotIn("send_events", row)
        self.assertEqual(self.download(reader, record, record.files.get()).status_code, 403)
        entitled = self.grant("wf43.tview@links-local.invalid", self.project, ["documents.view", "documents.transmittal_view"])
        self.assertEqual(self.download(entitled, record, record.files.get()).status_code, 200)
        self.assertEqual(self.post(reader).status_code, 403)

    def test_update_records_and_other_rights_do_not_substitute(self):
        self.assertEqual(self.post(self.manager).status_code, 201)  # LOT 2: documents.update processes the return
        for name, keys in {"manage_only": ["documents.view", MANAGE],
                           "issue": ["documents.view", "documents.transmittal_issue", "documents.transmittal_update"],
                           "receipt": ["documents.view", "documents.transmittal_update", "documents.transmittal_view"]}.items():
            user = self.grant(f"wf43.{name}@links-local.invalid", self.project, keys)
            self.assertEqual(self.post(user).status_code, 403, name)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_attaching_an_incoming_transmittal_also_needs_transmittal_view_on_it(self):
        incoming = self.incoming()
        self.assertEqual(self.listing(self.manager).data["results"][0]["in_transmittals"], [])
        self.assertEqual(self.post(self.manager, {"in_transmittal": incoming.pk}).status_code, 403)
        both = self.grant("wf43.both@links-local.invalid", self.project, ["documents.view", "documents.update", "documents.transmittal_view"])
        listed = self.listing(both).data["results"][0]["in_transmittals"]
        self.assertEqual([t["id"] for t in listed], [incoming.pk])
        response = self.post(both, {"in_transmittal": incoming.pk})
        self.assertEqual((response.status_code, response.data["in_transmittal"]["id"]), (201, incoming.pk))
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_projects_and_companies_are_isolated(self):
        record = self.record(uploads=[pdf("iso")]); stored = record.files.get()
        elsewhere = self.grant("wf43.elsewhere@links-local.invalid", self.project, ["documents.view"])
        self.grant("wf43.elsewhere@links-local.invalid", self.other_project, [MANAGE], user=elsewhere)
        self.assertEqual(self.post(elsewhere).status_code, 403)  # the right of another project does not apply
        outsider = self.grant("wf43.outsider@links-local.invalid", self.other_tenant_project, ["documents.view", MANAGE],
                              company=self.other_company)
        self.assertEqual(self.post(outsider).status_code, 404)
        self.assertEqual(self.listing(outsider).status_code, 404)
        self.assertEqual(self.download(outsider, record, stored).status_code, 404)
        # a file id never opens through another document or another issue
        other_revision = self.review()
        other_transmittal, _ = self.issue_and_send([(other_revision, "FOR_REVIEW")])
        other_issue = other_transmittal.items.get().client_issue
        self.assertEqual(self.download(self.manager, record, stored, document=other_revision.document, issue_id=other_issue.pk).status_code, 404)
        self.assertEqual(self.download(self.manager, record, stored, issue_id=other_issue.pk).status_code, 404)


class NoDocumentaryConsequenceTests(ReturnBase):
    def test_no_category_changes_the_document_its_revisions_or_the_register_status(self):
        from project_management.services.document_register_status import ClientControlStatus  # noqa: F401
        before = (DocumentRevision.objects.count(), list(DocumentRevision.objects.values_list("pk", "revision_label")),
                  self.document.status, TransmittalEvent.objects.count(), Transmittal.objects.count())
        for category in ("ACCEPTED", "ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED"):
            self.record(category=category, comment="note")
        self.document.refresh_from_db()
        after = (DocumentRevision.objects.count(), list(DocumentRevision.objects.values_list("pk", "revision_label")),
                 self.document.status, TransmittalEvent.objects.count(), Transmittal.objects.count())
        self.assertEqual(before, after)
        self.assertEqual(ClientIssue.objects.filter(pk=self.client_issue.pk).count(), 1)
