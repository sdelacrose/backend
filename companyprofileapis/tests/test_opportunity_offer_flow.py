import tempfile
import zipfile
from importlib import import_module
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.apps import apps
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework import serializers
from rest_framework.test import APIRequestFactory, force_authenticate

from companyprofileapis.models import OfferLineItem, Opportunity
from companyprofileapis.permissions import OpportunityOffersPermission
from companyprofileapis.serializers import OpportunitySerializer
from companyprofileapis.services.offer_pdf import (
    OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
    build_offer_pdf_context,
    create_or_replace_offer_pdf,
    get_offer_pdf_documents,
    get_stored_offer_pdf_document,
)
from companyprofileapis.services.offer_send import (
    OFFER_INCLUDE_MODE_OFFER_ONLY,
    OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
    _package_storage_path,
    create_offer_send_package,
)
from companyprofileapis.services.opportunity_lifecycle import (
    OpportunityLifecycleAction,
    OpportunityLifecycleError,
    target_status_for_action,
)
from companyprofileapis.services.opportunity_history import (
    OpportunityHistoryReason,
)
from companyprofileapis.views.opportunity import OpportunityViewSet
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import (
    AvailableProductPart,
    ContactCompany,
    ContactUser,
    Document,
    Folder,
)


def create_company_user(email="offer-owner@example.com"):
    user = User.objects.create_user(
        email=email,
        password="password",
        first_name="Offer",
        last_name="Owner",
        user_type="company_user",
    )
    company = CompanyProfile.objects.create(
        admin_user=user,
        company_name="Offer Company",
        company_address="Issuer Street 1",
        company_town="Issuer Town",
        company_tell_no="+33123456789",
        company_mob_no="+33987654321",
        company_web_address="https://issuer.example.com",
        company_email_address=email,
        company_tax_reference="ISSUER-VAT",
        company_registration_no="REG-42",
        company_iban="FR761234",
        currency="€",
    )
    user.company = company
    user.save(update_fields=["company"])
    CompanyConfig.objects.create(
        company_profile=company,
        solution="Energy Consultant Solution",
        offer_validity_periods=["1:month", "2:week"],
    )
    return user, company


def create_company_customer(company, email="customer@example.com"):
    return ContactCompany.objects.create(
        company_profile=company,
        company_name="Customer Company",
        company_address="Customer Street 2",
        company_postcode="75001",
        company_town="Paris",
        company_country="France",
        company_tel_no="+33111111111",
        contact_email=email,
    )


def serializer_request(user):
    request = APIRequestFactory().post("/api/company/opportunity/")
    request.user = user
    return request


class NotificationPreferenceStubMixin:
    def setUp(self):
        super().setUp()
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.addCleanup(self.notification_patcher.stop)


class OpportunityLifecycleTests(NotificationPreferenceStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user, self.company = create_company_user()
        self.customer = create_company_customer(self.company)
        self.request = serializer_request(self.user)

    def _create_direct_offer(self, **overrides):
        legacy_value = overrides.get("value")
        if legacy_value is not None and "line_items" not in overrides:
            overrides.pop("value")
            overrides["line_items"] = [
                {
                    "item_name": "Manual service",
                    "rate": str(legacy_value),
                    "quantity": "1.0000",
                    "vat_rate": "0.00",
                }
            ]
        payload = {
            "title": "Direct offer",
            "customer_company": self.customer.id,
            "offer_expiry_period": "1:month",
            "line_items": [
                {
                    "item_name": "Manual service",
                    "rate": "0.00",
                    "quantity": "1.0000",
                    "vat_rate": "12.00",
                }
            ],
            **overrides,
        }
        serializer = OpportunitySerializer(
            data=payload,
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _update(self, opportunity, data, lifecycle_action=None):
        context = {"request": self.request}
        if lifecycle_action:
            context["lifecycle_action"] = lifecycle_action
        serializer = OpportunitySerializer(
            opportunity,
            data=data,
            partial=True,
            context=context,
        )
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def test_direct_offer_is_draft_with_off_reference_and_created_by(self):
        with patch(
            "companyprofileapis.serializers.opportunity.timezone.localdate",
            return_value=date(2026, 9, 8),
        ):
            offer = self._create_direct_offer(sent_date=date(2026, 9, 8))

        self.assertEqual(offer.status, Opportunity.Status.DRAFT)
        self.assertTrue(offer.reference.startswith("OFF-"))
        self.assertEqual(offer.created_by, self.user)
        self.assertEqual(offer.sent_date, date(2026, 9, 8))
        self.assertEqual(offer.offer_created_date, date(2026, 9, 8))
        self.assertEqual(offer.offer_expiry_period, "1:month")
        self.assertEqual(offer.offer_expiry_date, date(2026, 10, 8))
        self.assertEqual(offer.value, Decimal("0.00"))
        history = offer.history.get()
        self.assertEqual(
            history.history_change_reason,
            OpportunityHistoryReason.OFFER_CREATED,
        )
        self.assertEqual(history.history_user_id, self.user.id)

    def test_direct_offer_requires_configured_expiry_period(self):
        base_payload = {
            "title": "Direct offer",
            "customer_company": self.customer.id,
            "line_items": [
                {
                    "item_name": "Service",
                    "rate": "10.00",
                    "quantity": "1.0000",
                    "vat_rate": "12.00",
                }
            ],
        }

        missing = OpportunitySerializer(
            data=base_payload,
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )
        self.assertFalse(missing.is_valid())
        self.assertEqual(
            missing.errors["offer_expiry_period"]["code"],
            "required",
        )

        unconfigured = OpportunitySerializer(
            data={**base_payload, "offer_expiry_period": "3:month"},
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )
        self.assertFalse(unconfigured.is_valid())
        self.assertEqual(
            unconfigured.errors["offer_expiry_period"]["code"],
            "not_configured",
        )

    def test_direct_offer_persists_line_items_and_calculates_inclusive_value(self):
        product = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Solar panel",
        )

        offer = self._create_direct_offer(
            line_items=[
                {
                    "available_product": product.id,
                    "item_name": "Solar panel",
                    "rate": "100.00",
                    "quantity": "2.0000",
                    "vat_rate": "12.00",
                },
                {
                    "item_name": "Installation",
                    "rate": "50.00",
                    "quantity": "1.0000",
                    "vat_rate": "5.00",
                },
            ],
            value=999999,
        )

        self.assertEqual(offer.value, Decimal("276.50"))
        self.assertEqual(offer.offer_line_items.count(), 2)
        first_item, second_item = offer.offer_line_items.order_by("line_number")
        self.assertEqual(first_item.total, Decimal("200.00"))
        self.assertEqual(second_item.total, Decimal("50.00"))

    def test_direct_offer_requires_at_least_one_line_item(self):
        serializer = OpportunitySerializer(
            data={
                "title": "Empty offer",
                "customer_company": self.customer.id,
                "offer_expiry_period": "1:month",
                "line_items": [],
            },
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("line_items", serializer.errors)

    def test_convert_to_offer_requires_items_and_calculates_value(self):
        opportunity = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="Opportunity",
            status=Opportunity.Status.OPEN,
            reference="OPP-202609-00001",
            customer_company=self.customer,
        )

        with self.assertRaises(serializers.ValidationError):
            self._update(
                opportunity,
                {},
                OpportunityLifecycleAction.CONVERT_TO_OFFER,
            )

        with patch(
            "companyprofileapis.serializers.opportunity.timezone.localdate",
            return_value=date(2026, 1, 31),
        ):
            offer = self._update(
                opportunity,
                {
                    "line_items": [
                        {
                            "item_name": "Converted service",
                            "rate": "80.00",
                            "quantity": "2.0000",
                            "vat_rate": "12.00",
                        }
                    ],
                    "offer_expiry_period": "1:month",
                },
                OpportunityLifecycleAction.CONVERT_TO_OFFER,
            )

        self.assertEqual(offer.status, Opportunity.Status.CONVERTED)
        self.assertTrue(offer.reference.startswith("OFF-"))
        self.assertEqual(offer.value, Decimal("179.20"))
        self.assertEqual(offer.offer_created_date, date(2026, 1, 31))
        self.assertEqual(offer.offer_expiry_date, date(2026, 2, 28))
        self.assertEqual(
            offer.history.first().history_change_reason,
            OpportunityHistoryReason.OPPORTUNITY_CONVERTED_TO_OFFER,
        )

    def test_offer_expiry_edit_recalculates_from_original_offer_date(self):
        with patch(
            "companyprofileapis.serializers.opportunity.timezone.localdate",
            return_value=date(2024, 2, 29),
        ):
            offer = self._create_direct_offer()
        offer.status = Opportunity.Status.ISSUED
        offer.save(update_fields=["status"])

        offer = self._update(offer, {"offer_expiry_period": "2:week"})

        self.assertEqual(offer.offer_created_date, date(2024, 2, 29))
        self.assertEqual(offer.offer_expiry_period, "2:week")
        self.assertEqual(offer.offer_expiry_date, date(2024, 3, 14))

    def test_legacy_offer_edit_does_not_require_expiry_period(self):
        offer = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="Legacy offer",
            status=Opportunity.Status.ISSUED,
            reference="OFF-202509-00001",
            customer_company=self.customer,
            offer_created_date=date(2025, 9, 1),
        )

        offer = self._update(offer, {"sent_date": date(2025, 9, 2)})

        self.assertIsNone(offer.offer_expiry_period)
        self.assertIsNone(offer.offer_expiry_date)

    def test_offer_item_update_preserves_on_omission_and_replaces_atomically(self):
        offer = self._create_direct_offer(value=100)
        original_item_id = offer.offer_line_items.get().id

        offer = self._update(offer, {"title": "Renamed"})
        self.assertEqual(offer.offer_line_items.get().id, original_item_id)

        offer = self._update(
            offer,
            {
                "line_items": [
                    {
                        "item_name": "Replacement",
                        "rate": "10.00",
                        "quantity": "3.0000",
                        "vat_rate": "20.00",
                    }
                ]
            },
        )
        self.assertEqual(offer.value, Decimal("36.00"))
        self.assertNotEqual(offer.offer_line_items.get().id, original_item_id)

        with self.assertRaises(serializers.ValidationError):
            self._update(offer, {"line_items": []})

    def test_open_opportunity_cannot_receive_line_items_without_conversion(self):
        opportunity = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="Open opportunity",
            status=Opportunity.Status.OPEN,
            reference="OPP-202609-00002",
            customer_company=self.customer,
        )

        with self.assertRaises(serializers.ValidationError):
            self._update(
                opportunity,
                {
                    "line_items": [
                        {
                            "item_name": "Premature",
                            "rate": "10.00",
                            "quantity": "1.0000",
                            "vat_rate": "12.00",
                        }
                    ]
                },
            )

    def test_available_product_must_belong_to_offer_company(self):
        _, other_company = create_company_user("product-owner@example.com")
        product = AvailableProductPart.objects.create(
            company_profile=other_company,
            name="Other company's product",
        )
        serializer = OpportunitySerializer(
            data={
                "title": "Cross-tenant product",
                "customer_company": self.customer.id,
                "offer_expiry_period": "1:month",
                "line_items": [
                    {
                        "available_product": product.id,
                        "rate": "10.00",
                        "quantity": "1.0000",
                        "vat_rate": "12.00",
                    }
                ],
            },
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("line_items", serializer.errors)

    def test_direct_offer_rejects_invalid_line_item_amounts(self):
        for field, invalid_value in (
            ("rate", "-1.00"),
            ("quantity", "-1.0000"),
            ("vat_rate", "101.00"),
        ):
            with self.subTest(field=field, value=invalid_value):
                item = {
                    "item_name": "Invalid item",
                    "rate": "10.00",
                    "quantity": "1.0000",
                    "vat_rate": "12.00",
                }
                item[field] = invalid_value
                serializer = OpportunitySerializer(
                    data={
                        "title": "Invalid offer",
                        "customer_company": self.customer.id,
                        "offer_expiry_period": "1:month",
                        "line_items": [item],
                    },
                    context={
                        "request": self.request,
                        "lifecycle_action": (
                            OpportunityLifecycleAction.CREATE_DIRECT_OFFER
                        ),
                    },
                )
                self.assertFalse(serializer.is_valid())
                self.assertIn("line_items", serializer.errors)

    def test_client_cannot_set_status_or_created_by(self):
        serializer = OpportunitySerializer(
            data={
                "title": "Manipulated offer",
                "customer_company": self.customer.id,
                "status": Opportunity.Status.ISSUED,
                "created_by": self.user.id,
            },
            context={"request": self.request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)

    def test_offer_transitions_and_editing_matrix(self):
        offer = self._create_direct_offer(value=100)
        offer = self._update(offer, {"title": "Edited", "sent_date": date(2026, 9, 7)})
        self.assertEqual(offer.title, "Edited")

        original_creator = offer.created_by
        offer = self._update(
            offer,
            {},
            OpportunityLifecycleAction.CONVERT_TO_PROJECT,
        )
        self.assertEqual(offer.status, Opportunity.Status.CONVERTED_TO_PROJECT)
        offer = self._update(offer, {"sent_date": date(2026, 9, 8)})
        self.assertEqual(offer.sent_date, date(2026, 9, 8))
        self.assertEqual(offer.created_by, original_creator)

        with self.assertRaises(serializers.ValidationError):
            self._update(offer, {"title": "Not allowed"})

    def test_closed_and_declined_records_are_terminal(self):
        for lifecycle_action in (
            OpportunityLifecycleAction.CLOSE,
            OpportunityLifecycleAction.DECLINE_OFFER,
        ):
            with self.subTest(action=lifecycle_action):
                offer = self._create_direct_offer(
                    title=f"Offer for {lifecycle_action}",
                    value=100,
                )
                payload = (
                    {"close_reason": "Lost interest"}
                    if lifecycle_action == OpportunityLifecycleAction.CLOSE
                    else {}
                )
                offer = self._update(offer, payload, lifecycle_action)
                with self.assertRaises(serializers.ValidationError):
                    self._update(offer, {"sent_date": date(2026, 9, 8)})
                with self.assertRaises(OpportunityLifecycleError):
                    target_status_for_action(
                        offer.status,
                        OpportunityLifecycleAction.SEND,
                    )

    def test_company_customer_must_belong_to_offer_owner(self):
        _, other_company = create_company_user("other-owner@example.com")
        other_customer = create_company_customer(
            other_company,
            "other-customer@example.com",
        )
        serializer = OpportunitySerializer(
            data={
                "title": "Cross-tenant offer",
                "customer_company": other_customer.id,
                "offer_expiry_period": "1:month",
                "line_items": [
                    {
                        "item_name": "Service",
                        "rate": "10.00",
                        "quantity": "1.0000",
                        "vat_rate": "12.00",
                    }
                ],
            },
            context={
                "request": self.request,
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("does not belong", str(serializer.errors))

    def test_freelancer_can_create_direct_offer(self):
        freelancer = User.objects.create_user(
            email="freelancer-offer@example.com",
            password="password",
            first_name="Free",
            last_name="Lancer",
            user_type="freelancer",
        )
        customer = ContactCompany.objects.create(
            user=freelancer,
            company_name="Freelancer Customer",
            contact_email="freelancer-customer@example.com",
        )
        CompanyConfig.objects.create(
            user=freelancer,
            solution="Energy Consultant Solution",
            offer_validity_periods=["2:week"],
        )
        serializer = OpportunitySerializer(
            data={
                "title": "Freelancer offer",
                "customer_company": customer.id,
                "offer_expiry_period": "2:week",
                "line_items": [
                    {
                        "item_name": "Freelancer service",
                        "rate": "25.00",
                        "quantity": "1.0000",
                        "vat_rate": "12.00",
                    }
                ],
            },
            context={
                "request": serializer_request(freelancer),
                "lifecycle_action": OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            },
        )

        serializer.is_valid(raise_exception=True)
        offer = serializer.save()
        self.assertEqual(offer.freelancer, freelancer)
        self.assertIsNone(offer.company_profile)
        self.assertTrue(offer.reference.startswith("OFF-"))


class OfferEndpointAtomicityTests(NotificationPreferenceStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user, self.company = create_company_user("endpoint-owner@example.com")
        self.customer = create_company_customer(
            self.company,
            "endpoint-customer@example.com",
        )
        self.factory = APIRequestFactory()

    def test_list_omits_offer_line_items(self):
        offer = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="List offer",
            status=Opportunity.Status.DRAFT,
            reference="OFF-202609-00001",
            customer_company=self.customer,
            value=Decimal("112.00"),
        )
        OfferLineItem.objects.create(
            opportunity=offer,
            line_number=1,
            item_name="Service",
            rate=Decimal("100.00"),
            quantity=Decimal("1.0000"),
            vat_rate=Decimal("12.00"),
            total=Decimal("100.00"),
        )
        request = self.factory.get("/api/company/opportunity/")
        force_authenticate(request, self.user)
        view = OpportunityViewSet.as_view({"get": "list"})

        with (
            patch.object(
                OpportunityOffersPermission,
                "has_permission",
                return_value=True,
            ),
            patch(
                "companyprofileapis.views.opportunity."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        rows = response.data.get("results", response.data)
        self.assertEqual(len(rows), 1)
        self.assertNotIn("line_items", rows[0])

    def test_direct_offer_pdf_failure_returns_clear_error_and_rolls_back(self):
        request = self.factory.post(
            "/api/company/opportunity/direct-offer/",
            {
                "title": "Failed direct offer",
                "customer_company": self.customer.id,
                "offer_expiry_period": "1:month",
                "line_items": [
                    {
                        "item_name": "Service",
                        "rate": "100.00",
                        "quantity": "1.0000",
                        "vat_rate": "12.00",
                    }
                ],
            },
            format="json",
        )
        force_authenticate(request, self.user)
        view = OpportunityViewSet.as_view({"post": "direct_offer"})

        with (
            patch.object(
                OpportunityOffersPermission,
                "has_permission",
                return_value=True,
            ),
            patch(
                "companyprofileapis.views.opportunity.create_or_replace_offer_pdf",
                side_effect=RuntimeError("render failed"),
            ),
        ):
            response = view(request)

        self.assertEqual(response.status_code, 400)
        self.assertIn("pdf", response.data)
        self.assertFalse(
            Opportunity.objects.filter(title="Failed direct offer").exists()
        )


class OfferLineItemMigrationTests(NotificationPreferenceStubMixin, TestCase):
    def test_backfill_preserves_old_values_and_leaves_open_opportunities_empty(self):
        user, company = create_company_user("line-item-migration@example.com")
        offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Legacy offer",
            status=Opportunity.Status.ISSUED,
            reference="OFF-202609-00001",
            value=Decimal("321.45"),
        )
        zero_offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Legacy offer without value",
            status=Opportunity.Status.DRAFT,
            reference="OFF-202609-00002",
            value=None,
        )
        opportunity = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Open opportunity",
            status=Opportunity.Status.OPEN,
            reference="OPP-202609-00003",
        )
        migration = import_module(
            "companyprofileapis.migrations.0013_offer_line_items"
        )

        migration.backfill_offer_line_items(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )
        migration.backfill_offer_line_items(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        migrated_item = offer.offer_line_items.get()
        self.assertEqual(migrated_item.item_name, "Legacy offer")
        self.assertEqual(migrated_item.rate, Decimal("321.45"))
        self.assertEqual(migrated_item.quantity, Decimal("1.0000"))
        self.assertEqual(migrated_item.vat_rate, Decimal("0.00"))
        self.assertEqual(offer.offer_line_items.count(), 1)
        zero_offer.refresh_from_db()
        self.assertEqual(zero_offer.value, Decimal("0.00"))
        self.assertEqual(zero_offer.offer_line_items.get().rate, Decimal("0.00"))
        self.assertFalse(opportunity.offer_line_items.exists())


class OfferExpiryMigrationTests(NotificationPreferenceStubMixin, TestCase):
    def test_backfill_uses_earliest_offer_history_and_is_idempotent(self):
        user, company = create_company_user("expiry-migration@example.com")
        offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Converted offer",
            status=Opportunity.Status.OPEN,
            reference="OPP-202601-00001",
        )
        offer.status = Opportunity.Status.CONVERTED
        offer.reference = "OFF-202601-00001"
        offer.save(update_fields=["status", "reference", "updated_at"])
        offer.offer_created_date = None
        offer.save(update_fields=["offer_created_date", "updated_at"])
        offer_history = offer.history.filter(status=Opportunity.Status.CONVERTED)
        offer_history.update(
            history_date=timezone.make_aware(datetime(2026, 1, 31, 12, 0))
        )
        migration = import_module(
            "companyprofileapis.migrations.0014_offer_expiry_and_pdf_document"
        )

        migration.backfill_offer_created_dates(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )
        migration.backfill_offer_created_dates(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        offer.refresh_from_db()
        self.assertEqual(offer.offer_created_date, date(2026, 1, 31))
        self.assertIsNone(offer.offer_expiry_period)
        self.assertIsNone(offer.offer_expiry_date)


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class OfferPdfAndSendTests(NotificationPreferenceStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.temp_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user, self.company = create_company_user("pdf-owner@example.com")
        self.customer = create_company_customer(
            self.company,
            "pdf-customer@example.com",
        )
        self.offer = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="Energy audit proposal",
            details="Detailed delivery description",
            status=Opportunity.Status.DRAFT,
            reference="OFF-202609-00001",
            customer_company=self.customer,
            value=1250,
            sent_date=date(2026, 1, 31),
        )
        OfferLineItem.objects.create(
            opportunity=self.offer,
            line_number=1,
            item_name="Energy audit",
            rate=Decimal("1000.00"),
            quantity=Decimal("1.0000"),
            vat_rate=Decimal("25.00"),
            total=Decimal("1000.00"),
        )

    def _create_supporting_document(
        self,
        *,
        file_name="supporting.txt",
        contents=b"supporting contents",
        company=None,
    ):
        company = company or self.company
        root, _ = Folder.objects.get_or_create(
            name=f"{company.id}_company_root_folder",
            parent=None,
            company_profile=company,
        )
        folder, _ = Folder.objects.get_or_create(
            name="Supporting documents",
            parent=root,
            defaults={"company_profile": company},
        )
        document = Document.objects.create(
            folder=folder,
            author=self.user,
            created_by=self.user,
            file_name=file_name,
        )
        document.file.save(file_name, ContentFile(contents), save=True)
        return document

    def _read_package_names(self):
        storage = Document().file.storage
        with storage.open(_package_storage_path(self.offer), "rb") as package_file:
            with zipfile.ZipFile(BytesIO(package_file.read())) as archive:
                return sorted(archive.namelist())

    def test_pdf_context_uses_created_by_dates_items_and_invoice_bill_to(self):
        context = build_offer_pdf_context(self.offer)

        self.assertEqual(context["contact_name"], self.user.full_name)
        self.assertEqual(context["contact_email"], self.user.email)
        self.assertEqual(context["contact_phone"], self.company.company_tell_no)
        self.assertEqual(context["issue_date"], "31-01-2026")
        self.assertEqual(context["sign_before"], "28-02-2026")
        self.assertEqual(context["line_items"][0]["vat_rate"], "25%")
        self.assertIn("250", context["line_items"][0]["vat_amount"])
        self.assertIn("1,000", context["subtotal"])
        self.assertIn("250", context["vat_amount"])
        self.assertIn("1,250", context["total"])
        self.assertEqual(context["recipient_company_name"], "Customer Company")
        self.assertEqual(context["recipient_vat_number"], "ISSUER-VAT")
        self.assertEqual(context["footer_company_registration_number"], "REG-42")
        self.assertEqual(context["footer_company_tax_number"], "ISSUER-VAT")

    def test_blank_sent_date_leaves_both_pdf_dates_blank(self):
        self.offer.sent_date = None
        context = build_offer_pdf_context(self.offer)
        self.assertEqual(context["issue_date"], "")
        self.assertEqual(context["sign_before"], "")

    def test_configured_expiry_is_displayed_without_sent_date(self):
        self.offer.sent_date = None
        self.offer.offer_created_date = date(2026, 1, 31)
        self.offer.offer_expiry_period = "1:month"
        self.offer.offer_expiry_date = date(2026, 2, 28)

        context = build_offer_pdf_context(self.offer)

        self.assertEqual(context["issue_date"], "")
        self.assertEqual(context["sign_before"], "28-02-2026")

    def test_customer_contact_bill_to_path(self):
        contact = ContactUser.objects.create(
            company_profile=self.company,
            name="Client",
            surname="Contact",
            email="client-contact@example.com",
            company_name="Client Contact Company",
            street="Contact Street",
            postcode="1000",
            city="Brussels",
            country="Belgium",
        )
        self.offer.customer_company = None
        self.offer.customer_user = contact
        context = build_offer_pdf_context(self.offer)

        self.assertEqual(context["recipient_name"], "Client Contact")
        self.assertEqual(context["recipient_company_name"], "Client Contact Company")
        self.assertEqual(context["recipient_vat_number"], "ISSUER-VAT")

    def test_store_replaces_pdf_and_keeps_supporting_document_in_place(self):
        supporting = self._create_supporting_document()
        self.offer.documents.add(supporting)

        first_document, action = create_or_replace_offer_pdf(self.offer)
        self.assertEqual(action, "created")
        self.offer.refresh_from_db()
        self.assertEqual(self.offer.offer_pdf_document_id, first_document.id)
        self.assertTrue(self.offer.documents.filter(pk=first_document.id).exists())
        second_document, action = create_or_replace_offer_pdf(self.offer)

        self.assertEqual(action, "updated")
        self.assertEqual(first_document.id, second_document.id)
        self.assertEqual(
            get_offer_pdf_documents(self.offer).count(),
            1,
        )
        self.assertEqual(second_document.file_name, f"{self.offer.reference}.pdf")
        self.assertEqual(
            second_document.folder.name,
            "Opportunities & Offers",
        )
        self.assertEqual(
            second_document.folder.system_key,
            OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
        )
        supporting.refresh_from_db()
        self.assertEqual(supporting.folder.name, "Supporting documents")

    def test_unlinked_pdf_remains_previewable_and_is_relinked_on_regeneration(self):
        document, _ = create_or_replace_offer_pdf(self.offer)
        self.offer.documents.remove(document)

        stored_document = get_stored_offer_pdf_document(self.offer)
        self.assertEqual(stored_document.id, document.id)
        self.assertFalse(self.offer.documents.filter(pk=document.id).exists())

        regenerated, action = create_or_replace_offer_pdf(self.offer)

        self.assertEqual(action, "updated")
        self.assertEqual(regenerated.id, document.id)
        self.assertTrue(self.offer.documents.filter(pk=document.id).exists())

    def test_deleted_pdf_is_recreated_and_relinked(self):
        document, _ = create_or_replace_offer_pdf(self.offer)
        original_id = document.id
        document.delete()
        self.offer.refresh_from_db()
        self.assertIsNone(self.offer.offer_pdf_document_id)

        recreated, action = create_or_replace_offer_pdf(self.offer)

        self.offer.refresh_from_db()
        self.assertEqual(action, "created")
        self.assertNotEqual(recreated.id, original_id)
        self.assertEqual(self.offer.offer_pdf_document_id, recreated.id)
        self.assertTrue(self.offer.documents.filter(pk=recreated.id).exists())

    def test_offer_pdf_summary_is_exposed_by_serializer(self):
        document, _ = create_or_replace_offer_pdf(self.offer)
        representation = OpportunitySerializer(
            self.offer,
            context={"request": serializer_request(self.user)},
        ).data

        self.assertEqual(representation["offer_pdf_document"]["id"], document.id)
        self.assertEqual(
            representation["offer_pdf_document"]["file_name"],
            f"{self.offer.reference}.pdf",
        )

    def test_store_reuses_legacy_named_offer_folder(self):
        root = Folder.objects.create(
            name=f"{self.company.id}_company_root_folder",
            company_profile=self.company,
        )
        legacy_folder = Folder.objects.create(
            name="Opportunities and Offers",
            parent=root,
            company_profile=self.company,
            system_key=OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
        )

        document, action = create_or_replace_offer_pdf(self.offer)

        self.assertEqual(action, "created")
        self.assertEqual(document.folder_id, legacy_folder.id)
        self.assertEqual(
            Folder.objects.filter(
                parent=root,
                system_key=OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
            ).count(),
            1,
        )

    @patch(
        "companyprofileapis.services.offer_send.render_offer_pdf",
        return_value=b"latest offer pdf",
    )
    def test_send_offer_only_defaults_date_and_marks_issued(self, _render):
        self.offer.sent_date = None
        self.offer.save(update_fields=["sent_date"])
        result = create_offer_send_package(
            self.offer,
            OFFER_INCLUDE_MODE_OFFER_ONLY,
        )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, Opportunity.Status.ISSUED)
        self.assertIsNotNone(self.offer.sent_date)
        self.assertEqual(result["recipient_email"], self.customer.contact_email)
        self.assertEqual(
            self._read_package_names(),
            [f"{self.offer.reference}.pdf"],
        )

    @patch(
        "companyprofileapis.services.offer_send.render_offer_pdf",
        return_value=b"latest offer pdf",
    )
    def test_send_with_documents_includes_every_linked_document_once(self, _render):
        first = self._create_supporting_document(file_name="one.txt", contents=b"1")
        second = self._create_supporting_document(file_name="two.txt", contents=b"2")
        self.offer.documents.add(first, second)

        create_offer_send_package(
            self.offer,
            OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
        )

        self.assertEqual(
            self._read_package_names(),
            [f"{self.offer.reference}.pdf", "one.txt", "two.txt"],
        )
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.folder.name, "Supporting documents")
        self.assertEqual(second.folder.name, "Supporting documents")

    @patch(
        "companyprofileapis.services.offer_send.render_offer_pdf",
        return_value=b"latest offer pdf",
    )
    def test_project_converted_offer_can_be_sent_without_status_change(self, _render):
        self.offer.status = Opportunity.Status.CONVERTED_TO_PROJECT
        self.offer.sent_date = date(2026, 3, 15)
        self.offer.save(update_fields=["status", "sent_date"])

        create_offer_send_package(
            self.offer,
            OFFER_INCLUDE_MODE_OFFER_ONLY,
            actor=self.user,
        )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, Opportunity.Status.CONVERTED_TO_PROJECT)
        self.assertEqual(self.offer.sent_date, date(2026, 3, 15))
        self.assertEqual(
            self.offer.history.first().history_change_reason,
            OpportunityHistoryReason.OFFER_ISSUED,
        )

    @patch(
        "companyprofileapis.services.offer_send.render_offer_pdf",
        return_value=b"latest offer pdf",
    )
    def test_every_reissue_creates_one_semantic_history_entry(self, _render):
        initial_count = self.offer.history.count()

        create_offer_send_package(
            self.offer,
            OFFER_INCLUDE_MODE_OFFER_ONLY,
            actor=self.user,
        )
        create_offer_send_package(
            self.offer,
            OFFER_INCLUDE_MODE_OFFER_ONLY,
            actor=self.user,
        )

        issue_history = self.offer.history.filter(
            history_change_reason=OpportunityHistoryReason.OFFER_ISSUED
        )
        self.assertEqual(issue_history.count(), 2)
        self.assertEqual(self.offer.history.count(), initial_count + 2)
        self.assertFalse(issue_history.exclude(history_user=self.user).exists())

    def test_failed_send_preserves_state_and_existing_pdf(self):
        existing_document, _ = create_or_replace_offer_pdf(self.offer)
        with existing_document.file.open("rb") as existing_file:
            previous_pdf = existing_file.read()
        previous_status = self.offer.status
        previous_date = self.offer.sent_date
        previous_history_count = self.offer.history.count()

        with patch(
            "companyprofileapis.services.offer_send.render_offer_pdf",
            side_effect=RuntimeError("render failed"),
        ):
            with self.assertRaises(RuntimeError):
                create_offer_send_package(
                    self.offer,
                    OFFER_INCLUDE_MODE_OFFER_ONLY,
                )

        self.offer.refresh_from_db()
        existing_document.refresh_from_db()
        with existing_document.file.open("rb") as existing_file:
            self.assertEqual(existing_file.read(), previous_pdf)
        self.assertEqual(self.offer.status, previous_status)
        self.assertEqual(self.offer.sent_date, previous_date)
        self.assertEqual(self.offer.history.count(), previous_history_count)

    def test_cross_tenant_supporting_document_is_rejected(self):
        _, other_company = create_company_user("pdf-other-owner@example.com")
        other_document = self._create_supporting_document(
            company=other_company,
            file_name="other.txt",
        )
        self.offer.documents.add(other_document)

        with patch(
            "companyprofileapis.services.offer_send.render_offer_pdf",
            return_value=b"latest offer pdf",
        ):
            with self.assertRaisesRegex(ValueError, "does not belong"):
                create_offer_send_package(
                    self.offer,
                    OFFER_INCLUDE_MODE_WITH_SUPPORTING_DOCUMENTS,
                )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.status, Opportunity.Status.DRAFT)

    def test_closed_and_declined_offers_cannot_be_sent(self):
        for offer_status in (Opportunity.Status.CLOSED, Opportunity.Status.DECLINED):
            with self.subTest(status=offer_status):
                self.offer.status = offer_status
                self.offer.save(update_fields=["status"])
                with self.assertRaises(OpportunityLifecycleError):
                    create_offer_send_package(
                        self.offer,
                        OFFER_INCLUDE_MODE_OFFER_ONLY,
                    )


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }
)
class OfferPdfBackfillTests(NotificationPreferenceStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.temp_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_directory.cleanup)
        self.media_override = override_settings(MEDIA_ROOT=self.temp_directory.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.user, self.company = create_company_user("pdf-backfill@example.com")
        self.offer = Opportunity.objects.create(
            created_by=self.user,
            company_profile=self.company,
            title="Existing offer",
            status=Opportunity.Status.DRAFT,
            reference="OFF-202609-00999",
        )
        root = Folder.objects.create(
            name=f"{self.company.id}_company_root_folder",
            company_profile=self.company,
        )
        self.folder = Folder.objects.create(
            name="Opportunities & Offers",
            system_key=OFFER_DOCUMENT_FOLDER_SYSTEM_KEY,
            parent=root,
            company_profile=self.company,
        )

    def _matching_document(self):
        document = Document.objects.create(
            folder=self.folder,
            author=self.user,
            created_by=self.user,
            file_name=f"{self.offer.reference}.pdf",
            document_type="Offer",
            identification_number=self.offer.reference,
        )
        document.file.save(document.file_name, ContentFile(b"offer"), save=True)
        return document

    def test_backfill_links_one_exact_match(self):
        document = self._matching_document()
        migration = import_module(
            "companyprofileapis.migrations.0015_backfill_offer_pdf_documents"
        )

        migration.backfill_offer_pdf_documents(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        self.offer.refresh_from_db()
        self.assertEqual(self.offer.offer_pdf_document_id, document.id)
        self.assertTrue(self.offer.documents.filter(pk=document.id).exists())

    def test_backfill_skips_ambiguous_matches(self):
        self._matching_document()
        self._matching_document()
        migration = import_module(
            "companyprofileapis.migrations.0015_backfill_offer_pdf_documents"
        )

        migration.backfill_offer_pdf_documents(
            apps,
            type("SchemaEditor", (), {"connection": connection})(),
        )

        self.offer.refresh_from_db()
        self.assertIsNone(self.offer.offer_pdf_document_id)
        self.assertFalse(self.offer.documents.exists())
