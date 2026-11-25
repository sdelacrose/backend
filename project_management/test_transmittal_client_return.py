"""GED-WF.4.3.1: client returns received through an incoming Transmittal (hybrid qualification).
Real project roles, real send/finalize/confirm services, real EvidenceBlob storage; only the licence check is stubbed."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.files.storage import FileSystemStorage, default_storage
from django.core.management import call_command
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import (
    ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn, DocumentRevision, EvidenceBlob,
    SupplierSurveillance, Transmittal, TransmittalClientReturnLine, TransmittalClientReturnLineFile, TransmittalEvent,
)
from project_management.models.transmittal_client_return_line import PENDING_CLIENT_RETURN_PREFIX
from project_management.services import transmittal_receipt
from project_management.services.transmittal_client_return import (
    add_client_return_line, line_state, qualify_client_return_line, remove_client_return_line,
)
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_receipt import ReceiptAnomalies, add_receipt_line, confirm_incoming_receipt
from project_management.test_client_surveillance_opening import OpeningBase
from project_management.test_client_surveillance_return import pdf, png
from project_management.views.transmittal import TransmittalConfirmReceiptView
from project_management.views.transmittal_client_return import (
    TransmittalClientReturnFileView, TransmittalClientReturnLineViewSet, TransmittalClientReturnMatchView,
    TransmittalClientReturnQualifyView,
)

MANAGE = "documents.client_surveillance_manage"
VIEW, UPDATE, TVIEW = "documents.view", "documents.transmittal_update", "documents.transmittal_view"


class ClientInBase(OpeningBase):
    def setUp(self):
        super().setUp()
        self.rev1, self.rev2, self.rev3 = self.review(), self.review(), self.review()
        self.issue1, self.issue2, self.issue3 = [self.sent_issue(r) for r in (self.rev1, self.rev2, self.rev3)]
        self.receiver = self.grant("in.receiver@links-local.invalid", self.project, [VIEW, UPDATE])
        self.controller = self.grant("in.controller@links-local.invalid", self.project, [VIEW, UPDATE, TVIEW, MANAGE, "documents.update"])
        self.qualifier = self.grant("in.qualifier@links-local.invalid", self.project, [VIEW, TVIEW, MANAGE, "documents.update"])  # LOT 2: documents.update qualifies
        self.reader = self.grant("in.reader@links-local.invalid", self.project, [VIEW, TVIEW])
        self.blind = self.grant("in.blind@links-local.invalid", self.project, [UPDATE])
        self.full = self.grant("in.full@links-local.invalid", self.project, ["documents.full_access"])
        self.blobs0 = EvidenceBlob.objects.count()
        self.counter = 0

    def sent_issue(self, revision, purpose="FOR_REVIEW"):
        transmittal, _ = self.issue_and_send([(revision, purpose)])
        return transmittal.items.get().client_issue

    def draft(self, company=None, project=None, **kw):
        self.counter += 1
        values = dict(project=project or self.project, direction="IN", status="DRAFT", transmittal_number=f"CLIENT-IN-{self.counter}",
                      external_company=company or self.client_company, created_by=self.owner)
        values.update(kw)
        return Transmittal.objects.create(**values)

    def line(self, transmittal, issue, **kw):
        values = dict(category="COMMENTED", received_at=timezone.now(), source_channel="EMAIL", comment="see marked-up files")
        values.update(kw)
        return add_client_return_line(transmittal, self.owner, client_issue_id=issue.pk, **values)

    def confirm(self, transmittal, user=None):
        return confirm_incoming_receipt(Transmittal.objects.get(pk=transmittal.pk), user or self.controller)

    def call(self, view, actions, user, method="get", data=None, fmt="multipart", **kwargs):
        factory = APIRequestFactory()
        request = getattr(factory, method)("/x/", data, format=fmt) if method != "get" else factory.get("/x/", data or {})
        force_authenticate(request, user=user)
        return view.as_view(actions)(request, **kwargs)

    def post_line(self, user, transmittal, issue, files=(), **data):
        body = {"client_issue": issue.pk, "category": "ACCEPTED", "received_at": timezone.now().isoformat(), "source_channel": "EMAIL"}
        body.update(data)
        if files:
            body["files"] = list(files)
        return self.call(TransmittalClientReturnLineViewSet, {"post": "create"}, user, "post", body, transmittal_id=transmittal.pk)


class AddLineTests(ClientInBase):
    def test_a_line_keeps_its_own_result_comments_and_pending_files_and_creates_nothing_canonical(self):
        draft = self.draft()
        revisions = DocumentRevision.objects.count()
        line = self.line(draft, self.issue1, uploads=[pdf("one"), png("two")], kinds=["annotated", "comment_sheet"],
                         client_reference="CLI-REF-9", client_revision="C3")
        self.assertEqual((line.category, line.client_reference, line.client_revision, line_state(line)), ("COMMENTED", "CLI-REF-9", "C3", "DRAFT"))
        self.assertEqual(line.send_event_id, TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", transmittal__items__client_issue=self.issue1).get().pk)
        files = list(line.files.all())
        self.assertEqual(len(files), 2)
        for row in files:
            self.assertTrue(row.file.name.startswith(PENDING_CLIENT_RETURN_PREFIX + "/"))
            self.assertIsNone(row.blob_id)
            self.assertTrue(row.file.storage.exists(row.file.name))
        self.assertEqual((draft.items.count(), draft.receipt_lines.count(), DocumentRevision.objects.count()), (0, 0, revisions))
        self.assertEqual(EvidenceBlob.objects.count(), self.blobs0)  # no blob before the confirmation
        self.issue1.refresh_from_db()
        self.assertIsNone(self.issue1.client_revision_label)  # the client index is informative only

    def test_unsuitable_distributions_are_refused(self):
        other_company_in = self.draft(company=self.supplier_company)
        with self.assertRaises(ValidationError):
            self.line(other_company_in, self.issue1)  # another client company
        other_project_in = self.draft(project=self.other_project)
        with self.assertRaises(ValidationError):
            self.line(other_project_in, self.issue1)  # another project
        withdrawn = self.review()
        issue = self.sent_issue(withdrawn)
        withdrawn.document.withdrawn_at = timezone.now()
        withdrawn.document.save(update_fields=["withdrawn_at"])
        with self.assertRaises(ValidationError):
            self.line(self.draft(), issue)  # D5: a withdrawn document is never linked
        self.assertEqual(TransmittalClientReturnLine.objects.count(), 0)

    def test_the_send_must_belong_to_the_issue_and_several_sends_need_an_explicit_choice(self):
        _, foreign = self.issue_and_send([(self.review(), "FOR_REVIEW")])
        draft = self.draft()
        with self.assertRaises(ValidationError):
            self.line(draft, self.issue1, send_event_id=foreign.pk)
        _, second = self.issue_and_send([(self.rev1, "FOR_REVIEW")])  # the same revision sent again
        with self.assertRaises(ValidationError):
            self.line(draft, self.issue1)  # two sends: never guessed
        first = TransmittalEvent.objects.filter(event_type="SEND_SUCCEEDED", transmittal__items__client_issue=self.issue1).order_by("pk").first()
        self.assertEqual(self.line(draft, self.issue1, send_event_id=first.pk).send_event_id, first.pk)
        self.assertNotEqual(first.pk, second.pk)

    def test_an_out_that_was_never_sent_needs_an_explicit_written_confirmation_and_no_send_is_invented(self):
        issued = self.issue([(self.review(), "FOR_REVIEW")])
        issue = issued.items.get().client_issue
        events, surveillances = TransmittalEvent.objects.count(), ClientSurveillance.objects.count()
        draft = self.draft()
        with self.assertRaises(ValidationError):
            self.line(draft, issue)
        line = self.line(draft, issue, no_send_justification="Delivered by hand at the meeting of 12 November.")
        self.assertIsNone(line.send_event_id)
        self.assertEqual(line.no_send_justification, "Delivered by hand at the meeting of 12 November.")
        record_event = self.confirm(draft).events.get(event_type="RECEIVED")
        self.assertEqual(record_event.metadata["client_return_lines"][0]["no_send_proof"], True)
        record = line.__class__.objects.get(pk=line.pk).client_return
        self.assertIsNone(record.send_event_id)
        self.assertEqual(TransmittalEvent.objects.count(), events + 1)  # only RECEIVED: no SEND_SUCCEEDED invented
        self.assertEqual(ClientSurveillance.objects.count(), surveillances)  # no surveillance opened artificially

    def test_one_line_per_distribution_and_transmittal(self):
        draft = self.draft()
        self.line(draft, self.issue1)
        with self.assertRaises(ValidationError):
            self.line(draft, self.issue1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            TransmittalClientReturnLine.objects.create(transmittal=draft, client_issue=self.issue1, received_at=timezone.now(), source_channel="EMAIL", category="ACCEPTED")
        self.assertEqual(self.line(self.draft(), self.issue1).category, "COMMENTED")  # another IN may carry another return

    def test_comments_files_and_date_rules(self):
        draft = self.draft()
        for category in ("ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED"):
            with self.assertRaises(ValidationError):
                self.line(draft, self.issue1, category=category, comment=" ")
        with self.assertRaises(ValidationError):
            self.line(draft, self.issue1, received_at=timezone.now() + timedelta(hours=2))
        with self.assertRaises(ValidationError):
            self.line(draft, self.issue1, uploads=[SimpleUpload("tool.exe", b"MZ binary")])
        self.assertEqual((TransmittalClientReturnLine.objects.count(), TransmittalClientReturnLineFile.objects.count()), (0, 0))

    def test_removing_a_draft_line_removes_its_pending_files_once_committed_and_a_draft_with_lines_can_be_deleted(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, uploads=[pdf("rm")])
        name = line.files.get().file.name
        with self.captureOnCommitCallbacks(execute=True):
            remove_client_return_line(line)
        self.assertFalse(default_storage.exists(name))
        line2 = self.line(draft, self.issue2, uploads=[pdf("rm2")])
        from project_management.views.transmittal import TransmittalViewSet
        with self.captureOnCommitCallbacks(execute=True):
            TransmittalViewSet().perform_destroy(draft)
        self.assertFalse(Transmittal.objects.filter(pk=draft.pk).exists())
        self.assertFalse(TransmittalClientReturnLine.objects.filter(pk=line2.pk).exists())


def SimpleUpload(name, content):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, content)


class ConfirmationTests(ClientInBase):
    def test_a_client_only_in_is_confirmed_and_the_returns_are_created_in_the_same_transaction(self):
        draft = self.draft()
        l1 = self.line(draft, self.issue1, category="ACCEPTED", comment="", uploads=[pdf("a1")])
        l2 = self.line(draft, self.issue2, category="REJECTED", comment="does not comply", uploads=[pdf("b1"), png("b2")])
        revisions, surveillance_status = DocumentRevision.objects.count(), list(ClientSurveillance.objects.values_list("pk", "status", "result"))
        with self.captureOnCommitCallbacks(execute=True):
            confirmed = self.confirm(draft)
        self.assertEqual((confirmed.status, confirmed.items.count()), ("ISSUED", 0))
        received = confirmed.events.filter(event_type="RECEIVED")
        self.assertEqual(received.count(), 1)
        self.assertEqual({m["line"] for m in received.get().metadata["client_return_lines"]}, {l1.pk, l2.pk})
        r1, r2 = [TransmittalClientReturnLine.objects.get(pk=l.pk).client_return for l in (l1, l2)]
        self.assertEqual((r1.category, r1.in_transmittal_id, r1.code_snapshot), ("ACCEPTED", draft.pk, "A"))
        self.assertEqual((r2.category, r2.comment, r2.files.count()), ("REJECTED", "does not comply", 2))
        for row in TransmittalClientReturnLineFile.objects.filter(line__transmittal=draft):
            self.assertIsNotNone(row.blob_id)  # kept as an EvidenceBlob...
            self.assertFalse(row.file)  # ...and the pending copy is cleared
        self.assertEqual(EvidenceBlob.objects.count(), self.blobs0 + 3)
        self.assertEqual(r2.files.get(file_name="b1.pdf").blob_id, TransmittalClientReturnLineFile.objects.get(line=l2, file_name="b1.pdf").blob_id)
        self.assertEqual(DocumentRevision.objects.count(), revisions)  # nothing canonical created
        self.assertEqual(list(ClientSurveillance.objects.values_list("pk", "status", "result")), surveillance_status)  # nothing closed/changed
        events = ClientSurveillanceEvent.objects.filter(event_type="RETURN_RECORDED").order_by("pk")
        self.assertEqual({e.data["in_line"] for e in events}, {l1.pk, l2.pk})

    def test_for_information_without_result_creates_a_return_but_no_surveillance(self):
        issue = self.sent_issue(self.review(), "FOR_INFORMATION")
        draft = self.draft()
        line = self.line(draft, issue, category=None, comment="Thank you, noted")
        self.confirm(draft)
        record = TransmittalClientReturnLine.objects.get(pk=line.pk).client_return
        self.assertIsNone(record.category)
        self.assertFalse(ClientSurveillance.objects.filter(client_issue=issue).exists())
        self.assertEqual(issue.client_returns.count(), 1)

    def test_a_return_on_a_closed_surveillance_is_late_and_changes_nothing(self):
        surveillance = ClientSurveillance.objects.get(client_issue=self.issue1)
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(status="CLOSED", result="ACCEPTED", closed_at=timezone.now())
        surveillance.refresh_from_db()
        frozen = (surveillance.status, surveillance.result, surveillance.due_at, surveillance.closed_at)
        draft = self.draft()
        self.line(draft, self.issue1, category="REJECTED", comment="too late but rejected")
        self.confirm(draft)
        surveillance.refresh_from_db()
        self.assertEqual((surveillance.status, surveillance.result, surveillance.due_at, surveillance.closed_at), frozen)
        self.assertEqual(surveillance.events.filter(event_type="RETURN_LATE").count(), 1)

    def test_documents_of_both_origins_and_a_supplier_document_leaves_the_supplier_circuit_untouched(self):
        supplier_revision = self.make_supplier_revision(category="FOR_REVIEW", client_category="For review")
        self.make_surveillance(supplier_revision, SupplierSurveillance.Status.CLOSED, SupplierSurveillance.Result.REVIEWED)
        supplier_issue = self.sent_issue(supplier_revision)
        before = list(SupplierSurveillance.objects.values_list("pk", "status", "result"))
        revisions = supplier_revision.document.revisions.count()
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED")  # internal origin
        self.line(draft, supplier_issue, category="ACCEPTED_WITH_COMMENTS", comment="minor")  # supplier origin
        self.confirm(draft)
        self.assertEqual(ClientSurveillanceReturn.objects.filter(in_transmittal=draft).count(), 2)
        self.assertEqual(list(SupplierSurveillance.objects.values_list("pk", "status", "result")), before)
        self.assertEqual(supplier_revision.document.revisions.count(), revisions)

    def test_a_mixed_in_with_a_supplier_document_and_a_client_return_works_for_a_company_that_is_both(self):
        draft = self.draft()
        add_receipt_line(draft, self.owner, reference="MIX-001", uploaded_file=SimpleUpload("mix.pdf", b"%PDF-1.4 mixed"), external_revision="A")
        self.line(draft, self.issue1, category="ACCEPTED")
        revisions = DocumentRevision.objects.count()
        confirmed = self.confirm(draft)
        self.assertEqual(confirmed.items.count(), 1)  # the supplier document: item + revision, as in GED-WF.3
        self.assertEqual(DocumentRevision.objects.count(), revisions + 1)
        self.assertEqual(ClientSurveillanceReturn.objects.filter(in_transmittal=draft).count(), 1)

    def test_empty_transmittals_are_still_refused(self):
        empty_in = self.draft()
        with self.assertRaises(ReceiptAnomalies) as caught:
            self.confirm(empty_in)
        self.assertIn("no_items", {a["code"] for a in caught.exception.anomalies})
        self.assertEqual(Transmittal.objects.get(pk=empty_in.pk).status, "DRAFT")
        out_empty = Transmittal.objects.create(project=self.project, direction="OUT", status="DRAFT", transmittal_number="OUT-EMPTY",
                                               external_company=self.client_company, created_by=self.owner)
        for allow in (False, True):  # the new switch never opens an empty OUT
            with self.assertRaises(ValidationError):
                finalize_transmittal(out_empty, self.owner, allow_empty_items=allow)
        with self.assertRaises(ValidationError):
            finalize_transmittal(self.draft(), self.owner)  # an empty IN without lines stays refused too

    def test_a_repeated_confirmation_is_refused_without_a_duplicate(self):
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED", uploads=[pdf("rep")])
        self.confirm(draft)
        returns, blobs, events = ClientSurveillanceReturn.objects.count(), EvidenceBlob.objects.count(), TransmittalEvent.objects.count()
        with self.assertRaises(ReceiptAnomalies) as caught:
            self.confirm(draft)
        self.assertEqual({a["code"] for a in caught.exception.anomalies}, {"already_received"})
        self.assertEqual((ClientSurveillanceReturn.objects.count(), EvidenceBlob.objects.count(), TransmittalEvent.objects.count()), (returns, blobs, events))
        self.assertEqual(draft.events.filter(event_type="RECEIVED").count(), 1)

    def test_client_issues_are_locked_in_ascending_order_before_anything_is_written(self):
        draft = self.draft()
        for issue in (self.issue3, self.issue1, self.issue2):  # added in a "wrong" order
            self.line(draft, issue, category="ACCEPTED")
        with CaptureQueriesContext(connection) as queries:
            self.confirm(draft)
        sql = [q["sql"] for q in queries.captured_queries]
        locks = [i for i, s in enumerate(sql) if "FOR UPDATE" in s and 'FROM "project_management_clientissue"' in s and "ORDER BY" in s]
        inserts = [i for i, s in enumerate(sql) if s.startswith('INSERT INTO "project_management_clientsurveillancereturn"')]
        self.assertTrue(locks and inserts)
        self.assertLess(locks[0], inserts[0])
        self.assertRegex(sql[locks[0]], r'"project_management_clientissue"\."id" ASC')

    def test_anomalies_block_the_whole_confirmation(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="ACCEPTED", uploads=[pdf("gone")])
        import os
        os.remove(line.files.get().file.path)  # the prepared file disappeared
        with self.assertRaises(ReceiptAnomalies) as caught:
            self.confirm(draft)
        self.assertIn("client_line_file_missing", {a["code"] for a in caught.exception.anomalies})
        self.assertEqual((Transmittal.objects.get(pk=draft.pk).status, ClientSurveillanceReturn.objects.count()), ("DRAFT", 0))
        unsent = self.issue([(self.review(), "FOR_REVIEW")])  # issued, never sent: the line carries its written justification
        issue = unsent.items.get().client_issue
        draft2 = self.draft()
        self.line(draft2, issue, category="ACCEPTED", no_send_justification="handed over by hand")
        self.send(unsent)  # a real send is recorded afterwards: the justification no longer holds, the user must choose
        with self.assertRaises(ReceiptAnomalies) as caught:
            self.confirm(draft2)
        self.assertIn("client_line_send_required", {a["code"] for a in caught.exception.anomalies})

    def test_a_storage_failure_on_the_third_line_rolls_everything_back_keeps_the_pending_files_and_leaves_no_orphan(self):
        draft = self.draft()
        lines = [self.line(draft, issue, category="ACCEPTED", uploads=[pdf(f"f{index}")]) for index, issue in enumerate((self.issue1, self.issue2, self.issue3))]
        pending = [row.file.name for row in TransmittalClientReturnLineFile.objects.filter(line__transmittal=draft).order_by("pk")]
        written, original, calls = [], FileSystemStorage.save, []

        def flaky(storage, name, content, max_length=None):
            calls.append(name)
            if len(calls) == 3:
                raise OSError("disk full")
            result = original(storage, name, content, max_length)
            written.append((storage, result))
            return result

        with patch.object(FileSystemStorage, "save", flaky), self.captureOnCommitCallbacks(execute=True), self.assertRaises(OSError):
            self.confirm(draft)
        self.assertEqual(Transmittal.objects.get(pk=draft.pk).status, "DRAFT")
        self.assertEqual((ClientSurveillanceReturn.objects.count(), EvidenceBlob.objects.count()), (0, self.blobs0))
        self.assertFalse(draft.events.filter(event_type="RECEIVED").exists())
        for storage, name in written:  # the files created for the lines already processed are removed again
            self.assertEqual(storage.exists(name), EvidenceBlob.objects.filter(file=name).exists())
        self.assertEqual(len(written), 2)
        for name in pending:  # the pending files stay, still referenced, for the retry
            self.assertTrue(default_storage.exists(name))
        self.assertTrue(all(row.blob_id is None and row.file for row in TransmittalClientReturnLineFile.objects.filter(line__transmittal=draft)))
        self.assertTrue(all(line.__class__.objects.get(pk=line.pk).client_return_id is None for line in lines))
        # the retry works
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.confirm(draft).status, "ISSUED")


class HybridQualificationTests(ClientInBase):
    def test_reception_only_confirms_and_leaves_the_lines_to_qualify_then_a_manager_qualifies_once(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="COMMENTED", comment="see files", uploads=[pdf("hq1"), png("hq2")])
        events = ClientSurveillanceEvent.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            confirmed = self.confirm(draft, self.receiver)
        self.assertEqual(confirmed.status, "ISSUED")
        line.refresh_from_db()
        self.assertEqual((line_state(line), ClientSurveillanceReturn.objects.count(), ClientSurveillanceEvent.objects.count()), ("TO_QUALIFY", 0, events))
        self.assertEqual(line.files.filter(blob__isnull=False).count(), 2)  # content and files are kept
        record = qualify_client_return_line(line.pk, self.qualifier)
        line.refresh_from_db()
        self.assertEqual((line_state(line), line.client_return_id, record.category, record.in_transmittal_id), ("QUALIFIED", record.pk, "COMMENTED", draft.pk))
        self.assertEqual(record.files.count(), 2)
        self.assertEqual({f.blob_id for f in record.files.all()}, {f.blob_id for f in line.files.all()})  # no second copy
        self.assertEqual(ClientSurveillanceEvent.objects.filter(event_type="RETURN_RECORDED", data__in_line=line.pk, data__qualified_later=True).count(), 1)
        with self.assertRaises(ValidationError):
            qualify_client_return_line(line.pk, self.qualifier)  # never two initial returns
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)

    def test_the_qualifier_can_complete_a_missing_result_and_the_line_itself_is_never_edited(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category=None, comment="client sent remarks, result to be read")
        self.confirm(draft, self.receiver)  # reception only: no result required from this user
        with self.assertRaises(ValidationError):
            qualify_client_return_line(line.pk, self.qualifier)  # a FOR_REVIEW distribution needs its result
        record = qualify_client_return_line(line.pk, self.qualifier, category="ACCEPTED_WITH_COMMENTS")
        self.assertEqual(record.category, "ACCEPTED_WITH_COMMENTS")
        line.refresh_from_db()
        self.assertEqual(line.category, None)  # the received fact is kept as it was received
        for action in (lambda: setattr(line, "comment", "x") or line.save(), line.delete):
            with self.assertRaises(ValidationError):
                action()

    def test_a_user_with_both_rights_qualifies_in_the_confirmation_but_one_without_a_result_is_blocked(self):
        draft = self.draft()
        self.line(draft, self.issue1, category=None, comment="remarks")
        with self.assertRaises(ReceiptAnomalies) as caught:
            self.confirm(draft, self.controller)  # this user will create the return now: the result is required
        self.assertIn("client_line_category_required", {a["code"] for a in caught.exception.anomalies})
        self.assertEqual(self.confirm(draft, self.receiver).status, "ISSUED")  # reception only is allowed

    def test_late_qualification_on_a_closed_surveillance_is_informative(self):
        draft = self.draft()
        line = self.line(draft, self.issue2, category="REJECTED", comment="rejected")
        self.confirm(draft, self.receiver)
        surveillance = ClientSurveillance.objects.get(client_issue=self.issue2)
        ClientSurveillance.objects.filter(pk=surveillance.pk).update(status="CLOSED", result="ACCEPTED", closed_at=timezone.now())
        qualify_client_return_line(line.pk, self.qualifier)
        surveillance.refresh_from_db()
        self.assertEqual((surveillance.status, surveillance.result), ("CLOSED", "ACCEPTED"))
        self.assertEqual(surveillance.events.filter(event_type="RETURN_LATE").count(), 1)

    def test_qualification_is_refused_before_confirmation_and_for_a_withdrawn_document(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="ACCEPTED")
        with self.assertRaises(ValidationError):
            qualify_client_return_line(line.pk, self.qualifier)  # the IN is still a draft
        self.confirm(draft, self.receiver)
        self.rev1.document.withdrawn_at = timezone.now()
        self.rev1.document.save(update_fields=["withdrawn_at"])
        with self.assertRaises(ValidationError):
            qualify_client_return_line(line.pk, self.qualifier)


class PermissionTests(ClientInBase):
    def test_adding_a_line_needs_transmittal_update_and_access_to_the_document(self):
        draft = self.draft()
        self.assertEqual(self.post_line(self.receiver, draft, self.issue1, files=[pdf("p1")]).status_code, 201)
        self.assertEqual(self.post_line(self.controller, draft, self.issue2).status_code, 201)
        self.assertEqual(self.post_line(self.qualifier, draft, self.issue3).status_code, 403)  # surveillance right alone: no
        self.assertEqual(self.post_line(self.reader, draft, self.issue3).status_code, 403)
        self.assertEqual(self.post_line(self.blind, draft, self.issue3).status_code, 403)  # update, but cannot read the document
        outsider = self.grant("in.outsider@links-local.invalid", self.other_tenant_project, [VIEW, UPDATE], company=self.other_company)
        self.assertIn(self.post_line(outsider, draft, self.issue3).status_code, (403, 404))
        elsewhere = self.grant("in.elsewhere@links-local.invalid", self.other_project, [VIEW, UPDATE, MANAGE])
        self.assertIn(self.post_line(elsewhere, draft, self.issue3).status_code, (403, 404))
        self.assertEqual(self.post_line(self.full, draft, self.issue3).status_code, 201)
        self.assertEqual(draft.client_return_lines.count(), 3)

    def test_confirming_needs_transmittal_update_and_the_surveillance_right_alone_never_confirms(self):
        draft = self.draft()
        self.line(draft, self.issue1, category="ACCEPTED")
        call = lambda user: self.call(TransmittalConfirmReceiptView, {"post": "create"}, user, "post", {}, "json", transmittal_id=draft.pk)
        self.assertEqual(call(self.qualifier).status_code, 403)
        self.assertEqual(call(self.reader).status_code, 403)
        self.assertEqual(call(self.receiver).status_code, 200)
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 0)  # no surveillance right: reception only

    def test_only_the_surveillance_right_qualifies_and_the_listing_follows_the_user_rights(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="COMMENTED", comment="x", uploads=[pdf("lst")])
        self.confirm(draft, self.receiver)
        qualify = lambda user: self.call(TransmittalClientReturnQualifyView, {"post": "create"}, user, "post", {}, "json", transmittal_id=draft.pk, line_id=line.pk)
        for user in (self.receiver, self.reader):
            self.assertEqual(qualify(user).status_code, 403, user.email)
        listing = lambda user: self.call(TransmittalClientReturnLineViewSet, {"get": "list"}, user, transmittal_id=draft.pk)
        row = listing(self.reader).data
        self.assertEqual((row["to_qualify"], len(row["results"]), row["results"][0]["state"], row["results"][0]["can_qualify"]), (1, 1, "TO_QUALIFY", False))
        self.assertTrue(listing(self.qualifier).data["results"][0]["can_qualify"])
        self.assertEqual(listing(self.blind).status_code, 403)  # no transmittal_view
        response = qualify(self.qualifier)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["line"]["state"], "QUALIFIED")
        self.assertEqual(qualify(self.qualifier).status_code, 400)  # a second qualification never creates a second return
        self.assertEqual(ClientSurveillanceReturn.objects.count(), 1)
        self.assertEqual(listing(self.full).data["results"][0]["client_return"]["code"], "C")

    def test_files_download_only_through_the_authenticated_route_with_transmittal_view_and_document_access(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="ACCEPTED", uploads=[pdf("dl")])
        row = line.files.get()
        download = lambda user, **kw: self.call(TransmittalClientReturnFileView, {"get": "retrieve"}, user, transmittal_id=kw.get("transmittal_id", draft.pk), line_id=line.pk, file_id=row.pk)
        response = download(self.reader)  # still a pending file
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(b"".join(response.streaming_content).startswith(b"%PDF"))
        self.assertEqual(download(self.blind).status_code, 403)  # no transmittal_view
        self.confirm(draft, self.controller)
        self.assertEqual(download(self.reader).status_code, 200)  # and once kept as a blob
        listed = self.call(TransmittalClientReturnLineViewSet, {"get": "list"}, self.reader, transmittal_id=draft.pk).data["results"][0]["files"][0]
        self.assertTrue(listed["kept"])
        self.assertNotIn(PENDING_CLIENT_RETURN_PREFIX, str(listed))  # the storage path is never exposed
        outsider = self.grant("in.outsider2@links-local.invalid", self.other_tenant_project, [VIEW, TVIEW], company=self.other_company)
        self.assertIn(download(outsider).status_code, (403, 404))

    def test_matching_proposes_candidates_of_this_company_and_project_and_never_chooses(self):
        draft = self.draft()
        other_company_issue = self.sent_issue(self.review(), "FOR_REVIEW")  # same client: also a candidate
        get = lambda user, **params: self.call(TransmittalClientReturnMatchView, {"get": "list"}, user, data=params, transmittal_id=draft.pk)
        everything = get(self.receiver).data["results"]
        self.assertGreaterEqual(len(everything), 4)
        reference = self.rev1.document.full_document_number or self.rev1.document.identification_number or self.rev1.document.file_name
        found = get(self.receiver, reference=reference).data["results"]
        self.assertIn(self.issue1.pk, [c["client_issue_id"] for c in found])
        send = found[0]["sends"][0]
        by_out = get(self.receiver, out_reference=send["transmittal_reference"]).data["results"]
        self.assertIn(found[0]["client_issue_id"], [c["client_issue_id"] for c in by_out])
        self.assertEqual(get(self.receiver, reference="no-such-reference").data["results"], [])
        self.assertTrue(other_company_issue)
        for candidate in everything:
            self.assertNotIn("selected", candidate)  # a proposal only
        wrong = self.draft(company=self.supplier_company)
        self.assertEqual(self.call(TransmittalClientReturnMatchView, {"get": "list"}, self.receiver, transmittal_id=wrong.pk).data["results"], [])
        self.assertEqual(self.call(TransmittalClientReturnMatchView, {"get": "list"}, self.blind, transmittal_id=draft.pk).data["results"], [])  # cannot read the documents


class CleanupProtectionTests(ClientInBase):
    def test_the_cleanup_command_never_touches_the_files_of_a_draft_however_old_and_still_lists_real_orphans(self):
        draft = self.draft()
        line = self.line(draft, self.issue1, category="ACCEPTED", uploads=[pdf("old")])
        referenced = line.files.get().file.name
        orphan = default_storage.save(f"{PENDING_CLIENT_RETURN_PREFIX}/project_0/transmittal_0/orphan/orphan.pdf", __import__("django").core.files.base.ContentFile(b"%PDF orphan"))
        try:
            out = StringIO()
            call_command("cleanup_receipt_pending_files", "--older-than-days", "0", stdout=out)
            text = out.getvalue()
            self.assertIn("WOULD DELETE " + orphan, text)  # a real orphan of the new prefix is recognised...
            self.assertNotIn(referenced, text)  # ...but a draft's referenced file is never a candidate
            deleted = []
            with patch.object(FileSystemStorage, "delete", lambda storage, name: deleted.append(name)):
                call_command("cleanup_receipt_pending_files", "--older-than-days", "0", "--apply", stdout=StringIO())
            self.assertNotIn(referenced, deleted)
            self.assertTrue(default_storage.exists(referenced))
        finally:
            default_storage.delete(orphan)
