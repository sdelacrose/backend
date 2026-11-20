"""GED-WF.3: manual confirmation of an incoming Transmittal (transmittal_update), document matching
and anti-duplicate rules. Real grants (ProjectTeamMembers + project role) are used so the permission
matrix is proven through the actual TransmittalPermission, not a stub."""
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import Permission, RolePermission
from project_management import test_document_origin as origin_tests
from project_management.models import Document, DocumentOrigin, Transmittal, TransmittalEvent
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_revision import add_document_revision
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_receipt import (
    ReceiptAnomalies, confirm_incoming_receipt, find_reception_matches, normalize_reference,
    receipt_anomalies,
)
from project_management.test_transmittal import (
    TransmittalTestBase, grant_transmittal_project_permissions, make_user,
)
from project_management.views.transmittal import (
    TransmittalConfirmReceiptView, TransmittalDocumentMatchView, TransmittalIssueView,
    TransmittalReceiptCheckView,
)


class ReceiptBase(TransmittalTestBase):
    def supplier_doc(self, reference, company=None, revision_label=None, content=b"s1"):
        document = self.make_document(origin=DocumentOrigin.SUPPLIER, name=f"{reference}.txt")
        document.identification_number = reference
        document.external_issuer_company = company or self.supplier_company
        document.save(update_fields=["identification_number", "external_issuer_company"])
        revision = add_document_revision(
            document, SimpleUploadedFile("r.txt", content), self.owner, external_revision=revision_label,
        )
        return document, revision

    def in_draft(self, number="SUP-TR-2026-0001", *revisions):
        transmittal = self.create_in_transmittal(transmittal_number=number)
        for revision in revisions:
            add_transmittal_item(transmittal, revision, None, self.owner)
        return transmittal

    def post(self, view_class, transmittal_id, user):
        request = APIRequestFactory().post("/x/", {}, format="json")
        force_authenticate(request, user=user)
        return view_class.as_view({"post": "create"}, permission_classes=[])(request, transmittal_id=transmittal_id)

    def get(self, view_class, transmittal_id, user, **params):
        request = APIRequestFactory().get("/x/", params)
        force_authenticate(request, user=user)
        return view_class.as_view({"get": "list"}, permission_classes=[])(request, transmittal_id=transmittal_id)

    def user_with(self, email, project, actions, company=None):
        user = make_user(email)
        user.company = company or self.company
        user.save(update_fields=["company"])
        if "full_access" in actions:
            role = grant_transmittal_project_permissions(user, project, user.company, ())
            perm, _ = Permission.objects.get_or_create(
                key="documents.full_access",
                defaults={"name": "full", "module": "documents", "description": "test"},
            )
            RolePermission.objects.get_or_create(role=role, permission=perm)
        else:
            grant_transmittal_project_permissions(user, project, user.company, actions)
        return user

    def received_events(self, transmittal):
        return TransmittalEvent.objects.filter(transmittal=transmittal, event_type="RECEIVED").count()


class ConfirmReceiptTests(ReceiptBase):
    # 1
    def test_incoming_draft_stays_draft_until_confirmation(self):
        transmittal = self.in_draft("D-1", self.supplier_doc("DWG-001")[1])
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")
        self.assertEqual(self.received_events(transmittal), 0)

    # 2 + 14
    def test_authorized_confirmation_records_event_user_date_and_freezes_evidence(self):
        user = self.user_with("wf3.updater@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("D-2", self.supplier_doc("DWG-002", revision_label="A")[1])
        response = self.post(TransmittalConfirmReceiptView, transmittal.pk, user)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        stored = Transmittal.objects.get(pk=transmittal.pk)
        self.assertEqual(stored.status, "ISSUED")
        self.assertIsNotNone(stored.received_at)
        self.assertIsNone(stored.issued_at)
        event = stored.events.get(event_type="RECEIVED")
        self.assertEqual(event.actor_id, user.pk)
        item = stored.items.get()
        self.assertEqual(item.evidence_snapshot["revision"]["external_revision"], "A")
        self.assertIsNotNone(stored.evidence_snapshot)
        # immutable afterwards
        with self.assertRaises(DjangoValidationError):
            add_transmittal_item(stored, self.supplier_doc("DWG-002B")[1], None, self.owner)
        with self.assertRaises(DjangoValidationError):
            item.delete()

    # 3 + 8-permissions matrix
    def test_permission_matrix(self):
        def fresh():
            return self.in_draft(f"P-{Transmittal.objects.count()}", self.supplier_doc(f"REF-{Transmittal.objects.count()}")[1])

        cases = {
            "issue only": self.user_with("wf3.issue@links-local.invalid", self.project, ("issue",)),
            "view only": self.user_with("wf3.view@links-local.invalid", self.project, ("view",)),
            "create only": self.user_with("wf3.create@links-local.invalid", self.project, ("create",)),
            "update in another project": self.user_with("wf3.other@links-local.invalid", self.other_project, ("update",)),
        }
        for label, user in cases.items():
            with self.subTest(label):
                transmittal = fresh()
                response = self.post(TransmittalConfirmReceiptView, transmittal.pk, user)
                # GED-SEC.2 (SEC-06): outside the caller's project -> 404, inside it with a missing right -> 403
                self.assertEqual(response.status_code, 404 if label == "update in another project" else 403)
                self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")
                self.assertEqual(self.received_events(transmittal), 0)
        stranger = make_user("wf3.stranger@links-local.invalid")
        stranger.company = self.other_company
        stranger.save(update_fields=["company"])
        transmittal = fresh()
        self.assertEqual(self.post(TransmittalConfirmReceiptView, transmittal.pk, stranger).status_code, 404)  # GED-SEC.2 (SEC-06)
        for label, actions in (("update", ("update",)), ("full_access", ("full_access",))):
            with self.subTest(label):
                user = self.user_with(f"wf3.ok.{label}@links-local.invalid", self.project, actions)
                transmittal = fresh()
                response = self.post(TransmittalConfirmReceiptView, transmittal.pk, user)
                self.assertEqual(response.status_code, 200, getattr(response, "data", None))

    # transmittal_issue never confirms an IN, even through the issue endpoint
    def test_issue_endpoint_does_not_confirm_an_incoming_transmittal(self):
        user = self.user_with("wf3.issuer@links-local.invalid", self.project, ("issue", "update"))
        transmittal = self.in_draft("D-3", self.supplier_doc("DWG-003")[1])
        response = self.post(TransmittalIssueView, transmittal.pk, user)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")

    # 5
    def test_second_confirmation_is_refused_without_a_new_event(self):
        user = self.user_with("wf3.twice@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("D-4", self.supplier_doc("DWG-004")[1])
        self.assertEqual(self.post(TransmittalConfirmReceiptView, transmittal.pk, user).status_code, 200)
        before = TransmittalEvent.objects.filter(transmittal=transmittal).count()
        second = self.post(TransmittalConfirmReceiptView, transmittal.pk, user)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data["anomalies"][0]["code"], "already_received")
        self.assertEqual(TransmittalEvent.objects.filter(transmittal=transmittal).count(), before)
        self.assertEqual(self.received_events(transmittal), 1)

    def test_blocking_anomaly_writes_nothing(self):
        user = self.user_with("wf3.empty@links-local.invalid", self.project, ("update",))
        transmittal = self.in_draft("D-5")  # no item
        response = self.post(TransmittalConfirmReceiptView, transmittal.pk, user)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["anomalies"][0]["code"], "no_items")
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "DRAFT")
        check = self.get(TransmittalReceiptCheckView, transmittal.pk, user)
        self.assertFalse(check.data["can_confirm"])

    # 11
    def test_issuer_inconsistent_with_the_transmittal_company_blocks(self):
        other_doc, other_rev = self.supplier_doc("DWG-011", company=self.other_supplier_company)
        transmittal = self.in_draft("D-6", other_rev)
        codes = [a["code"] for a in receipt_anomalies(transmittal)]
        self.assertIn("issuer_mismatch", codes)
        with self.assertRaises(ReceiptAnomalies):
            confirm_incoming_receipt(transmittal, self.owner)
        self.assertEqual(self.received_events(transmittal), 0)

    # 12
    def test_same_reception_with_another_formatting_of_the_reference_is_a_duplicate(self):
        document, revision = self.supplier_doc("DWG-012")
        first = self.in_draft("SUP-TR-2026-0099", revision)
        confirm_incoming_receipt(first, self.owner)
        second = self.in_draft("sup tr 2026 0099", revision)
        codes = [a["code"] for a in receipt_anomalies(second)]
        self.assertIn("duplicate_reception", codes)
        # the exact same reference is already refused by the existing database constraint
        with self.assertRaises(DRFValidationError):
            self.create_in_transmittal_expect_error("SUP-TR-2026-0099")

    def create_in_transmittal_expect_error(self, number):
        from project_management.serializers.transmittal import TransmittalSerializer
        serializer = TransmittalSerializer(data={
            "direction": "IN", "status": "DRAFT", "project": self.project.id,
            "external_company": self.supplier_company.id, "transmittal_number": number,
        }, context={"user": self.owner})
        serializer.is_valid(raise_exception=True)
        serializer.save()

    # 13 + 6 + 7
    def test_legitimate_retransmission_and_new_revision_are_accepted(self):
        document, revision_a = self.supplier_doc("DWG-013", revision_label="A")
        first = self.in_draft("RT-1", revision_a)
        confirm_incoming_receipt(first, self.owner)
        # same document + same revision received again by another supplier transmittal: legitimate
        again = self.in_draft("RT-2", revision_a)
        self.assertEqual(receipt_anomalies(again), [])
        confirm_incoming_receipt(again, self.owner)
        # a new revision of the known document
        revision_b = add_document_revision(
            document, SimpleUploadedFile("b.txt", b"rev b"), self.owner, external_revision="B",
        )
        newer = self.in_draft("RT-3", revision_b)
        self.assertEqual(receipt_anomalies(newer), [])
        confirm_incoming_receipt(newer, self.owner)
        self.assertEqual(
            Transmittal.objects.filter(direction="IN", status="ISSUED", items__document_revision__document=document).distinct().count(), 3,
        )
        self.assertEqual(Document.objects.filter(identification_number="DWG-013").count(), 1)

    def test_two_documents_with_the_same_reference_in_one_transmittal_is_blocked(self):
        _d1, r1 = self.supplier_doc("DWG-014")
        # created behind the guard on purpose (legacy data): same reference, formatting differs
        _d2, r2 = self.supplier_doc("dwg 014")
        transmittal = self.in_draft("D-7", r1, r2)
        codes = [a["code"] for a in receipt_anomalies(transmittal)]
        self.assertIn("duplicate_canonical_document", codes)


class MatchingTests(ReceiptBase):
    # 6 / 10
    def test_matching_ignores_formatting_and_returns_every_candidate(self):
        d1, r1 = self.supplier_doc("DWG-100", revision_label="A")
        d2, _r2 = self.supplier_doc("dwg 100")
        self.supplier_doc("DWG-100", company=self.other_supplier_company)  # another supplier: never a candidate
        transmittal = self.create_in_transmittal(transmittal_number="M-1")
        matches = find_reception_matches(transmittal, "DWG_100", "a")
        self.assertEqual({m["document_id"] for m in matches}, {d1.pk, d2.pk})
        by_id = {m["document_id"]: m for m in matches}
        self.assertEqual(by_id[d1.pk]["match"], "formatting")
        self.assertEqual(by_id[d1.pk]["known_revision_id"], r1.pk)
        exact = find_reception_matches(transmittal, "DWG-100")
        self.assertEqual({m["match"] for m in exact if m["document_id"] == d1.pk}, {"exact"})
        self.assertEqual(find_reception_matches(transmittal, "UNKNOWN-1"), [])
        self.assertEqual(normalize_reference(" DWG-100 / a "), "dwg100a")

    def test_matching_endpoint_needs_transmittal_update(self):
        self.supplier_doc("DWG-101")
        transmittal = self.create_in_transmittal(transmittal_number="M-2")
        allowed = self.user_with("wf3.match@links-local.invalid", self.project, ("update",))
        denied = self.user_with("wf3.nomatch@links-local.invalid", self.project, ("view",))
        ok = self.get(TransmittalDocumentMatchView, transmittal.pk, allowed, reference="dwg101")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(len(ok.data["matches"]), 1)
        self.assertEqual(self.get(TransmittalDocumentMatchView, transmittal.pk, denied, reference="dwg101").status_code, 403)


class CanonicalDuplicateGuardTests(origin_tests.OriginBase):
    def folder_in_project(self):
        return self.folder

    def save_supplier(self, reference, **extra):
        serializer = self.build(self.payload(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id,
            identification_number=reference, **extra,
        ))
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            return serializer.save()

    # 8 / 9
    def test_unknown_reference_creates_and_a_formatting_variant_needs_an_explicit_confirmation(self):
        first = self.save_supplier("DWG-200")
        self.assertEqual(first.identification_number, "DWG-200")
        if not first.folder.project_id:
            self.skipTest("fixture folder is not a project folder")
        with self.assertRaises(DRFValidationError) as ctx:
            self.save_supplier("dwg 200")
        self.assertIn("identification_number", ctx.exception.detail)
        self.assertEqual(Document.objects.filter(identification_number__icontains="200").count(), 1)
        # explicit confirmation creates it
        confirmed = self.save_supplier("dwg 200", allow_duplicate_reference="true")
        self.assertEqual(Document.objects.filter(pk=confirmed.pk).count(), 1)

    def test_another_supplier_with_the_same_reference_is_not_a_duplicate(self):
        self.save_supplier("DWG-201")
        other = self.network_company("Cube Supplier")
        serializer = self.build(self.payload(
            document_origin="SUPPLIER", external_issuer_company=other.id, identification_number="DWG-201",
        ))
        with self.relaxed():
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()


class OutRegressionTests(ReceiptBase):
    # 15
    def test_out_issue_still_works_with_transmittal_issue(self):
        user = self.user_with("wf3.out@links-local.invalid", self.project, ("issue",))
        transmittal = self.create_out_transmittal()
        add_transmittal_item(transmittal, self.make_approved_internal_revision(), "FOR_REVIEW", self.owner)
        response = self.post(TransmittalIssueView, transmittal.pk, user)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        self.assertEqual(Transmittal.objects.get(pk=transmittal.pk).status, "ISSUED")
        self.assertEqual(TransmittalEvent.objects.filter(transmittal=transmittal, event_type="ISSUED").count(), 1)
