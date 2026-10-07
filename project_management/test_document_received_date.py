# FINISH-C: Document.received_date -- the business date a CLIENT-origin ("Input Data")
# document was actually received, distinct from uploaded_at/DocumentRevision.created_at. See
# the model's own comment (models/document.py) and the migration (0223_document_received_date)
# for the full rationale. Reuses OriginBase/FilelessBase (test_document_origin.py's own
# fixtures) rather than inventing a second CLIENT-document test harness.
from datetime import date

from project_management.filters import DocumentFilter
from project_management.models import Document, DocumentOrigin
from project_management.serializers.document import DocumentSerializer
from project_management.test_document_origin import OriginBase

CLIENT = DocumentOrigin.CLIENT
INTERNAL = DocumentOrigin.INTERNAL
SUPPLIER = DocumentOrigin.SUPPLIER


class ReceivedDateSerializationTests(OriginBase):
    def test_received_date_is_accepted_and_serialized_on_create(self):
        document = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-03-15",
        )
        document.refresh_from_db()
        self.assertEqual(document.received_date, date(2024, 3, 15))
        data = DocumentSerializer(document, context={"user": self.creator}).data
        self.assertEqual(data["received_date"], "2024-03-15")

    def test_received_date_is_optional_and_defaults_to_null(self):
        # Section 11: no backend hard-requirement exists at this generic create path (many
        # pre-existing CLIENT-creation call sites rely on that) -- confirmed here explicitly,
        # not merely by the absence of a failure elsewhere.
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        document.refresh_from_db()
        self.assertIsNone(document.received_date)
        data = DocumentSerializer(document, context={"user": self.creator}).data
        self.assertIsNone(data["received_date"])

    def test_received_date_is_irrelevant_and_accepted_for_internal_documents_too(self):
        # received_date is a plain Document field (D13-style: origin-agnostic at the model/
        # serializer layer, like client_reference) -- never rejected for INTERNAL, even though
        # the UI never shows/sends it there.
        document = self.create(document_origin="INTERNAL", received_date="2024-01-01")
        document.refresh_from_db()
        self.assertEqual(document.received_date, date(2024, 1, 1))


class ReceivedDateHistoricalCompatibilityTests(OriginBase):
    def test_a_legacy_document_has_no_received_date_and_stays_valid(self):
        legacy = self.legacy_document()
        legacy.refresh_from_db()
        self.assertIsNone(legacy.received_date)
        data = DocumentSerializer(legacy, context={"user": self.creator}).data
        self.assertIsNone(data["received_date"])

    def test_an_existing_client_document_without_received_date_is_never_backfilled(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        document.refresh_from_db()
        self.assertIsNone(document.received_date)
        # A later, unrelated partial update must not silently assign one either.
        updated = self.update(document, file_name="Renamed plan")
        updated.refresh_from_db()
        self.assertIsNone(updated.received_date)


class ReceivedDatePersistenceTests(OriginBase):
    def test_received_date_can_be_updated_after_creation(self):
        document = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        updated = self.update(document, received_date="2024-06-01")
        updated.refresh_from_db()
        self.assertEqual(updated.received_date, date(2024, 6, 1))

    def test_received_date_can_be_cleared_back_to_null(self):
        document = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-06-01",
        )
        updated = self.update(document, received_date=None)
        updated.refresh_from_db()
        self.assertIsNone(updated.received_date)


class ReceivedDateOrderingTests(OriginBase):
    def test_documents_can_be_ordered_by_received_date(self):
        # FINISH-C section 22: received_date joined the view's ordering_fields whitelist --
        # confirmed directly against the model/queryset here, not merely by absence of a
        # DRF "invalid ordering field" error on the view (out of this test's fixture scope).
        early = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-01-01",
        )
        late = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-12-31",
        )
        ids = list(
            Document.objects.filter(id__in=[early.id, late.id])
            .order_by("received_date")
            .values_list("id", flat=True)
        )
        self.assertEqual(ids, [early.id, late.id])


class FirstRevisionExternalRevisionRegressionTests(OriginBase):
    def test_the_first_revision_external_revision_flow_is_unaffected_by_received_date(self):
        # Section 29 item 6: received_date must not interfere with the pre-existing first-
        # DocumentRevision/external_revision creation flow (FINISH-C0's core reused mechanism).
        document = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-05-01",
        )
        data = self.upload_first_file(document, external_revision="A")
        self.assertEqual(data["version_number"], 1)
        self.assertEqual(data["external_revision"], "A")
        document.refresh_from_db()
        self.assertEqual(document.received_date, date(2024, 5, 1))
        self.assertEqual(document.document_origin, CLIENT)


class LivrablesClientExclusionTests(OriginBase):
    """FINISH-C.1 Correction A: a true CLIENT-origin Document must never appear in the
    Livrables fixed-scope result (document_origin=INTERNAL,SUPPLIER). Diagnosis confirmed the
    existing DocumentFilter already enforces this correctly -- this pins it as a permanent
    regression test, not merely a one-off manual check."""

    def test_a_client_document_never_appears_in_the_livrables_fixed_scope(self):
        client_doc = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        internal_doc = self.create(document_origin="INTERNAL")
        supplier_doc = self.create(
            document_origin="SUPPLIER", external_issuer_company=self.supplier_company.id
        )
        livrables_scope = DocumentFilter(
            {"document_origin": "INTERNAL,SUPPLIER"},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(livrables_scope.values_list("id", flat=True))
        self.assertNotIn(client_doc.id, ids)
        self.assertIn(internal_doc.id, ids)
        self.assertIn(supplier_doc.id, ids)
        self.assertEqual({d.document_origin for d in livrables_scope}, {INTERNAL, SUPPLIER})

    def test_input_data_fixed_scope_returns_only_client(self):
        client_doc = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        self.create(document_origin="INTERNAL")
        input_data_scope = DocumentFilter(
            {"document_origin": "CLIENT"},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(input_data_scope.values_list("id", flat=True))
        self.assertEqual(ids, {client_doc.id})


class ClientFilterTests(OriginBase):
    """FINISH-C.1 Correction C: external_issuer_company as a server-side filter (the Input
    Data "Client" filter) -- by ContactCompany id, reusing the existing FK, no new filtering
    infrastructure."""

    def test_filtering_by_external_issuer_company_returns_only_that_issuer_s_documents(self):
        other_client_company = self.network_company("Other Client Co")
        doc_a = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        doc_b = self.create(
            document_origin="CLIENT", external_issuer_company=other_client_company.id
        )
        result = DocumentFilter(
            {"external_issuer_company": str(self.client_company.id)},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(result.values_list("id", flat=True))
        self.assertEqual(ids, {doc_a.id})
        self.assertNotIn(doc_b.id, ids)

    def test_external_issuer_company_accepts_a_comma_separated_list_like_author_approver(self):
        other_client_company = self.network_company("Other Client Co")
        doc_a = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        doc_b = self.create(
            document_origin="CLIENT", external_issuer_company=other_client_company.id
        )
        result = DocumentFilter(
            {
                "external_issuer_company": f"{self.client_company.id},{other_client_company.id}"
            },
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(result.values_list("id", flat=True))
        self.assertEqual(ids, {doc_a.id, doc_b.id})


class ReceivedDateRangeFilterTests(OriginBase):
    """FINISH-C.1 Correction E: received_date_from/received_date_to -- dedicated Input Data
    date filtering, never repurposing date_from/date_to (which stay on uploaded_at)."""

    def test_received_date_from_and_to_filter_inclusively(self):
        early = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-01-01",
        )
        mid = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-06-15",
        )
        late = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-12-31",
        )
        result = DocumentFilter(
            {"received_date_from": "2024-01-01", "received_date_to": "2024-06-15"},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(result.values_list("id", flat=True))
        self.assertEqual(ids, {early.id, mid.id})
        self.assertNotIn(late.id, ids)

    def test_a_single_exact_date_is_expressed_as_from_equal_to_to(self):
        target = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-06-15",
        )
        other = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-06-16",
        )
        result = DocumentFilter(
            {"received_date_from": "2024-06-15", "received_date_to": "2024-06-15"},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(result.values_list("id", flat=True))
        self.assertEqual(ids, {target.id})
        self.assertNotIn(other.id, ids)

    def test_a_null_received_date_is_excluded_by_either_bound(self):
        undated = self.create(
            document_origin="CLIENT", external_issuer_company=self.client_company.id
        )
        dated = self.create(
            document_origin="CLIENT",
            external_issuer_company=self.client_company.id,
            received_date="2024-06-15",
        )
        result = DocumentFilter(
            {"received_date_from": "2024-01-01"},
            queryset=Document.objects.filter(folder=self.folder),
        ).qs
        ids = set(result.values_list("id", flat=True))
        self.assertIn(dated.id, ids)
        self.assertNotIn(undated.id, ids)

    def test_date_from_to_still_targets_uploaded_at_unchanged(self):
        # Correction E's own explicit constraint: the pre-existing date_from/date_to filters
        # must keep their uploaded_at meaning, never be repurposed for received_date.
        import project_management.filters as filters_module

        assert filters_module.DocumentFilter.base_filters["date_from"].field_name == "uploaded_at"
        assert filters_module.DocumentFilter.base_filters["date_to"].field_name == "uploaded_at"
        assert (
            filters_module.DocumentFilter.base_filters["received_date_from"].field_name
            == "received_date"
        )
        assert (
            filters_module.DocumentFilter.base_filters["received_date_to"].field_name
            == "received_date"
        )
