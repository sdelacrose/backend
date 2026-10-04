"""L3B: Supplier Surveillance foundation -- targeted tests only (root model, reference,
on-demand creation service, minimal API). No reviewers/events/template/PDF/closure: those are
later lots, deliberately not covered here.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import DocumentRevision, SupplierSurveillance
from project_management.services.supplier_surveillance import (
    create_supplier_surveillance,
    generate_supplier_surveillance_reference,
    get_supplier_surveillance_for_revision,
)
from project_management.test_document_origin import CLIENT, INTERNAL, SUPPLIER, OriginBase
from project_management.views.supplier_surveillance import SupplierSurveillanceViewSet


class SupplierSurveillanceBase(OriginBase):
    def make_supplier_revision(self, category="FOR_REVIEW", external_revision="A", **extra):
        document = self.create(
            document_origin=SUPPLIER,
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category=category,
            identification_number="VEN-4578-DWG-002",
            **extra,
        )
        self.upload_first_file(document, external_revision=external_revision)
        return DocumentRevision.objects.get(document=document)

    def make_internal_revision(self):
        document = self.create(document_origin=INTERNAL)
        self.upload_first_file(document)
        return DocumentRevision.objects.get(document=document)

    def make_client_revision(self):
        document = self.create(
            document_origin=CLIENT, external_issuer_company=self.client_company.id,
        )
        self.upload_first_file(document)
        return DocumentRevision.objects.get(document=document)


class CreateSupplierSurveillanceTests(SupplierSurveillanceBase):
    # 1/2/3/5/14/15/16/17/18 -- success path and the initial, deliberately-empty schema.
    def test_create_for_supplier_for_review_revision_succeeds(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator
        )
        self.assertIsNotNone(surveillance.pk)

    def test_belongs_to_the_exact_revision(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator
        )
        self.assertEqual(surveillance.document_revision_id, revision.id)

    def test_project_is_derived_from_the_revision_not_accepted_as_input(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator
        )
        self.assertEqual(surveillance.project_id, revision.document.folder.project_id)

    def test_supplier_is_derived_from_the_document_not_duplicated(self):
        revision = self.make_supplier_revision()
        create_supplier_surveillance(document_revision=revision, actor=self.creator)
        # No supplier/issuer field exists on the model itself (L3A/L3B section 3): it is only
        # ever read via document_revision.document.external_issuer_company.
        field_names = {f.name for f in SupplierSurveillance._meta.get_fields()}
        self.assertNotIn("external_issuer_company", field_names)
        self.assertNotIn("supplier", field_names)
        self.assertEqual(
            revision.document.external_issuer_company_id, self.supplier_company.id
        )

    def test_reference_is_generated_and_unique(self):
        revision = self.make_supplier_revision()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator
        )
        self.assertTrue(surveillance.reference)
        self.assertTrue(surveillance.reference.startswith("SR-"))

    def test_reference_counter_increments_across_surveillances(self):
        first = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        second = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertNotEqual(first.reference, second.reference)

    def test_duplicate_surveillance_for_the_same_revision_is_rejected(self):
        revision = self.make_supplier_revision()
        create_supplier_surveillance(document_revision=revision, actor=self.creator)
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(document_revision=revision, actor=self.creator)
        self.assertEqual(
            SupplierSurveillance.objects.filter(document_revision=revision).count(), 1
        )

    def test_internal_revision_is_rejected(self):
        revision = self.make_internal_revision()
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(document_revision=revision, actor=self.creator)

    def test_client_revision_is_rejected(self):
        revision = self.make_client_revision()
        with self.assertRaises(ValidationError):
            create_supplier_surveillance(document_revision=revision, actor=self.creator)

    def test_for_information_backend_creation_remains_possible(self):
        # L3B section 10: FOR_INFORMATION must not be structurally blocked server-side -- only
        # the frontend's normal entry point is FOR_REVIEW-only.
        revision = self.make_supplier_revision(category="FOR_INFORMATION")
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator
        )
        self.assertIsNotNone(surveillance.pk)

    def test_initial_status_is_draft(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertEqual(surveillance.status, SupplierSurveillance.Status.DRAFT)

    def test_result_and_new_revision_required_start_null(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertIsNone(surveillance.result)
        self.assertIsNone(surveillance.new_revision_required)

    def test_template_snapshot_starts_empty(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertIsNone(surveillance.template_snapshot)

    def test_created_by_is_the_acting_user(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertEqual(surveillance.created_by_id, self.creator.id)

    def test_closed_at_stays_null(self):
        surveillance = create_supplier_surveillance(
            document_revision=self.make_supplier_revision(), actor=self.creator
        )
        self.assertIsNone(surveillance.closed_at)


class SupplierSurveillanceReferenceTests(SupplierSurveillanceBase):
    # 6/7/21 -- reference allocation in isolation.
    def test_reference_matches_the_transmittal_style_project_scoped_format(self):
        revision = self.make_supplier_revision()
        reference = generate_supplier_surveillance_reference(project=revision.document.folder.project)
        self.assertRegex(reference, r"^SR-[A-Z0-9]+-\d{5}$")

    def test_sequence_is_isolated_per_project(self):
        revision_a = self.make_supplier_revision()
        project_a = revision_a.document.folder.project
        other_project, _other_folder = self.make_project("Second Project")
        first = generate_supplier_surveillance_reference(project=project_a)
        second = generate_supplier_surveillance_reference(project=other_project)
        # Both start their own counter at 00001 -- independent per-project sequences.
        self.assertTrue(first.endswith("00001"))
        self.assertTrue(second.endswith("00001"))

    def test_concurrency_pattern_locks_the_counter_row(self):
        # Not an actual multi-thread test (expensive integration machinery, per L3B section
        # 32/21): confirms the allocator goes through select_for_update inside a real atomic
        # transaction by construction, mirroring generate_transmittal_number exactly.
        import inspect

        import project_management.services.supplier_surveillance as module

        source = inspect.getsource(module)
        self.assertIn(
            "@transaction.atomic\ndef generate_supplier_surveillance_reference",
            source,
        )
        self.assertIn(
            "SupplierSurveillanceReferenceCounter.objects.select_for_update()",
            source,
        )


ACCESS = "project_management.views.supplier_surveillance.get_register_document_or_404"
CAN_UPDATE = "project_management.views.supplier_surveillance.assert_can_update_document"


class GetSupplierSurveillanceApiTests(SupplierSurveillanceBase):
    # 19/20 -- the minimal read endpoint.
    def _request(self, document, revision_id, method="get", data=None):
        factory = APIRequestFactory()
        request = (
            factory.post("/x/", data or {}, format="multipart")
            if method == "post"
            else factory.get("/x/")
        )
        force_authenticate(request, user=self.creator)
        # Same bypasses test_document_fileless.FilelessBase.revisions_api already uses: the
        # module-license permission class and the real accessible-Documents queryset are not the
        # behaviour under test here (document access control is covered by test_document_origin/
        # test_document_register already) -- only this view's own create/retrieve logic is.
        view = SupplierSurveillanceViewSet.as_view(
            {"get": "retrieve", "post": "create"} if method == "post" else {"get": "retrieve"},
            permission_classes=[],
        )
        with patch(ACCESS, return_value=document), patch(CAN_UPDATE):
            return view(request, document_id=document.id, revision_id=revision_id)

    def test_get_returns_null_before_creation_and_the_row_after(self):
        revision = self.make_supplier_revision()
        before = self._request(revision.document, revision.id)
        self.assertIsNone(before.data["supplier_surveillance"])
        create_supplier_surveillance(document_revision=revision, actor=self.creator)
        after = self._request(revision.document, revision.id)
        self.assertIsNotNone(after.data["supplier_surveillance"])
        self.assertTrue(after.data["supplier_surveillance"]["reference"].startswith("SR-"))

    def test_create_via_the_real_view_returns_201_with_the_reference(self):
        revision = self.make_supplier_revision()
        response = self._request(revision.document, revision.id, method="post")
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.data["reference"].startswith("SR-"))
        self.assertEqual(response.data["status"], "DRAFT")

    def test_create_via_the_real_view_rejects_a_second_one_with_400(self):
        revision = self.make_supplier_revision()
        self._request(revision.document, revision.id, method="post")
        second = self._request(revision.document, revision.id, method="post")
        self.assertEqual(second.status_code, 400)

    def test_wrong_revision_id_for_the_document_404s(self):
        revision = self.make_supplier_revision()
        # DRF's dispatch() catches Http404 and returns a 404 Response (DocumentWorkflowViewSet's
        # own _revision helper relies on the exact same behaviour) -- never a raised exception
        # at this level.
        response = self._request(revision.document, revision.id + 9999)
        self.assertEqual(response.status_code, 404)


class NoAutomaticCreationTests(SupplierSurveillanceBase):
    # 12/13 -- creating the Document/Revision alone must never create a Surveillance.
    def test_no_automatic_surveillance_on_supplier_document_creation(self):
        self.create(
            document_origin=SUPPLIER,
            external_issuer_company=self.supplier_company.id,
            supplier_transmittal_category="FOR_REVIEW",
        )
        self.assertEqual(SupplierSurveillance.objects.count(), 0)

    def test_no_automatic_surveillance_on_supplier_revision_creation(self):
        self.make_supplier_revision()
        self.assertEqual(SupplierSurveillance.objects.count(), 0)
