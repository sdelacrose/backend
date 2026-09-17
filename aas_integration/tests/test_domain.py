import io
import hashlib
import json
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import parse_qs, urlparse
from decimal import Decimal
from unittest.mock import patch

import jwt
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from models.models import CompanyProfile, Permission, Role, RolePermission, User, UserRole
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    Document,
    Folder,
    PartsListConfig,
    Project,
    ProjectSolutionSpaceItem,
    SolutionSpaceConfig,
)
from project_management.serializers.matrix import (
    AvailableProductPartSerializer,
    BillOfMaterialPartSerializer,
    normalize_available_product_columns,
)
from project_management.signals import create_default_folders, notify_on_project_save

from aas_integration.adapters import (
    create_aasx,
    inspect_aasx,
    parse_aas_environment,
    read_aasx,
    read_aasx_package,
)
from aas_integration.domain import (
    ProductAASMapper,
    ProjectBOMAASMapper,
    SolutionSpaceAASMapper,
    canonical_json,
    source_hash,
)
from aas_integration.models import (
    AASFieldProvenance,
    AASOutboxEvent,
    AASPublication,
    AASResourceIdentity,
    AASSubmodelFieldMapping,
    AASSubmodelTemplate,
    AASSubmodelTemplateSupplementaryFile,
    ExternalAASClient,
    ExternalAASGrant,
    ProductAASSubmodelAssignment,
    ProductAASProfile,
)
from aas_integration.official_templates import OfficialTemplate
from aas_integration.services import (
    apply_supplier_product_fields,
    confirm_festo_handover,
    create_festo_preview,
    current_publication,
    enqueue_publication,
    get_or_create_company_assembly_identity,
    get_or_create_product_identity,
    get_or_create_project_identity,
    get_or_create_project_assembly_identity,
    mark_project_out_of_date,
    mark_product_out_of_date,
    publish_resource,
    synchronize_product_aas_profile,
)


class FakeRepository:
    def __init__(self):
        self.environments = {}

    def publish(self, document, revision_aas_id):
        revision = document.as_revision_repository_environment(revision_aas_id)
        self.environments[revision_aas_id] = revision
        return revision

    def read_environment(self, aas_id):
        return self.environments[aas_id]


class FakeArtifactStore:
    def __init__(self):
        self.files = {}

    def put_json(self, key, payload):
        self.files[key] = payload
        return key

    def put_aasx(self, key, payload):
        self.files[key] = payload
        return key


class FailingRepository:
    def publish(self, document, revision_aas_id):
        del document, revision_aas_id
        raise ConnectionError("repository unavailable")


@override_settings(
    AAS_ENABLED=True,
    AAS_EXTERNAL_API_ENABLED=True,
    AAS_EXTERNAL_BASE_URL="https://aas.example",
    FESTO_AAS_CLIENT_SUBJECT="festo-test",
    FESTO_AAS_CLIENT_ID="festo-client-id",
    FESTO_PROJECT_LAUNCH_URL="https://aas.festo.test/myfestodataspaceconnector/create-project",
    FESTO_GRANT_TTL_SECONDS=3600,
    FESTO_GRANT_MIN_EXPIRES_AT="2026-10-31T23:59:59Z",
    FESTO_CONFIRMATION_MAX_AGE_SECONDS=900,
)
class AASDomainTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        cls.notification_patcher.start()
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        post_save.connect(create_default_folders, sender=Project)
        post_save.connect(notify_on_project_save, sender=Project)
        cls.notification_patcher.stop()

    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.media_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.license_permission_patcher = patch(
            "ecpmpapp.permissions.LicensePermission.has_permission",
            return_value=True,
        )
        self.license_permission_patcher.start()
        self.addCleanup(self.license_permission_patcher.stop)
        self.api = APIClient()
        self.user = User.objects.create_user(
            email="aas-owner@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            company_name="AAS Test Company",
            company_address="Street 1",
            post_code="10000",
            company_town="Berlin",
            company_country="Germany",
            company_tell_no="-",
            company_mob_no="-",
            company_web_address="https://example.com",
            company_email_address="aas@example.com",
            company_tax_reference="TAX",
            company_iban="DEMO",
            admin_user=self.user,
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.aas_view_permission = Permission.objects.create(
            key="aas_models.view",
            name="View AAS Models",
            module="aas_models",
        )
        self.aas_full_access_permission = Permission.objects.create(
            key="aas_models.full_access",
            name="AAS Models Full Access",
            module="aas_models",
        )
        self.aas_role = Role.objects.create(
            name="AAS Test Role",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=self.aas_role,
            permission=self.aas_full_access_permission,
        )
        UserRole.objects.create(
            user=self.user,
            role=self.aas_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.project = Project.objects.create(
            project_name="AAS Project",
            company_profile=self.company,
            user=self.user,
            contract_value=Decimal("100.00"),
            solution="Energy Consultant Solution",
        )
        self.product = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Servo drive",
            part_number="PN-100",
            sort_order=1,
            custom_fields={
                "supplier": "Festo",
                "rated_voltage": "400 V",
                "rated_voltage_manual_override": True,
                "quantity": "5",
                "supplier_aas_id": "https://supplier.example/aas/pn-100",
                "name_de": "Servoantrieb",
                "name_fr": "Servovariateur",
            },
        )
        ProductAASProfile.objects.create(
            product=self.product,
            origin=ProductAASProfile.Origin.PURCHASED,
            manufacturer_name="Festo",
            supplier_name="Festo",
            supplier_aas_id="https://supplier.example/aas/pn-100",
            order_code="PN-100",
        )
        PartsListConfig.objects.create(
            company_profile=self.company,
            available_product_columns=[
                {
                    "key": "rated_voltage",
                    "label": "Rated voltage",
                    "idta_semantic_id": "https://example.com/rated-voltage",
                    "aas_ownership": "aas",
                },
                {
                    "key": "quantity",
                    "label": "Quantity",
                    "idta_semantic_id": "",
                    "aas_ownership": "links",
                },
            ],
        )
        PartsListConfig.objects.create(
            company_profile=self.company,
            project=self.project,
            bom_columns=[
                {"key": "quantity", "label": "Quantity", "idta_semantic_id": "https://example.com/quantity"}
            ],
        )

    def test_canonical_hash_is_independent_of_dictionary_order(self):
        self.assertEqual(canonical_json({"b": 2, "a": 1}), canonical_json({"a": 1, "b": 2}))
        self.assertEqual(source_hash({"b": 2, "a": 1}), source_hash({"a": 1, "b": 2}))

    def test_demo_seed_and_oracle_commands(self):
        seed_output = io.StringIO()
        verify_output = io.StringIO()
        call_command("seed_aas_demo", stdout=seed_output)
        call_command("verify_aas_demo", stdout=verify_output)
        self.assertIn("Seeded AAS demo project", seed_output.getvalue())
        self.assertIn('"rows": 5', verify_output.getvalue())

    def test_empty_bom_is_valid_aas_but_blocked_from_publication(self):
        identity = get_or_create_project_identity(self.project)
        document = ProjectBOMAASMapper().map(self.project, identity.logical_aas_id)
        self.assertEqual(document.validation[0].code, "empty_bom")
        with self.assertRaises(ValidationError):
            publish_resource(
                identity.id,
                actor=self.user,
                repository=FakeRepository(),
                artifact_store=FakeArtifactStore(),
            )
        self.assertFalse(AASPublication.objects.filter(resource=identity).exists())

    def test_publication_outbox_coalesces_pending_updates(self):
        identity = get_or_create_project_identity(self.project)
        first = enqueue_publication(identity, actor_id=self.user.id)
        second = enqueue_publication(identity, actor_id=self.user.id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            AASOutboxEvent.objects.filter(
                resource=identity,
                status=AASOutboxEvent.Status.PENDING,
            ).count(),
            1,
        )

    @override_settings(AAS_AUTO_PUBLISH_ENABLED=True)
    def test_empty_project_does_not_create_automatic_publication(self):
        with patch("aas_integration.services._wake_publication_worker") as wake_worker:
            mark_project_out_of_date(self.project, self.user.id)

        self.assertFalse(AASResourceIdentity.objects.filter(project=self.project).exists())
        self.assertFalse(AASOutboxEvent.objects.filter(resource__project=self.project).exists())
        wake_worker.assert_not_called()

    @override_settings(AAS_AUTO_PUBLISH_ENABLED=True)
    def test_empty_published_project_becomes_stale_without_failed_publication(self):
        identity = get_or_create_project_identity(self.project)
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])

        with patch("aas_integration.services._wake_publication_worker") as wake_worker:
            mark_project_out_of_date(self.project, self.user.id)

        identity.refresh_from_db()
        self.assertTrue(identity.out_of_date)
        self.assertFalse(AASOutboxEvent.objects.filter(resource=identity).exists())
        wake_worker.assert_not_called()

    @override_settings(AAS_AUTO_PUBLISH_ENABLED=True)
    def test_first_bom_row_enqueues_automatic_publication(self):
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Line",
            level=1,
            sort_order=1,
        )

        with patch("aas_integration.services._wake_publication_worker") as wake_worker:
            mark_project_out_of_date(self.project, self.user.id)

        identity = AASResourceIdentity.objects.get(project=self.project)
        event = AASOutboxEvent.objects.get(resource=identity)
        self.assertEqual(event.status, AASOutboxEvent.Status.PENDING)
        wake_worker.assert_called_once_with(event)

    def test_product_mapper_uses_stable_public_identity_and_semantics(self):
        identity = get_or_create_product_identity(self.product)
        document = ProductAASMapper().map(self.product, identity.logical_aas_id)
        self.assertNotIn(f"/{self.product.id}", identity.logical_aas_id)
        self.assertEqual(document.shell["assetInformation"]["assetKind"], "Type")
        self.assertEqual(
            {item["language"] for item in document.shell["displayName"]},
            {"en", "de", "fr"},
        )
        technical = document.submodels[1]
        voltage = next(item for item in technical["submodelElements"] if item["idShort"] == "rated_voltage")
        self.assertEqual(voltage["value"], "400 V")
        self.assertEqual(voltage["semanticId"]["keys"][0]["value"], "https://example.com/rated-voltage")
        self.assertFalse(
            any(item["idShort"] == "quantity" for item in technical["submodelElements"])
        )
        self.assertFalse(
            any(message.path == "TechnicalData.quantity" for message in document.validation)
        )
        self.assertFalse(any(item["idShort"].endswith("manual_override") for item in technical["submodelElements"]))

    def test_product_without_an_origin_profile_cannot_be_published(self):
        unclassified = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Unclassified product",
            part_number="UNCLASSIFIED-1",
            sort_order=2,
        )
        document = ProductAASMapper().map(
            unclassified,
            "https://aas.sharemundolinks.com/ids/unclassified",
        )
        self.assertEqual(document.validation[0].level, "error")
        self.assertEqual(document.validation[0].code, "unclassified_product_origin")

    def test_product_origin_backfill_is_dry_run_by_default(self):
        candidate = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Supplier product",
            part_number="SUP-1",
            sort_order=3,
        )
        profile = ProductAASProfile.objects.create(
            product=candidate,
            supplier_aas_id="https://supplier.example/aas/SUP-1",
        )
        output = io.StringIO()

        call_command("classify_aas_product_origins", stdout=output)
        profile.refresh_from_db()
        self.assertEqual(profile.origin, ProductAASProfile.Origin.UNCLASSIFIED)
        self.assertIn("No records changed", output.getvalue())

        call_command("classify_aas_product_origins", "--apply", stdout=output)
        profile.refresh_from_db()
        self.assertEqual(profile.origin, ProductAASProfile.Origin.PURCHASED)

    def test_product_origin_is_inferred_only_from_explicit_company_or_supplier_evidence(self):
        manufactured = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Company motor",
            part_number="OWN-1",
            sort_order=4,
            custom_fields={"manufacturer": self.company.company_name},
        )
        purchased = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Supplier sensor",
            part_number="BUY-1",
            sort_order=5,
            custom_fields={"manufacturer": "SICK", "supplier": "SICK AG"},
        )
        ambiguous = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Unknown origin",
            part_number="UNKNOWN-1",
            sort_order=6,
        )

        self.assertEqual(
            synchronize_product_aas_profile(manufactured).origin,
            ProductAASProfile.Origin.MANUFACTURED,
        )
        self.assertEqual(
            synchronize_product_aas_profile(purchased).origin,
            ProductAASProfile.Origin.PURCHASED,
        )
        self.assertEqual(
            synchronize_product_aas_profile(ambiguous).origin,
            ProductAASProfile.Origin.UNCLASSIFIED,
        )

        explicit = ProductAASProfile.objects.get(product=purchased)
        explicit.origin = ProductAASProfile.Origin.MANUFACTURED
        explicit.save(update_fields=["origin", "updated_at"])
        self.assertEqual(
            synchronize_product_aas_profile(purchased).origin,
            ProductAASProfile.Origin.MANUFACTURED,
        )

    def test_purchased_fields_are_locked_and_manufactured_profile_updates_are_published(self):
        self.api.force_authenticate(self.user)
        profile_url = f"/api/aas/company-products/{self.product.id}/profile/"
        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            denied = self.api.patch(
                profile_url,
                {"manufacturer_name": "Tampered manufacturer"},
                format="json",
            )
        self.assertEqual(denied.status_code, 400)

        product_serializer = AvailableProductPartSerializer(
            self.product,
            data={"name": "Tampered supplier product"},
            partial=True,
            context={"request": type("Request", (), {"user": self.user})()},
        )
        self.assertFalse(product_serializer.is_valid())
        self.assertIn("name", product_serializer.errors)

        links_owned_serializer = AvailableProductPartSerializer(
            self.product,
            data={"custom_fields": {"quantity": "6"}},
            partial=True,
            context={"request": type("Request", (), {"user": self.user})()},
        )
        self.assertTrue(links_owned_serializer.is_valid(), links_owned_serializer.errors)
        links_owned_serializer.save()
        self.product.refresh_from_db()
        self.assertEqual(self.product.custom_fields["quantity"], "6")
        self.assertEqual(
            AvailableProductPartSerializer(self.product).data["aas_origin"],
            ProductAASProfile.Origin.PURCHASED,
        )
        bom = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Matrix row",
            level=1,
            sort_order=1,
        )
        self.assertNotIn("aas_origin", BillOfMaterialPartSerializer(bom).data)

        protected_technical_serializer = AvailableProductPartSerializer(
            self.product,
            data={"custom_fields": {"rated_voltage": "999 V"}},
            partial=True,
            context={"request": type("Request", (), {"user": self.user})()},
        )
        self.assertFalse(protected_technical_serializer.is_valid())
        self.assertIn("custom_fields", protected_technical_serializer.errors)

        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        identity = get_or_create_product_identity(self.product)
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])
        with override_settings(AAS_AUTO_PUBLISH_ENABLED=True), patch(
            "aas_integration.services._wake_publication_worker"
        ) as wake_worker, patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            updated = self.api.patch(
                profile_url,
                {
                    "product_name": "ShareMundo servo drive",
                    "article_number": "SM-100",
                    "manufacturer_name": "ShareMundo",
                    "technical_values": {"rated_voltage": "690 V"},
                    "profile_key": "client-controlled-value",
                },
                format="json",
            )
        self.assertEqual(updated.status_code, 200, updated.data)
        self.product.refresh_from_db()
        profile.refresh_from_db()
        identity.refresh_from_db()
        self.assertEqual(self.product.name, "ShareMundo servo drive")
        self.assertEqual(self.product.part_number, "SM-100")
        self.assertEqual(self.product.custom_fields["rated_voltage"], "690 V")
        self.assertEqual(profile.manufacturer_name, "ShareMundo")
        self.assertEqual(profile.profile_key, "idta-product")
        self.assertTrue(identity.out_of_date)
        event = AASOutboxEvent.objects.get(resource=identity)
        wake_worker.assert_called_once_with(event)

    def test_typed_product_columns_are_validated_exposed_and_mapped(self):
        config = PartsListConfig.objects.get(
            company_profile=self.company,
            project__isnull=True,
        )
        config.available_product_columns = normalize_available_product_columns(
            [
                {
                    "key": "rated_voltage",
                    "label": "Rated voltage",
                    "type": "number",
                    "unit": "V",
                    "idta_semantic_id": "https://example.com/rated-voltage",
                },
                {
                    "key": "ingress_protection",
                    "label": "Ingress protection",
                    "type": "enum",
                    "options": ["IP54", "IP65"],
                    "idta_semantic_id": "https://example.com/ingress-protection",
                },
            ]
        )
        config.save(update_fields=["available_product_columns", "updated_at"])
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        profile_url = f"/api/aas/company-products/{self.product.id}/profile/"
        self.api.force_authenticate(self.user)

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(profile_url)
            invalid = self.api.patch(
                profile_url,
                {
                    "technical_values": {
                        "rated_voltage": "not-a-number",
                        "ingress_protection": "IP99",
                    }
                },
                format="json",
            )
            valid = self.api.patch(
                profile_url,
                {
                    "technical_values": {
                        "rated_voltage": "690",
                        "ingress_protection": "IP65",
                    }
                },
                format="json",
            )

        self.assertEqual(response.status_code, 200, response.data)
        fields = {item["key"]: item for item in response.data["technical_fields"]}
        self.assertEqual(fields["rated_voltage"]["data_type"], "number")
        self.assertEqual(fields["rated_voltage"]["unit"], "V")
        self.assertEqual(fields["ingress_protection"]["options"], ["IP54", "IP65"])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(valid.status_code, 200, valid.data)

        self.product.refresh_from_db()
        document = ProductAASMapper().map(
            self.product,
            "https://aas.sharemundolinks.com/ids/typed-product",
        )
        technical = next(
            submodel
            for submodel in document.submodels
            if submodel["idShort"] == "TechnicalData"
        )
        elements = {
            element["idShort"]: element
            for element in technical["submodelElements"]
        }
        self.assertEqual(elements["rated_voltage"]["valueType"], "xs:decimal")
        self.assertEqual(elements["rated_voltage"]["value"], "690")
        self.assertEqual(elements["ingress_protection"]["value"], "IP65")

    def test_regular_manufactured_product_edit_marks_published_aas_out_of_date(self):
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        identity = get_or_create_product_identity(self.product)
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])
        payload = {
            **self.product.custom_fields,
            "rated_voltage": "500 V",
        }
        serializer = AvailableProductPartSerializer(
            self.product,
            data={"custom_fields": payload},
            partial=True,
            context={"request": type("Request", (), {"user": self.user})()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        with override_settings(AAS_AUTO_PUBLISH_ENABLED=True), patch(
            "aas_integration.services._wake_publication_worker"
        ) as wake_worker:
            serializer.save()
        identity.refresh_from_db()
        self.assertTrue(identity.out_of_date)
        event = AASOutboxEvent.objects.get(resource=identity)
        wake_worker.assert_called_once_with(event)

    def test_solution_space_assembly_infers_supplier_origin_before_initialization(self):
        assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Purchased valve terminal",
            custom_fields={
                "supplier": "Festo",
                "manufacturer": "Festo SE",
                "article_number": "VTUG-1",
                "supplier_aas_id": "https://smart.festo.com/aas/VTUG-1",
            },
            sort_order=10,
        )
        self.api.force_authenticate(self.user)
        listed = self.api.get("/api/aas/company-assemblies/")
        self.assertEqual(listed.status_code, 200, listed.data)
        row = next(item for item in listed.data["results"] if item["assembly_id"] == assembly.id)
        self.assertEqual(listed.data["count"], 1)
        self.assertIsNone(row["product_id"])
        self.assertEqual(row["origin"], ProductAASProfile.Origin.PURCHASED)
        self.assertTrue(row["aas_eligible"])
        self.assertFalse(row["editable_aas_fields"])

        with patch(
            "aas_integration.views.CompanySolutionSpacePermission.has_object_permission",
            return_value=True,
        ):
            initialized = self.api.post(
                f"/api/aas/company-assemblies/{assembly.id}/initialize/",
                {},
                format="json",
            )
        self.assertEqual(initialized.status_code, 201, initialized.data)
        assembly.refresh_from_db()
        self.assertIsNotNone(assembly.aas_product_id)
        profile = assembly.aas_product.aas_profile
        self.assertEqual(profile.origin, ProductAASProfile.Origin.PURCHASED)
        self.assertEqual(profile.supplier_name, "Festo")
        self.assertEqual(profile.manufacturer_name, "Festo SE")
        self.assertEqual(profile.order_code, "VTUG-1")

        with patch(
            "aas_integration.views.CompanySolutionSpacePermission.has_object_permission",
            return_value=True,
        ):
            invalid = self.api.post(
                f"/api/aas/company-assemblies/{assembly.id}/initialize/",
                {"origin": ProductAASProfile.Origin.UNCLASSIFIED},
                format="json",
            )
        self.assertEqual(invalid.status_code, 400)

    def test_solution_space_mappers_publish_only_the_selected_subtree(self):
        SolutionSpaceConfig.objects.create(
            company_profile=self.company,
            columns=[
                {
                    "key": "capacity",
                    "label": "Capacity",
                    "idta_semantic_id": "https://example.com/capacity",
                }
            ],
        )
        root = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Packaging line",
            aas_product=self.product,
            custom_fields={"capacity": "120 units/min"},
            sort_order=1,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=root,
            assembly_name="Infeed",
            level=2,
            sort_order=1,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Unrelated line",
            sort_order=2,
        )
        get_or_create_product_identity(self.product)
        company_identity = get_or_create_company_assembly_identity(root)
        company_document = SolutionSpaceAASMapper().map(
            root,
            company_identity.logical_aas_id,
        )
        company_entry = company_document.submodels[0]["submodelElements"][0]
        company_children = [
            element
            for element in company_entry["statements"]
            if element["modelType"] == "Entity"
        ]
        self.assertEqual(company_document.shell["assetInformation"]["assetKind"], "Type")
        self.assertEqual(company_entry["idShort"], "Assembly_1")
        self.assertEqual([child["idShort"] for child in company_children], ["Assembly_1_1"])
        self.assertNotIn("Unrelated", canonical_json(company_document.as_environment()).decode())
        self.assertEqual(
            {submodel["idShort"] for submodel in company_document.submodels},
            {
                "AssemblyMetadata",
                "HierarchicalStructures",
                "Nameplate",
                "TechnicalData",
            },
        )
        technical_data = next(
            submodel
            for submodel in company_document.submodels
            if submodel["idShort"] == "TechnicalData"
        )
        self.assertEqual(
            {
                element["idShort"]: element["value"]
                for element in technical_data["submodelElements"]
            }["rated_voltage"],
            "400 V",
        )
        self.assertTrue(
            any(
                item["field_path"] == "custom_fields.rated_voltage"
                and item["owner"] == "supplier"
                for item in company_document.provenance
            )
        )

        project_root = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=root,
            assembly_name="Project packaging line",
            sort_order=1,
        )
        ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            parent=project_root,
            assembly_name="Project infeed",
            level=2,
            sort_order=1,
        )
        project_identity = get_or_create_project_assembly_identity(project_root)
        project_document = SolutionSpaceAASMapper().map(
            project_root,
            project_identity.logical_aas_id,
        )
        self.assertEqual(
            project_document.shell["assetInformation"]["assetKind"],
            "Instance",
        )
        self.assertIn(
            "TechnicalData",
            {submodel["idShort"] for submodel in project_document.submodels},
        )
        self.assertEqual(
            company_identity.source_type,
            AASResourceIdentity.SourceType.COMPANY_ASSEMBLY,
        )
        self.assertEqual(
            project_identity.source_type,
            AASResourceIdentity.SourceType.PROJECT_ASSEMBLY,
        )

    def test_solution_space_aas_endpoints_use_assembly_resources(self):
        company_assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Company assembly",
            sort_order=1,
        )
        project_assembly = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Project assembly",
            sort_order=1,
        )
        self.api.force_authenticate(self.user)
        with patch(
            "aas_integration.views.CompanySolutionSpacePermission.has_object_permission",
            return_value=True,
        ), patch(
            "aas_integration.views.ProjectSolutionSpacePermission.has_object_permission",
            return_value=True,
        ):
            company_model = self.api.get(
                f"/api/aas/company-assemblies/{company_assembly.id}/model/"
            )
            project_model = self.api.get(
                f"/api/aas/project-assemblies/{project_assembly.id}/model/"
            )
            company_validation = self.api.post(
                f"/api/aas/company-assemblies/{company_assembly.id}/validate/"
            )
            project_validation = self.api.post(
                f"/api/aas/project-assemblies/{project_assembly.id}/validate/"
            )

        self.assertEqual(company_model.status_code, 200, company_model.data)
        self.assertEqual(project_model.status_code, 200, project_model.data)
        self.assertEqual(company_validation.status_code, 200, company_validation.data)
        self.assertEqual(project_validation.status_code, 200, project_validation.data)
        self.assertEqual(
            company_model.data["environment"]["assetAdministrationShells"][0][
                "assetInformation"
            ]["assetKind"],
            "Type",
        )
        self.assertEqual(
            project_model.data["environment"]["assetAdministrationShells"][0][
                "assetInformation"
            ]["assetKind"],
            "Instance",
        )

    def test_company_assembly_publication_persists_linked_product_provenance(self):
        assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Supplier servo assembly",
            aas_product=self.product,
            sort_order=1,
        )
        identity = get_or_create_company_assembly_identity(assembly)

        publish_resource(
            identity.id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=FakeArtifactStore(),
        )

        provenance = AASFieldProvenance.objects.get(
            resource=identity,
            field_path="custom_fields.rated_voltage",
        )
        self.assertEqual(provenance.owner, "supplier")
        self.assertEqual(
            provenance.source_aas_id,
            self.product.aas_profile.supplier_aas_id,
        )
        self.assertEqual(provenance.last_value, "400 V")

    def test_project_mapper_preserves_hierarchy_and_order(self):
        root = BillOfMaterialPart.objects.create(project=self.project, name="Line", level=1, sort_order=1)
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=root,
            available_product=self.product,
            name="Servo drive",
            part_number="PN-100",
            level=2,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        identity = get_or_create_project_identity(self.project)
        document = ProjectBOMAASMapper().map(self.project, identity.logical_aas_id)
        entry_node = document.submodels[0]["submodelElements"][0]
        root_entity = next(item for item in entry_node["statements"] if item["modelType"] == "Entity")
        child_entity = next(item for item in root_entity["statements"] if item["modelType"] == "Entity")
        root_relation = next(item for item in entry_node["statements"] if item["modelType"] == "RelationshipElement")
        child_relation = next(item for item in root_entity["statements"] if item["modelType"] == "RelationshipElement")
        self.assertEqual(entry_node["idShort"], "EntryNode")
        self.assertEqual([root_entity["idShort"], child_entity["idShort"]], ["Part_1", "Part_1_1"])
        self.assertEqual(root_relation["semanticId"]["keys"][0]["value"], "https://admin-shell.io/idta/HierarchicalStructures/HasPart/1/0")
        self.assertEqual(child_relation["second"]["keys"][-1]["value"], "Part_1_1")
        metadata = document.submodels[1]
        self.assertEqual(metadata["idShort"], "ProjectMetadata")
        self.assertEqual(
            next(item["value"] for item in metadata["submodelElements"] if item["idShort"] == "ProjectName"),
            self.project.project_name,
        )
        quantity = next(
            item
            for item in document.provenance
            if item["field_path"] == "custom_fields.quantity"
        )
        self.assertEqual(quantity["owner"], "links")

    def test_thousand_row_bom_maps_and_validates_within_budget(self):
        BillOfMaterialPart.objects.bulk_create(
            [
                BillOfMaterialPart(
                    project=self.project,
                    name=f"Part {index:04d}",
                    part_number=f"PERF-{index:04d}",
                    level=1,
                    sort_order=index,
                    custom_fields={"quantity": 1},
                )
                for index in range(1, 1001)
            ],
            batch_size=250,
        )
        identity = get_or_create_project_identity(self.project)
        started_at = time.monotonic()
        document = ProjectBOMAASMapper().map(self.project, identity.logical_aas_id)
        parse_aas_environment(document.as_environment())
        elapsed = time.monotonic() - started_at
        entry_node = document.submodels[0]["submodelElements"][0]
        entities = [
            item for item in entry_node["statements"] if item["modelType"] == "Entity"
        ]
        self.assertEqual(len(entities), 1000)
        self.assertLess(elapsed, 30)

    def test_project_model_api_is_permission_scoped_and_hides_database_ids(self):
        BillOfMaterialPart.objects.create(project=self.project, name="Line", level=1, sort_order=1)
        self.api.force_authenticate(self.user)
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True), patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(f"/api/aas/projects/{self.project.id}/model/")
        self.assertEqual(response.status_code, 200, response.data)
        payload = response.json()
        shell_id = payload["environment"]["assetAdministrationShells"][0]["id"]
        self.assertNotIn(f"/{self.project.id}", shell_id)
        self.assertNotIn(f"/{self.project.bom_parts.get().id}", str(payload["environment"]))

        outsider = User.objects.create_user(
            email="aas-outsider@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Outsider",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            company_name="Other Company",
            company_address="Street 2",
            post_code="20000",
            company_town="Paris",
            company_country="France",
            company_tell_no="-",
            company_mob_no="-",
            company_web_address="https://other.example.com",
            company_email_address="other@example.com",
            company_tax_reference="OTHER",
            company_iban="OTHER",
            admin_user=outsider,
        )
        outsider.company = other_company
        outsider.save(update_fields=["company"])
        self.api.force_authenticate(outsider)
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True), patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ):
            denied = self.api.get(f"/api/aas/projects/{self.project.id}/model/")
        self.assertIn(denied.status_code, {403, 404})

    def test_project_assembly_selection_focuses_subtree_without_filtering_the_model(self):
        company_assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            aas_product=self.product,
            assembly_name="Servo package",
            sort_order=1,
        )
        project_assembly = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=company_assembly,
            assembly_name="Servo package",
            level=1,
            sort_order=1,
        )
        root = BillOfMaterialPart.objects.create(
            project=self.project,
            available_product=self.product,
            name="Servo package",
            level=1,
            sort_order=1,
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=root,
            name="Servo cable",
            level=2,
            sort_order=1,
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Unrelated SICK sensor",
            level=1,
            sort_order=2,
        )
        self.api.force_authenticate(self.user)
        with patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(
                f"/api/aas/projects/{self.project.id}/model/?assembly_id={project_assembly.id}"
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["focus"]["id_shorts"],
            ["Part_1", "Part_1_1"],
        )
        self.assertIn("Unrelated SICK sensor", str(response.data["environment"]))

    def test_project_bom_selection_focuses_selected_row_and_descendants(self):
        root = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Assembly line",
            level=1,
            sort_order=1,
        )
        station = BillOfMaterialPart.objects.create(
            project=self.project,
            parent=root,
            name="Drive station",
            level=2,
            sort_order=1,
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=station,
            name="Servo drive",
            level=3,
            sort_order=1,
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=root,
            name="Unrelated station",
            level=2,
            sort_order=2,
        )
        self.api.force_authenticate(self.user)
        with patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(
                f"/api/aas/projects/{self.project.id}/model/?bom_part_id={station.id}"
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["focus"]["bom_part_id"], station.id)
        self.assertEqual(
            response.data["focus"]["id_shorts"],
            ["Part_1_1", "Part_1_1_1"],
        )
        self.assertIn("Unrelated station", str(response.data["environment"]))

    def test_aasx_preview_normalizes_valid_models_and_rejects_unsafe_archives(self):
        identity = get_or_create_product_identity(self.product)
        payload = create_aasx(
            ProductAASMapper().map(self.product, identity.logical_aas_id).as_environment()
        )
        preview = inspect_aasx(
            payload,
            max_entries=100,
            max_decompressed_bytes=10 * 1024 * 1024,
            max_compression_ratio=100,
        )
        self.assertEqual(preview["shells"][0]["id"], identity.logical_aas_id)
        self.assertEqual(
            {submodel["id_short"] for submodel in preview["submodels"]},
            {"Nameplate", "TechnicalData"},
        )

        unsafe = io.BytesIO()
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("../outside.txt", "not allowed")
        with self.assertRaisesMessage(ValueError, "unsafe archive path"):
            inspect_aasx(
                unsafe.getvalue(),
                max_entries=100,
                max_decompressed_bytes=1024,
                max_compression_ratio=100,
            )

        folder = Folder.objects.create(
            project=self.project,
            name="AASX previews",
        )
        document = Document.objects.create(
            folder=folder,
            author=self.user,
            file_name="product.aasx",
            created_by=self.user,
        )
        document.file.save("product.aasx", ContentFile(payload), save=True)
        self.api.force_authenticate(self.user)
        with patch(
            "aas_integration.views.get_accessible_document_queryset",
            return_value=Document.objects.filter(id=document.id),
        ):
            response = self.api.get(
                f"/api/aas/documents/{document.id}/aasx-preview/"
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "ready")
        self.assertEqual(response.data["checksum"], preview["checksum"])

        missing = Document.objects.create(
            folder=folder,
            author=self.user,
            file="missing-file.aasx",
            file_name="missing-file.aasx",
            created_by=self.user,
        )
        with patch(
            "aas_integration.views.get_accessible_document_queryset",
            return_value=Document.objects.filter(id=missing.id),
        ):
            unavailable = self.api.get(
                f"/api/aas/documents/{missing.id}/aasx-preview/"
            )
        self.assertEqual(unavailable.status_code, 422)
        self.assertEqual(unavailable.data["detail"], "The document file is unavailable.")

    def test_aasx_preview_supports_legacy_read_only_festo_packages(self):
        legacy_xml = b"""<?xml version="1.0"?>
        <aas:aasenv xmlns:aas="http://www.admin-shell.io/aas/1/0">
          <aas:assetAdministrationShells>
            <aas:assetAdministrationShell>
              <aas:idShort>LegacyFestoShell</aas:idShort>
              <aas:identification idType="URI">https://example.test/legacy-shell</aas:identification>
              <aas:assetRef><aas:keys><aas:key>https://example.test/legacy-asset</aas:key></aas:keys></aas:assetRef>
              <aas:submodelRefs><aas:submodelRef><aas:keys><aas:key>https://example.test/nameplate</aas:key></aas:keys></aas:submodelRef></aas:submodelRefs>
            </aas:assetAdministrationShell>
          </aas:assetAdministrationShells>
          <aas:assets><aas:asset><aas:identification>https://example.test/legacy-asset</aas:identification><aas:kind>Type</aas:kind></aas:asset></aas:assets>
          <aas:submodels>
            <aas:submodel>
              <aas:idShort>Nameplate</aas:idShort>
              <aas:identification>https://example.test/nameplate</aas:identification>
              <aas:submodelElements><aas:submodelElement><aas:property><aas:idShort>ManufacturerName</aas:idShort><aas:valueType>string</aas:valueType><aas:value>Festo</aas:value></aas:property></aas:submodelElement></aas:submodelElements>
            </aas:submodel>
          </aas:submodels>
        </aas:aasenv>"""
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("aasx/aasx-origin", "")
            archive.writestr(
                "_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="http://www.admin-shell.io/aasx/relationships/aasx-origin" Target="/aasx/aasx-origin" Id="root" /></Relationships>',
            )
            archive.writestr(
                "aasx/_rels/aasx-origin.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Type="http://www.admin-shell.io/aasx/relationships/aas-spec" Target="/aasx/model.aas.xml" Id="model" /></Relationships>',
            )
            archive.writestr("aasx/model.aas.xml", legacy_xml)
            archive.writestr("aasx/files/manual.pdf", b"legacy manual")

        preview = inspect_aasx(
            payload.getvalue(),
            max_entries=100,
            max_decompressed_bytes=1024 * 1024,
            max_compression_ratio=100,
        )

        self.assertEqual(preview["shells"][0]["id_short"], "LegacyFestoShell")
        self.assertEqual(preview["submodels"][0]["id_short"], "Nameplate")
        self.assertEqual(
            preview["submodels"][0]["elements"][0]["value"],
            "Festo",
        )
        self.assertEqual(preview["warnings"][0]["code"], "legacy_aas_version")
        self.assertEqual(preview["supplementary_files"][0]["name"], "aasx/files/manual.pdf")

    def test_publication_is_deduplicated_and_aasx_round_trips(self):
        BillOfMaterialPart.objects.create(project=self.project, name="Line", level=1, sort_order=1)
        identity = get_or_create_project_identity(self.project)
        repository = FakeRepository()
        artifacts = FakeArtifactStore()
        first = publish_resource(identity.id, actor=self.user, repository=repository, artifact_store=artifacts)
        second = publish_resource(identity.id, actor=self.user, repository=repository, artifact_store=artifacts)
        self.assertEqual(first.id, second.id)
        self.assertEqual(AASPublication.objects.filter(resource=identity).count(), 1)
        aasx = artifacts.files[first.aasx_artifact_key]
        with zipfile.ZipFile(io.BytesIO(aasx)) as archive:
            self.assertIn("aasx/data.json", archive.namelist())
        round_trip = read_aasx(aasx)
        self.assertEqual(len(round_trip), 3)
        revision_environment = repository.environments[
            f"{identity.logical_aas_id}/revisions/{first.revision}"
        ]
        for submodel in revision_environment["submodels"]:
            self.assertEqual(
                next(
                    item["value"]
                    for item in submodel["submodelElements"]
                    if item["idShort"] == "PublicationRevision"
                ),
                str(first.revision),
            )

    def test_deduplicated_publication_restores_a_missing_repository_revision(self):
        BillOfMaterialPart.objects.create(project=self.project, name="Line", level=1, sort_order=1)
        identity = get_or_create_project_identity(self.project)
        repository = FakeRepository()
        artifacts = FakeArtifactStore()
        first = publish_resource(identity.id, actor=self.user, repository=repository, artifact_store=artifacts)
        revision_aas_id = f"{identity.logical_aas_id}/revisions/{first.revision}"
        repository.environments.clear()

        restored = publish_resource(identity.id, actor=self.user, repository=repository, artifact_store=artifacts)

        self.assertEqual(restored.id, first.id)
        self.assertIn(revision_aas_id, repository.environments)
        self.assertEqual(AASPublication.objects.filter(resource=identity).count(), 1)

    def test_reverting_source_reactivates_the_matching_publication(self):
        row = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Line",
            part_number="original",
            level=1,
            sort_order=1,
        )
        identity = get_or_create_project_identity(self.project)
        repository = FakeRepository()
        artifacts = FakeArtifactStore()
        original = publish_resource(
            identity.id,
            actor=self.user,
            repository=repository,
            artifact_store=artifacts,
        )

        row.part_number = "changed"
        row.save(update_fields=["part_number", "updated_at"])
        changed = publish_resource(
            identity.id,
            actor=self.user,
            repository=repository,
            artifact_store=artifacts,
        )
        self.assertEqual(current_publication(identity).id, changed.id)

        row.part_number = "original"
        row.save(update_fields=["part_number", "updated_at"])
        restored = publish_resource(
            identity.id,
            actor=self.user,
            repository=repository,
            artifact_store=artifacts,
        )

        self.assertEqual(restored.id, original.id)
        self.assertEqual(current_publication(identity).id, original.id)
        self.assertEqual(AASPublication.objects.filter(resource=identity).count(), 2)

        self.api.force_authenticate(self.user)
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True), patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(f"/api/aas/projects/{self.project.id}/publications/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["id"], str(original.id))
        self.assertTrue(response.data[0]["is_active"])
        self.assertFalse(response.data[1]["is_active"])

    def test_aasx_download_query_is_not_consumed_as_a_renderer_override(self):
        BillOfMaterialPart.objects.create(project=self.project, name="Line", level=1, sort_order=1)
        identity = get_or_create_project_identity(self.project)
        publication = publish_resource(
            identity.id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=FakeArtifactStore(),
        )
        self.api.force_authenticate(self.user)
        with patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True), patch(
            "aas_integration.views.BillOfMaterialPermission.has_object_permission",
            return_value=True,
        ), patch(
            "aas_integration.views.DjangoArtifactStore.download_url",
            return_value="https://artifacts.example/environment.aasx",
        ):
            response = self.api.get(
                f"/api/aas/publications/{publication.id}/download/?format=aasx"
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://artifacts.example/environment.aasx")

    def test_failed_publication_preserves_the_last_valid_revision(self):
        row = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Line",
            level=1,
            sort_order=1,
        )
        identity = get_or_create_project_identity(self.project)
        current = publish_resource(
            identity.id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=FakeArtifactStore(),
        )
        row.name = "Changed line"
        row.save(update_fields=["name", "updated_at"])
        identity.out_of_date = True
        identity.save(update_fields=["out_of_date", "updated_at"])

        with self.assertRaises(ConnectionError):
            publish_resource(
                identity.id,
                actor=self.user,
                repository=FailingRepository(),
                artifact_store=FakeArtifactStore(),
            )

        current.refresh_from_db()
        identity.refresh_from_db()
        self.assertEqual(current.status, AASPublication.Status.CURRENT)
        self.assertTrue(identity.out_of_date)
        failed = AASPublication.objects.filter(
            resource=identity,
            status=AASPublication.Status.FAILED,
        ).get()
        self.assertEqual(
            failed.validation_report["repository_error"]["message"],
            "The AAS publication could not be completed.",
        )

    def test_manual_override_becomes_review_proposal(self):
        identity = get_or_create_product_identity(self.product)
        publish_resource(identity.id, actor=self.user, repository=FakeRepository(), artifact_store=FakeArtifactStore())
        result = apply_supplier_product_fields(
            self.product,
            {"rated_voltage": "480 V", "protection_class": "IP67"},
            supplier="Festo",
            source_aas_id="https://supplier.example/aas/pn-100",
            actor_id=self.user.id,
        )
        self.product.refresh_from_db()
        provenance = AASFieldProvenance.objects.get(
            resource=identity,
            field_path="custom_fields.rated_voltage",
        )
        self.assertTrue(provenance.manual_override)
        self.assertEqual(provenance.refresh_state, "review_required")
        self.assertEqual(provenance.proposed_value, "480 V")
        self.assertEqual(self.product.custom_fields["rated_voltage"], "400 V")
        self.assertEqual(self.product.custom_fields["protection_class"], "IP67")
        self.assertEqual(result, {"updated": ["protection_class"], "proposed": ["rated_voltage"]})

    @override_settings(FESTO_AAS_CLIENT_ID="")
    def test_festo_confirmation_requires_an_oauth_client_id(self):
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Servo drive",
            part_number="PN-100",
            level=1,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        handover, token, _ = create_festo_preview(self.project, self.user)

        with self.assertRaisesMessage(ValidationError, "OAuth client"):
            confirm_festo_handover(
                handover.id,
                token,
                self.user,
                repository=FakeRepository(),
                artifact_store=FakeArtifactStore(),
            )

        self.assertFalse(
            AASPublication.objects.filter(resource__festo_handover=handover).exists()
        )

    def test_festo_confirmation_is_idempotent_and_granted_to_exact_revision(self):
        machine = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Machine package",
            level=1,
            sort_order=1,
            custom_fields={"supplier": "ShareMundo", "quantity": 1},
        )
        drive = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Servo drive",
            part_number="PN-100",
            parent=machine,
            level=2,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        adapter = BillOfMaterialPart.objects.create(
            project=self.project,
            name="SICK adapter",
            part_number="SICK-100",
            parent=drive,
            level=3,
            sort_order=1,
            custom_fields={"supplier": "SICK", "quantity": 1},
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Festo valve",
            part_number="VUVG-L10",
            parent=adapter,
            level=4,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 3},
        )
        handover, token, excluded = create_festo_preview(self.project, self.user)
        self.assertEqual(len(excluded), 2)
        self.assertEqual(handover.context_snapshot, [])
        self.assertEqual(handover.item_snapshot[1]["parent_row_id"], drive.id)
        repository = FakeRepository()
        first = confirm_festo_handover(
            handover.id,
            token,
            self.user,
            repository=repository,
            artifact_store=FakeArtifactStore(),
        )
        second = confirm_festo_handover(handover.id, token, self.user)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.status, first.Status.READY)
        self.assertEqual(first.external_project_reference, "")
        self.assertGreaterEqual(
            first.grant_expires_at,
            datetime(2026, 10, 31, 23, 59, 59, tzinfo=datetime_timezone.utc),
        )
        self.assertEqual(first.publication.resource.source_type, AASResourceIdentity.SourceType.FESTO_HANDOVER)
        self.assertIn("/api/v3.1/shells/", first.aas_endpoint)
        encoded_endpoint = parse_qs(urlparse(first.external_url).query)["aasEndpoint"][0]
        self.assertNotIn("=", encoded_endpoint)
        padding = "=" * (-len(encoded_endpoint) % 4)
        decoded_endpoint = __import__("base64").urlsafe_b64decode(encoded_endpoint + padding).decode()
        self.assertEqual(decoded_endpoint, first.aas_endpoint)
        self.assertEqual(ExternalAASGrant.objects.filter(publication=first.publication).count(), 1)
        self.assertFalse(
            ExternalAASGrant.objects.filter(resource__source_type=AASResourceIdentity.SourceType.PROJECT).exists()
        )
        revision_id = f"{first.publication.resource.logical_aas_id}/revisions/{first.publication.revision}"
        exposed_environment = repository.environments[revision_id]
        self.assertIn("PN-100", str(exposed_environment))
        self.assertIn("VUVG-L10", str(exposed_environment))
        self.assertNotIn("Machine package", str(exposed_environment))
        self.assertNotIn("SICK-100", str(exposed_environment))

        def entities(value):
            if isinstance(value, dict):
                if value.get("modelType") == "Entity":
                    yield value
                for child in value.values():
                    yield from entities(child)
            elif isinstance(value, list):
                for child in value:
                    yield from entities(child)

        product_entities = list(entities(exposed_environment))
        self.assertEqual(len(product_entities), 2)
        self.assertFalse(any(entity["idShort"] == "EntryNode" for entity in product_entities))
        for entity in product_entities:
            self.assertEqual(entity["entityType"], "SelfManagedEntity")
            self.assertTrue(
                entity["globalAssetId"].startswith("https://smart.festo.com/asset/type/oc/")
            )
        root_entity = next(
            entity for entity in product_entities if entity["globalAssetId"].endswith("/PN-100")
        )
        child_entity = next(
            entity for entity in product_entities if entity["globalAssetId"].endswith("/VUVG-L10")
        )
        self.assertEqual(
            root_entity["semanticId"]["keys"][0]["value"],
            "https://admin-shell.io/idta/HierarchicalStructures/EntryNode/1/0",
        )
        self.assertEqual(
            child_entity["semanticId"]["keys"][0]["value"],
            "https://admin-shell.io/idta/HierarchicalStructures/Node/1/0",
        )
        self.api.force_authenticate(None)
        grants = self.api.get(
            "/api/aas/internal/grants/",
            HTTP_X_AAS_SERVICE_KEY="",
            HTTP_X_EXTERNAL_SUBJECT="festo-test",
        )
        self.assertEqual(grants.status_code, 403)
        with override_settings(AAS_INTERNAL_SERVICE_KEY="0123456789abcdef"):
            grants = self.api.get(
                "/api/aas/internal/grants/",
                HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
                HTTP_X_EXTERNAL_CLIENT_ID="festo-client-id",
            )
        identifiers = grants.json()["aas_ids"]
        self.assertIn(revision_id, identifiers)
        self.assertIn(f"{revision_id}/submodels/hierarchical-bom", identifiers)
        self.assertIn(f"{revision_id}/submodels/handover-metadata", identifiers)
        self.assertNotIn(first.publication.resource.logical_aas_id, identifiers)

        with override_settings(AAS_INTERNAL_SERVICE_KEY="0123456789abcdef"):
            subject_only = self.api.get(
                "/api/aas/internal/grants/",
                HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
                HTTP_X_EXTERNAL_SUBJECT="festo-test",
            )
            wrong_client = self.api.get(
                "/api/aas/internal/grants/",
                HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
                HTTP_X_EXTERNAL_CLIENT_ID="different-client",
            )
        self.assertEqual(subject_only.json(), {"aas_ids": []})
        self.assertEqual(wrong_client.json(), {"aas_ids": []})

        with override_settings(
            AAS_EXTERNAL_API_ENABLED=False,
            AAS_INTERNAL_SERVICE_KEY="0123456789abcdef",
        ):
            disabled = self.api.get(
                "/api/aas/internal/grants/",
                HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
                HTTP_X_EXTERNAL_CLIENT_ID="festo-client-id",
            )
        self.assertEqual(disabled.json(), {"aas_ids": []})

        certificate_client = ExternalAASClient.objects.create(
            name="Festo mTLS",
            auth0_subject="festo-mtls-test",
            certificate_fingerprint="a" * 64,
            allowed_operations=["read"],
        )
        ExternalAASGrant.objects.create(
            client=certificate_client,
            resource=first.publication.resource,
            publication=first.publication,
            created_by=self.user,
        )
        with override_settings(AAS_INTERNAL_SERVICE_KEY="0123456789abcdef"):
            certificate_grants = self.api.get(
                "/api/aas/internal/grants/",
                HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
                HTTP_X_EXTERNAL_CERTIFICATE_FINGERPRINT=":".join(["AA"] * 32),
            )
        self.assertIn(revision_id, certificate_grants.json()["aas_ids"])

    @override_settings(FESTO_GRANT_MIN_EXPIRES_AT="not-a-date")
    def test_festo_confirmation_rejects_an_invalid_minimum_grant_expiry(self):
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Servo drive",
            part_number="PN-100",
            level=1,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        handover, token, _ = create_festo_preview(self.project, self.user)

        with self.assertRaisesMessage(ValidationError, "ISO 8601"):
            confirm_festo_handover(
                handover.id,
                token,
                self.user,
                repository=FakeRepository(),
                artifact_store=FakeArtifactStore(),
            )

        self.assertFalse(
            AASPublication.objects.filter(resource__festo_handover=handover).exists()
        )

    def test_festo_confirmation_rejects_a_changed_bom(self):
        row = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Servo drive",
            part_number="PN-100",
            level=1,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        handover, token, _ = create_festo_preview(self.project, self.user)
        row.custom_fields = {**row.custom_fields, "quantity": 3}
        row.save(update_fields=["custom_fields", "updated_at"])
        with self.assertRaisesMessage(ValidationError, "The BOM changed after preview"):
            confirm_festo_handover(
                handover.id,
                token,
                self.user,
                repository=FakeRepository(),
                artifact_store=FakeArtifactStore(),
            )
        self.assertFalse(AASPublication.objects.filter(resource__festo_handover=handover).exists())

    def test_festo_publication_failure_records_failure_without_exposing_project(self):
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Servo drive",
            part_number="PN-100",
            level=1,
            sort_order=1,
            custom_fields={"supplier": "Festo", "quantity": 2},
        )
        handover, token, _ = create_festo_preview(self.project, self.user)
        with self.assertRaises(ConnectionError):
            confirm_festo_handover(
                handover.id,
                token,
                self.user,
                repository=FailingRepository(),
                artifact_store=FakeArtifactStore(),
            )
        handover.refresh_from_db()
        self.assertEqual(handover.status, handover.Status.FAILED)
        self.assertIsNone(handover.publication)
        self.assertFalse(ExternalAASGrant.objects.filter(resource__festo_handover=handover).exists())

    @override_settings(
        AAS_ADVANCED_EDITOR_ENABLED=True,
        AAS_EDITOR_BASE_URL="https://staging.aas-admin.sharemundolinks.com",
        AAS_EDITOR_SESSION_KEY="editor-session-key-with-at-least-32-bytes",
        AAS_EDITOR_LAUNCH_TTL_SECONDS=60,
    )
    def test_company_owner_can_create_scoped_editor_launch(self):
        self.api.force_authenticate(self.user)
        response = self.api.post("/api/aas/editor-session/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["editor_url"],
            "https://staging.aas-admin.sharemundolinks.com/editor/session",
        )
        claims = jwt.decode(
            response.json()["launch_token"],
            "editor-session-key-with-at-least-32-bytes",
            algorithms=["HS256"],
            audience="sharemundo-aas-editor",
        )
        self.assertEqual(claims["sub"], str(self.user.id))
        self.assertEqual(claims["company_id"], str(self.company.id))
        self.assertEqual(claims["purpose"], "aas-editor-launch")

    @override_settings(
        AAS_ADVANCED_EDITOR_ENABLED=True,
        AAS_EDITOR_BASE_URL="https://staging.aas-admin.sharemundolinks.com",
        AAS_EDITOR_SESSION_KEY="editor-session-key-with-at-least-32-bytes",
    )
    def test_user_without_aas_permission_cannot_create_editor_launch(self):
        member = User.objects.create_user(
            email="aas-member@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Member",
            user_type="company_user",
            company=self.company,
        )
        self.api.force_authenticate(member)

        response = self.api.post("/api/aas/editor-session/")

        self.assertEqual(response.status_code, 403)

    @override_settings(
        AAS_ADVANCED_EDITOR_ENABLED=True,
        AAS_EDITOR_BASE_URL="https://staging.aas-admin.sharemundolinks.com",
        AAS_EDITOR_SESSION_KEY="editor-session-key-with-at-least-32-bytes",
    )
    def test_non_admin_with_aas_view_permission_can_create_editor_launch(self):
        member = User.objects.create_user(
            email="aas-viewer@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Viewer",
            user_type="company_user",
            company=self.company,
        )
        viewer_role = Role.objects.create(
            name="AAS Viewer",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=viewer_role,
            permission=self.aas_view_permission,
        )
        UserRole.objects.create(
            user=member,
            role=viewer_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.api.force_authenticate(member)

        response = self.api.post("/api/aas/editor-session/")

        self.assertEqual(response.status_code, 200)

    def test_view_permission_cannot_publish(self):
        member = User.objects.create_user(
            email="aas-read-only@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Read Only",
            user_type="company_user",
            company=self.company,
        )
        viewer_role = Role.objects.create(
            name="AAS Read Only",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=viewer_role,
            permission=self.aas_view_permission,
        )
        UserRole.objects.create(
            user=member,
            role=viewer_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.api.force_authenticate(member)

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.post(
                f"/api/aas/company-products/{self.product.id}/publish/"
            )

        self.assertEqual(response.status_code, 403)

    @override_settings(AAS_ADVANCED_EDITOR_ENABLED=True)
    def test_view_permission_reports_read_only_product_capabilities(self):
        member = User.objects.create_user(
            email="aas-product-viewer@example.com",
            password="test-password",
            first_name="AAS",
            last_name="Product Viewer",
            user_type="company_user",
            company=self.company,
        )
        viewer_role = Role.objects.create(
            name="AAS Product Viewer",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=viewer_role,
            permission=self.aas_view_permission,
        )
        UserRole.objects.create(
            user=member,
            role=viewer_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.api.force_authenticate(member)

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(
                f"/api/aas/company-products/{self.product.id}/"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["features"]["can_write"])
        self.assertTrue(response.data["features"]["advanced_editor"])

    @override_settings(
        AAS_ADVANCED_EDITOR_ENABLED=True,
        AAS_INTERNAL_SERVICE_KEY="0123456789abcdef",
    )
    def test_editor_grants_include_only_current_company_publications(self):
        identity = get_or_create_product_identity(self.product)
        publication = publish_resource(
            identity.id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=FakeArtifactStore(),
        )
        self.api.force_authenticate(None)

        denied = self.api.get(
            "/api/aas/internal/editor-grants/",
            HTTP_X_AAS_EDITOR_USER_ID=str(self.user.id),
            HTTP_X_AAS_EDITOR_COMPANY_ID=str(self.company.id),
        )
        self.assertEqual(denied.status_code, 403)

        response = self.api.get(
            "/api/aas/internal/editor-grants/",
            HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
            HTTP_X_AAS_EDITOR_USER_ID=str(self.user.id),
            HTTP_X_AAS_EDITOR_COMPANY_ID=str(self.company.id),
        )

        self.assertEqual(response.status_code, 200)
        revision_id = f"{identity.logical_aas_id}/revisions/{publication.revision}"
        self.assertEqual(
            response.json()["aas_ids"],
            sorted(
                [
                    revision_id,
                    f"{revision_id}/submodels/nameplate",
                    f"{revision_id}/submodels/technical-data",
                ]
            ),
        )

    @staticmethod
    def _maintenance_submodel(version="1", *, identifier=None, id_short="Maintenance"):
        return {
            "modelType": "Submodel",
            "id": identifier or "https://example.com/idta/maintenance/1/0",
            "idShort": id_short,
            "administration": {"version": version},
            "semanticId": {
                "type": "ExternalReference",
                "keys": [
                    {
                        "type": "GlobalReference",
                        "value": "https://example.com/idta/maintenance/1/0/Submodel",
                    }
                ],
            },
            "submodelElements": [
                {
                    "modelType": "Property",
                    "idShort": "ProductName",
                    "valueType": "xs:string",
                    "value": "",
                },
                {
                    "modelType": "Property",
                    "idShort": "MaintenanceIntervalDays",
                    "valueType": "xs:integer",
                    "value": "",
                    "qualifiers": [
                        {
                            "type": "SMT/Cardinality",
                            "valueType": "xs:string",
                            "value": "One",
                        }
                    ],
                },
            ],
        }

    def _import_submodel_template(self, submodel=None, file_name="maintenance.json"):
        payload = json.dumps(submodel or self._maintenance_submodel()).encode()
        upload = SimpleUploadedFile(file_name, payload, content_type="application/json")
        return self.api.post(
            "/api/aas/submodel-templates/import/",
            {"file": upload, "profile_keys": json.dumps(["idta-product"])},
            format="multipart",
        )

    def _assign_submodel_template(self, template_id):
        profile = self.product.aas_profile
        template = AASSubmodelTemplate.objects.get(id=template_id)
        return ProductAASSubmodelAssignment.objects.create(
            profile=profile,
            template=template,
            template_identifier=template.template_identifier,
            assigned_by=self.user,
        )

    def test_submodel_template_json_import_validate_and_activate(self):
        self.api.force_authenticate(self.user)
        identity = get_or_create_product_identity(self.product)
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])

        imported = self._import_submodel_template()

        self.assertEqual(imported.status_code, 201)
        self.assertEqual(len(imported.data), 1)
        self.assertEqual(imported.data[0]["status"], "draft")
        self.assertEqual(imported.data[0]["field_count"], 2)
        mappings = imported.data[0]["field_mappings"]
        self.assertEqual(mappings[0]["source"], "product_name")
        self.assertEqual(mappings[1]["source"], "profile_field")
        self.assertTrue(mappings[1]["required"])

        template_id = imported.data[0]["id"]
        validated = self.api.post(
            f"/api/aas/submodel-templates/{template_id}/validate/"
        )
        activated = self.api.post(
            f"/api/aas/submodel-templates/{template_id}/activate/"
        )

        self.assertEqual(validated.status_code, 200)
        self.assertTrue(validated.data["valid"])
        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.data["template"]["status"], "active")
        self.assertEqual(activated.data["out_of_date_resources"], 0)
        identity.refresh_from_db()
        self.assertFalse(identity.out_of_date)

    def test_active_submodel_is_available_but_not_published_until_assigned(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        template = imported.data[0]
        self.api.post(
            f"/api/aas/submodel-templates/{template['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.get(
                f"/api/aas/company-products/{self.product.id}/profile/"
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["available_submodels"][0]["assigned"])
        self.assertEqual(response.data["registered_submodels"], [])
        document = ProductAASMapper().map(
            self.product,
            "https://aas.example/products/servo-drive",
        )
        self.assertNotIn(
            "Maintenance",
            [submodel["idShort"] for submodel in document.submodels],
        )

    def test_manufactured_product_can_assign_and_remove_active_submodel(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        template = imported.data[0]
        self.api.post(
            f"/api/aas/submodel-templates/{template['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        identity = get_or_create_product_identity(self.product)
        identity.out_of_date = False
        identity.save(update_fields=["out_of_date", "updated_at"])

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            assigned = self.api.patch(
                f"/api/aas/company-products/{self.product.id}/profile/",
                {"assigned_submodel_template_ids": [template["id"]]},
                format="json",
            )
            removed = self.api.patch(
                f"/api/aas/company-products/{self.product.id}/profile/",
                {"assigned_submodel_template_ids": []},
                format="json",
            )

        self.assertEqual(assigned.status_code, 200)
        self.assertTrue(assigned.data["available_submodels"][0]["assigned"])
        self.assertEqual(len(assigned.data["registered_submodels"]), 1)
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(removed.data["available_submodels"][0]["assigned"])
        self.assertEqual(removed.data["registered_submodels"], [])
        identity.refresh_from_db()
        self.assertTrue(identity.out_of_date)

    def test_populated_multilanguage_property_is_imported_as_a_field(self):
        self.api.force_authenticate(self.user)
        submodel = self._maintenance_submodel()
        submodel["submodelElements"].append(
            {
                "modelType": "MultiLanguageProperty",
                "idShort": "MaintenanceInstructions",
                "value": [
                    {"language": "en", "text": "Inspect every month."},
                    {"language": "de", "text": "Monatlich prüfen."},
                ],
            }
        )

        imported = self._import_submodel_template(submodel)

        self.assertEqual(imported.status_code, 201)
        self.assertEqual(imported.data[0]["field_count"], 3)
        multilingual = next(
            mapping
            for mapping in imported.data[0]["field_mappings"]
            if mapping["element_id_short"] == "MaintenanceInstructions"
        )
        self.assertEqual(multilingual["model_type"], "MultiLanguageProperty")
        self.assertEqual(multilingual["source"], "profile_field")
        mappings = imported.data[0]["field_mappings"]
        for mapping in mappings:
            if mapping["element_id_short"] == "MaintenanceInstructions":
                mapping["source"] = "template_default"
                mapping["source_key"] = ""
        updated = self.api.patch(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/",
            {"field_mappings": mappings},
            format="json",
        )
        activated = self.api.post(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/activate/"
        )
        self._assign_submodel_template(imported.data[0]["id"])
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(activated.status_code, 200)
        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            profile = self.api.get(
                f"/api/aas/company-products/{self.product.id}/profile/"
            )
        self.assertEqual(profile.status_code, 200)
        instructions = next(
            field
            for field in profile.data["registered_submodels"][0]["fields"]
            if field["element_id_short"] == "MaintenanceInstructions"
        )
        self.assertEqual(
            instructions["value"],
            {"en": "Inspect every month.", "de": "Monatlich prüfen."},
        )

    def test_empty_profile_fields_do_not_publish_template_example_values(self):
        self.api.force_authenticate(self.user)
        submodel = self._maintenance_submodel()
        submodel["submodelElements"].extend(
            [
                {
                    "modelType": "MultiLanguageProperty",
                    "idShort": "MaintenanceInstructions",
                    "value": [
                        {"language": "en", "text": "Example instruction."},
                    ],
                },
                {
                    "modelType": "Range",
                    "idShort": "OperatingTemperature",
                    "valueType": "xs:decimal",
                    "min": "10",
                    "max": "20",
                },
            ]
        )
        imported = self._import_submodel_template(submodel)
        self.api.post(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/activate/"
        )
        self._assign_submodel_template(imported.data[0]["id"])

        document = ProductAASMapper().map(
            self.product,
            "https://aas.example/products/servo-drive",
        )
        maintenance = next(
            item for item in document.submodels if item["idShort"] == "Maintenance"
        )
        elements = {
            item["idShort"]: item for item in maintenance["submodelElements"]
        }

        self.assertNotIn("value", elements["MaintenanceInstructions"])
        self.assertNotIn("value", elements["MaintenanceIntervalDays"])
        self.assertNotIn("min", elements["OperatingTemperature"])
        self.assertNotIn("max", elements["OperatingTemperature"])
        parse_aas_environment(document.as_environment())

    @override_settings(
        AAS_ADVANCED_EDITOR_ENABLED=True,
        AAS_INTERNAL_SERVICE_KEY="0123456789abcdef",
    )
    def test_registered_submodel_values_are_rendered_for_manufactured_product(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        template = imported.data[0]
        self.api.post(
            f"/api/aas/submodel-templates/{template['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            updated = self.api.patch(
                f"/api/aas/company-products/{self.product.id}/profile/",
                {
                    "assigned_submodel_template_ids": [template["id"]],
                    "submodel_values": {
                        template["template_identifier"]: {
                            "MaintenanceIntervalDays": 30,
                        }
                    }
                },
                format="json",
            )

        self.assertEqual(updated.status_code, 200)
        self.product.refresh_from_db()
        self.product.aas_profile.refresh_from_db()
        document = ProductAASMapper().map(
            self.product,
            "https://aas.example/products/servo-drive",
        )
        maintenance = next(
            item for item in document.submodels if item["idShort"] == "Maintenance"
        )
        values = {
            item["idShort"]: item.get("value")
            for item in maintenance["submodelElements"]
        }
        self.assertEqual(values["ProductName"], "Servo drive")
        self.assertEqual(values["MaintenanceIntervalDays"], "30")
        self.assertTrue(document.as_environment()["assetAdministrationShells"][0]["submodels"][-1]["keys"][0]["value"].endswith(maintenance["id"].rsplit("/", 1)[-1]))
        self.assertFalse(
            any(
                message.code == "registered_submodel_required_value_missing"
                for message in document.validation
            )
        )
        identity = get_or_create_product_identity(self.product)
        publication = publish_resource(
            identity.id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=FakeArtifactStore(),
        )
        maintenance_id = next(
            identifier
            for identifier in publication.published_identifiers
            if "/submodels/maintenance-" in identifier
        )
        self.api.force_authenticate(None)
        grants = self.api.get(
            "/api/aas/internal/editor-grants/",
            HTTP_X_AAS_SERVICE_KEY="0123456789abcdef",
            HTTP_X_AAS_EDITOR_USER_ID=str(self.user.id),
            HTTP_X_AAS_EDITOR_COMPANY_ID=str(self.company.id),
        )
        self.assertEqual(grants.status_code, 200)
        self.assertIn(maintenance_id, grants.data["aas_ids"])

    def test_purchased_product_cannot_edit_registered_submodel_values(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        self.api.post(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/activate/"
        )
        self._assign_submodel_template(imported.data[0]["id"])

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.patch(
                f"/api/aas/company-products/{self.product.id}/profile/",
                {
                    "submodel_values": {
                        imported.data[0]["template_identifier"]: {
                            "MaintenanceIntervalDays": 15,
                        }
                    }
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("origin", response.data)

    def test_registered_submodel_values_enforce_template_value_types(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        template = imported.data[0]
        self.api.post(
            f"/api/aas/submodel-templates/{template['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        self._assign_submodel_template(template["id"])

        with patch(
            "aas_integration.views.AvailableProductPartPermission.has_object_permission",
            return_value=True,
        ):
            response = self.api.patch(
                f"/api/aas/company-products/{self.product.id}/profile/",
                {
                    "submodel_values": {
                        template["template_identifier"]: {
                            "MaintenanceIntervalDays": "every month",
                        }
                    }
                },
                format="json",
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("MaintenanceIntervalDays", str(response.data))

    def test_required_registered_submodel_value_blocks_publication_validation(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        self.api.post(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.save(update_fields=["origin", "updated_at"])
        self._assign_submodel_template(imported.data[0]["id"])

        document = ProductAASMapper().map(
            self.product,
            "https://aas.example/products/servo-drive",
        )

        self.assertTrue(
            any(
                message.code == "registered_submodel_required_value_missing"
                and message.path == "Maintenance.MaintenanceIntervalDays"
                for message in document.validation
            )
        )

    def test_empty_required_range_blocks_publication_validation(self):
        self.api.force_authenticate(self.user)
        submodel = self._maintenance_submodel()
        submodel["submodelElements"].append(
            {
                "modelType": "Range",
                "idShort": "OperatingTemperature",
                "valueType": "xs:decimal",
                "qualifiers": [
                    {
                        "type": "SMT/Cardinality",
                        "valueType": "xs:string",
                        "value": "One",
                    }
                ],
            }
        )
        imported = self._import_submodel_template(submodel)
        template = imported.data[0]
        self.api.post(
            f"/api/aas/submodel-templates/{template['id']}/activate/"
        )
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.standard_fields = {
            "registered_submodels": {
                template["template_identifier"]: {
                    "values": {
                        "OperatingTemperature": {"min": "", "max": ""}
                    }
                }
            }
        }
        profile.save(update_fields=["origin", "standard_fields", "updated_at"])
        self._assign_submodel_template(template["id"])

        document = ProductAASMapper().map(
            self.product,
            "https://aas.example/products/servo-drive",
        )

        self.assertTrue(
            any(
                message.code == "registered_submodel_required_value_missing"
                and message.path == "Maintenance.OperatingTemperature"
                for message in document.validation
            )
        )

    def test_new_template_version_deprecates_previous_active_version(self):
        self.api.force_authenticate(self.user)
        first = self._import_submodel_template(self._maintenance_submodel("1"))
        self.api.post(
            f"/api/aas/submodel-templates/{first.data[0]['id']}/activate/"
        )
        assignment = self._assign_submodel_template(first.data[0]["id"])
        second = self._import_submodel_template(self._maintenance_submodel("2"))
        activated = self.api.post(
            f"/api/aas/submodel-templates/{second.data[0]['id']}/activate/"
        )

        self.assertEqual(activated.status_code, 200)
        self.assertEqual(activated.data["deprecated_versions"], 1)
        self.assertEqual(activated.data["migrated_assignments"], 1)
        assignment.refresh_from_db()
        self.assertEqual(str(assignment.template_id), second.data[0]["id"])
        self.assertEqual(
            AASSubmodelTemplate.objects.get(id=first.data[0]["id"]).status,
            AASSubmodelTemplate.Status.DEPRECATED,
        )
        self.assertEqual(
            AASSubmodelTemplate.objects.get(id=second.data[0]["id"]).status,
            AASSubmodelTemplate.Status.ACTIVE,
        )

    def test_reserved_submodel_is_imported_as_invalid_and_cannot_activate(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template(
            self._maintenance_submodel(id_short="Nameplate")
        )

        self.assertEqual(imported.status_code, 201)
        self.assertEqual(imported.data[0]["status"], "invalid")
        activated = self.api.post(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/activate/"
        )
        self.assertEqual(activated.status_code, 400)
        self.assertFalse(activated.data["validation_report"]["valid"])

    def test_submodel_template_aasx_import_preserves_complete_definition(self):
        self.api.force_authenticate(self.user)
        submodel = self._maintenance_submodel()
        submodel["submodelElements"][1].pop("value")
        submodel["submodelElements"][1]["semanticId"] = {
            "type": "ExternalReference",
            "keys": [
                {
                    "type": "GlobalReference",
                    "value": "https://example.com/concepts/maintenance-interval",
                }
            ],
        }
        submodel["submodelElements"].append(
            {
                "modelType": "File",
                "idShort": "MaintenanceManual",
                "contentType": "application/pdf",
                "value": "/aasx/files/maintenance-manual.pdf",
            }
        )
        concept_description = {
            "modelType": "ConceptDescription",
            "id": "https://example.com/concepts/maintenance-interval",
            "idShort": "MaintenanceInterval",
        }
        shell_id = "https://example.com/aas/template-package"
        payload = create_aasx(
            {
                "assetAdministrationShells": [
                    {
                        "modelType": "AssetAdministrationShell",
                        "id": shell_id,
                        "idShort": "TemplatePackage",
                        "assetInformation": {
                            "assetKind": "Type",
                            "globalAssetId": "https://example.com/assets/template-package",
                        },
                        "submodels": [
                            {
                                "type": "ModelReference",
                                "keys": [
                                    {"type": "Submodel", "value": submodel["id"]}
                                ],
                            }
                        ],
                    }
                ],
                "submodels": [submodel],
                "conceptDescriptions": [concept_description],
            },
            supplementary_files=[
                {
                    "path": "/aasx/files/maintenance-manual.pdf",
                    "content_type": "application/pdf",
                    "content": b"test-maintenance-manual",
                }
            ],
        )
        upload = SimpleUploadedFile(
            "maintenance.aasx",
            payload,
            content_type="application/asset-administration-shell-package",
        )

        imported = self.api.post(
            "/api/aas/submodel-templates/import/",
            {"file": upload},
            format="multipart",
        )

        self.assertEqual(imported.status_code, 201)
        self.assertEqual(imported.data[0]["source_format"], "aasx")
        self.assertEqual(imported.data[0]["definition"]["idShort"], "Maintenance")
        self.assertEqual(imported.data[0]["concept_description_count"], 1)
        self.assertEqual(len(imported.data[0]["supplementary_files"]), 1)
        stored_file = AASSubmodelTemplateSupplementaryFile.objects.get(
            template_id=imported.data[0]["id"]
        )
        with stored_file.file.open("rb") as source:
            self.assertEqual(source.read(), b"test-maintenance-manual")
        template = AASSubmodelTemplate.objects.get(id=imported.data[0]["id"])
        template.status = AASSubmodelTemplate.Status.ACTIVE
        template.save(update_fields=["status", "updated_at"])
        profile = self.product.aas_profile
        profile.origin = ProductAASProfile.Origin.MANUFACTURED
        profile.standard_fields = {
            "registered_submodels": {
                template.template_identifier: {
                    "values": {"MaintenanceIntervalDays": 30}
                }
            }
        }
        profile.save(update_fields=["origin", "standard_fields", "updated_at"])
        self._assign_submodel_template(template.id)
        artifact_store = FakeArtifactStore()
        publication = publish_resource(
            get_or_create_product_identity(self.product).id,
            actor=self.user,
            repository=FakeRepository(),
            artifact_store=artifact_store,
        )
        aasx_payload = artifact_store.files[publication.aasx_artifact_key]
        environment, files = read_aasx_package(
            aasx_payload,
            max_entries=500,
            max_decompressed_bytes=10 * 1024 * 1024,
            max_compression_ratio=100,
        )
        self.assertEqual(len(environment["conceptDescriptions"]), 1)
        self.assertEqual(files[0]["path"], "/aasx/files/maintenance-manual.pdf")
        self.assertEqual(files[0]["content"], b"test-maintenance-manual")

    def test_official_submodel_catalog_is_pinned_and_import_is_idempotent(self):
        self.api.force_authenticate(self.user)
        catalog = self.api.get("/api/aas/submodel-catalog/")

        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(len(catalog.data["results"]), 4)
        self.assertTrue(
            all(item["status"] == "not_imported" for item in catalog.data["results"])
        )
        payload = json.dumps(self._maintenance_submodel()).encode()
        item = OfficialTemplate(
            key="test-official",
            name="Test official",
            id_short="Maintenance",
            template_identifier="https://example.com/idta/maintenance/1/0",
            version="1.0",
            source_path="published/test.json",
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        response = type(
            "OfficialResponse",
            (),
            {"content": payload, "raise_for_status": lambda self: None},
        )()
        with patch(
            "aas_integration.official_templates.OFFICIAL_TEMPLATES_BY_KEY",
            {item.key: item},
        ), patch(
            "aas_integration.official_templates.requests.get",
            return_value=response,
        ) as get_template:
            imported = self.api.post(
                f"/api/aas/submodel-catalog/{item.key}/import/"
            )
            repeated = self.api.post(
                f"/api/aas/submodel-catalog/{item.key}/import/"
            )

        self.assertEqual(imported.status_code, 201)
        self.assertTrue(imported.data["created"])
        self.assertEqual(repeated.status_code, 200)
        self.assertFalse(repeated.data["created"])
        self.assertEqual(get_template.call_count, 1)

    def test_submodel_templates_are_isolated_by_company(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        other_user = User.objects.create_user(
            email="other-aas-owner@example.com",
            password="test-password",
            first_name="Other",
            last_name="Owner",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            company_name="Other AAS Company",
            company_address="Street 2",
            post_code="20000",
            company_town="Paris",
            company_country="France",
            company_tell_no="-",
            company_mob_no="-",
            company_web_address="https://other.example.com",
            company_email_address="other-aas@example.com",
            company_tax_reference="OTHER-TAX",
            company_iban="OTHER",
            admin_user=other_user,
        )
        other_user.company = other_company
        other_user.save(update_fields=["company"])
        other_role = Role.objects.create(
            name="Other AAS Role",
            company=other_company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=other_role,
            permission=self.aas_full_access_permission,
        )
        UserRole.objects.create(
            user=other_user,
            role=other_role,
            company=other_company,
            assigned_by=other_user,
        )
        self.api.force_authenticate(other_user)

        listed = self.api.get("/api/aas/submodel-templates/")
        detail = self.api.get(
            f"/api/aas/submodel-templates/{imported.data[0]['id']}/"
        )

        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 0)
        self.assertEqual(detail.status_code, 404)

    def test_view_permission_can_list_templates_but_cannot_import(self):
        self.api.force_authenticate(self.user)
        imported = self._import_submodel_template()
        member = User.objects.create_user(
            email="aas-template-viewer@example.com",
            password="test-password",
            first_name="Template",
            last_name="Viewer",
            user_type="company_user",
            company=self.company,
        )
        viewer_role = Role.objects.create(
            name="AAS Template Viewer",
            company=self.company,
            type="solution",
            scope="company",
        )
        RolePermission.objects.create(
            role=viewer_role,
            permission=self.aas_view_permission,
        )
        UserRole.objects.create(
            user=member,
            role=viewer_role,
            company=self.company,
            assigned_by=self.user,
        )
        self.api.force_authenticate(member)

        listed = self.api.get("/api/aas/submodel-templates/")
        upload = SimpleUploadedFile(
            "another.json",
            json.dumps(
                self._maintenance_submodel(
                    identifier="https://example.com/idta/another/1/0"
                )
            ).encode(),
            content_type="application/json",
        )
        denied = self.api.post(
            "/api/aas/submodel-templates/import/",
            {"file": upload},
            format="multipart",
        )

        self.assertEqual(listed.status_code, 200)
        self.assertFalse(listed.data["features"]["can_write"])
        self.assertEqual(listed.data["results"][0]["id"], imported.data[0]["id"])
        self.assertEqual(denied.status_code, 403)
