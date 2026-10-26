# L2A: two distinct transmittal-category axes.
#
# Client Transmittal Category ("how is this meant to reach the Client") REUSES the existing,
# already-tested Document.transmittal_category field/OUT-eligibility behavior unchanged -- see
# test_transmittal.py / services/transmittal_eligibility.py's own tests for that coverage. Here:
# only a safety-net confirming it is genuinely untouched by this lot.
#
# Supplier Transmittal Category ("why did the Supplier send us this document") is the new,
# additive, SUPPLIER-only field: Document.supplier_transmittal_category.
from unittest.mock import patch

from project_management.models import Document, Folder
from project_management.serializers.document import DocumentSerializer
from project_management.services.transmittal_eligibility import (
    OUT_ELIGIBLE_TRANSMITTAL_CATEGORIES,
)
from project_management.test_document_workflow import WorkflowTestBase
from project_management.test_transmittal import make_project


class ClientTransmittalCategoryUnchangedTests(WorkflowTestBase):
    """Safety net: transmittal_category / OUT eligibility semantics are untouched by L2A."""

    def test_out_eligible_categories_unchanged(self):
        self.assertEqual(
            OUT_ELIGIBLE_TRANSMITTAL_CATEGORIES, {"for review", "for information"}
        )

    def test_field_still_a_plain_nullable_charfield(self):
        field = Document._meta.get_field("transmittal_category")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertIsNone(getattr(field, "choices", None))


class SupplierTransmittalCategoryTests(WorkflowTestBase):
    """Document.supplier_transmittal_category: new, additive, SUPPLIER-only field."""

    def setUp(self):
        super().setUp()
        project = make_project(self.document.folder.company_profile, self.creator)
        self.project_folder = Folder.objects.create(
            project=project, name="L2A Supplier Category Folder"
        )

    def create_supplier_document(self, name="supplier-doc.txt", **extra):
        # Same pattern already established in test_document_reference.py's
        # OriginAwareReferenceTests: .create() called directly (bypassing .is_valid(), which
        # would also require satisfying unrelated issuer-network/document-type-code validation),
        # with get_config patched (no self.initial_data outside a real is_valid() pass).
        with patch.object(DocumentSerializer, "get_config", return_value=None):
            return DocumentSerializer(context={"user": self.creator}).create(
                {
                    "folder": self.project_folder,
                    "author": self.creator,
                    "file_name": name,
                    "document_origin": "SUPPLIER",
                    **extra,
                }
            )

    def test_accepts_for_information_and_for_review(self):
        for value in ("FOR_INFORMATION", "FOR_REVIEW"):
            document = self.create_supplier_document(
                name=f"{value}.txt", supplier_transmittal_category=value
            )
            document.refresh_from_db()
            self.assertEqual(document.supplier_transmittal_category, value)

    def test_is_independent_of_client_transmittal_category(self):
        document = self.create_supplier_document(
            supplier_transmittal_category="FOR_REVIEW",
            transmittal_category="For information",
        )
        document.refresh_from_db()
        self.assertEqual(document.supplier_transmittal_category, "FOR_REVIEW")
        self.assertEqual(document.transmittal_category, "For information")

    def test_serializer_rejects_an_invalid_value(self):
        serializer = DocumentSerializer(
            data={"supplier_transmittal_category": "NOT_A_REAL_VALUE"}, partial=True
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("supplier_transmittal_category", serializer.errors)

    def test_is_optional_and_nullable_blank_for_historical_documents(self):
        # No supplier_transmittal_category at all -- an existing/legacy document (or any
        # non-Supplier document) must stay valid and simply read back as None.
        document = self.create_supplier_document(name="no-category.txt")
        document.refresh_from_db()
        self.assertIsNone(document.supplier_transmittal_category)

    def test_exposed_on_the_serializer_independently_of_the_client_field(self):
        document = self.create_supplier_document(
            supplier_transmittal_category="FOR_INFORMATION"
        )
        data = DocumentSerializer(document, context={"include_control": True}).data
        self.assertEqual(data["supplier_transmittal_category"], "FOR_INFORMATION")
