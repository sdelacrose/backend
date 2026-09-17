from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from types import SimpleNamespace
from datetime import date
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory

from companyprofileapis.models import Opportunity, OpportunityReferenceCounter
from companyprofileapis.serializers import OpportunitySerializer
from companyprofileapis.services.opportunity_lifecycle import (
    OpportunityLifecycleAction,
)
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import ContactCompany


def create_company_user(email="opportunity-owner@example.com"):
    user = User.objects.create_user(
        email=email,
        password="password",
        first_name="Opportunity",
        last_name="Owner",
        user_type="company_user",
    )
    company = CompanyProfile.objects.create(
        admin_user=user,
        company_name="Opportunity Company",
        company_address="Street 1",
        company_town="Town",
        company_tell_no="123",
        company_mob_no="456",
        company_web_address="https://example.com",
        company_email_address=email,
        company_tax_reference="tax",
        company_iban="iban",
    )
    user.company = company
    user.save(update_fields=["company"])
    CompanyConfig.objects.create(
        company_profile=company,
        solution="Energy Consultant Solution",
        offer_validity_periods=["1:month"],
    )
    return user, company


def create_customer(company, email="customer@example.com"):
    return ContactCompany.objects.create(
        company_profile=company,
        company_name="Customer Company",
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


class OpportunityReferenceTests(NotificationPreferenceStubMixin, TestCase):
    def test_creating_opportunity_allocates_owner_reference(self):
        user, company = create_company_user()
        customer = create_customer(company)
        serializer = OpportunitySerializer(
            data={
                "title": "New engagement",
                "customer_company": customer.id,
            },
            context={"request": serializer_request(user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        opportunity = serializer.save()

        current_period = timezone.localdate().strftime("%Y%m")
        self.assertEqual(
            opportunity.reference,
            f"OPP-{current_period}-00001",
        )
        self.assertEqual(opportunity.company_profile, company)
        self.assertEqual(opportunity.status, Opportunity.Status.OPEN)

    def test_converting_opportunity_replaces_only_reference_prefix(self):
        user, company = create_company_user()
        customer = create_customer(company)
        create_serializer = OpportunitySerializer(
            data={
                "title": "New engagement",
                "customer_company": customer.id,
            },
            context={"request": serializer_request(user)},
        )
        self.assertTrue(create_serializer.is_valid(), create_serializer.errors)
        opportunity = create_serializer.save()
        original_reference = opportunity.reference

        update_serializer = OpportunitySerializer(
            opportunity,
            data={
                "line_items": [
                    {
                        "item_name": "Converted service",
                        "rate": "0.00",
                        "quantity": "1.0000",
                        "vat_rate": "12.00",
                    }
                ],
                "offer_expiry_period": "1:month",
            },
            partial=True,
            context={
                "request": serializer_request(user),
                "lifecycle_action": OpportunityLifecycleAction.CONVERT_TO_OFFER,
            },
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        converted = update_serializer.save()

        self.assertEqual(
            converted.reference,
            original_reference.replace("OPP-", "OFF-", 1),
        )
        self.assertEqual(
            OpportunityReferenceCounter.objects.get(
                company_profile=company,
            ).last_sequence,
            1,
        )

    def test_direct_offer_shares_sequence_without_monthly_reset(self):
        user, company = create_company_user()
        customer = create_customer(company)
        with patch(
            "companyprofileapis.services.opportunity_references.timezone.localdate",
            return_value=date(2026, 9, 30),
        ):
            opportunity_serializer = OpportunitySerializer(
                data={
                    "title": "September opportunity",
                    "customer_company": customer.id,
                },
                context={"request": serializer_request(user)},
            )
            self.assertTrue(
                opportunity_serializer.is_valid(),
                opportunity_serializer.errors,
            )
            opportunity = opportunity_serializer.save()

        with patch(
            "companyprofileapis.services.opportunity_references.timezone.localdate",
            return_value=date(2026, 10, 1),
        ):
            offer_serializer = OpportunitySerializer(
                data={
                    "title": "October direct offer",
                    "customer_company": customer.id,
                    "offer_expiry_period": "1:month",
                    "line_items": [
                        {
                            "item_name": "Direct service",
                            "rate": "0.00",
                            "quantity": "1.0000",
                            "vat_rate": "12.00",
                        }
                    ],
                },
                context={
                    "request": serializer_request(user),
                    "lifecycle_action": (
                        OpportunityLifecycleAction.CREATE_DIRECT_OFFER
                    ),
                },
            )
            self.assertTrue(offer_serializer.is_valid(), offer_serializer.errors)
            direct_offer = offer_serializer.save()

        self.assertEqual(opportunity.reference, "OPP-202609-00001")
        self.assertEqual(direct_offer.reference, "OFF-202610-00002")

    def test_reference_expands_beyond_five_digits(self):
        user, company = create_company_user()
        customer = create_customer(company)
        OpportunityReferenceCounter.objects.create(
            company_profile=company,
            last_sequence=99999,
        )
        serializer = OpportunitySerializer(
            data={
                "title": "Large sequence",
                "customer_company": customer.id,
            },
            context={"request": serializer_request(user)},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        opportunity = serializer.save()

        current_period = timezone.localdate().strftime("%Y%m")
        self.assertEqual(
            opportunity.reference,
            f"OPP-{current_period}-100000",
        )

    def test_company_and_freelancer_have_independent_sequences(self):
        company_user, company = create_company_user()
        company_customer = create_customer(company)
        company_serializer = OpportunitySerializer(
            data={
                "title": "Company opportunity",
                "customer_company": company_customer.id,
            },
            context={"request": serializer_request(company_user)},
        )
        self.assertTrue(company_serializer.is_valid(), company_serializer.errors)
        company_opportunity = company_serializer.save()

        freelancer = User.objects.create_user(
            email="freelancer-owner@example.com",
            password="password",
            first_name="Freelancer",
            last_name="Owner",
            user_type="freelancer",
        )
        freelancer_customer = ContactCompany.objects.create(
            user=freelancer,
            company_name="Freelancer Customer",
            contact_email="freelancer-customer@example.com",
        )
        freelancer_serializer = OpportunitySerializer(
            data={
                "title": "Freelancer opportunity",
                "customer_company": freelancer_customer.id,
            },
            context={"request": serializer_request(freelancer)},
        )
        self.assertTrue(
            freelancer_serializer.is_valid(),
            freelancer_serializer.errors,
        )
        freelancer_opportunity = freelancer_serializer.save()

        current_period = timezone.localdate().strftime("%Y%m")
        self.assertEqual(
            company_opportunity.reference,
            f"OPP-{current_period}-00001",
        )
        self.assertEqual(
            freelancer_opportunity.reference,
            f"OPP-{current_period}-00001",
        )

    def test_reference_is_read_only_through_serializer(self):
        user, company = create_company_user()
        customer = create_customer(company)
        create_serializer = OpportunitySerializer(
            data={
                "title": "Protected reference",
                "customer_company": customer.id,
                "reference": "OPP-199901-99999",
            },
            context={"request": serializer_request(user)},
        )
        self.assertTrue(create_serializer.is_valid(), create_serializer.errors)
        opportunity = create_serializer.save()
        generated_reference = opportunity.reference

        update_serializer = OpportunitySerializer(
            opportunity,
            data={"reference": "OFF-199901-99999"},
            partial=True,
            context={"request": serializer_request(user)},
        )
        self.assertTrue(update_serializer.is_valid(), update_serializer.errors)
        updated = update_serializer.save()

        self.assertEqual(updated.reference, generated_reference)


class OpportunityReferenceMigrationTests(NotificationPreferenceStubMixin, TestCase):
    def test_backfill_assigns_prefixes_and_one_owner_sequence(self):
        user, company = create_company_user("migration-owner@example.com")
        open_opportunity = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Open",
            status=Opportunity.Status.OPEN,
        )
        converted_offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Converted",
            status=Opportunity.Status.CONVERTED,
        )
        project_converted_offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Converted to project",
            status=Opportunity.Status.CONVERTED_TO_PROJECT,
        )
        closed_opportunity = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Closed opportunity",
            status=Opportunity.Status.CLOSED,
        )
        closed_offer = Opportunity.objects.create(
            created_by=user,
            company_profile=company,
            title="Closed offer",
            status=Opportunity.Status.CLOSED,
            value=0,
        )
        freelancer = User.objects.create_user(
            email="migration-freelancer@example.com",
            password="password",
            first_name="Migration",
            last_name="Freelancer",
            user_type="freelancer",
        )
        freelancer_offer = Opportunity.objects.create(
            created_by=freelancer,
            freelancer=freelancer,
            title="Freelancer offer",
            status=Opportunity.Status.DRAFT,
        )
        migration = import_module(
            "companyprofileapis.migrations.0012_opportunity_offer_lifecycle_references"
        )

        migration.backfill_opportunity_references(
            apps,
            SimpleNamespace(connection=connection),
        )

        for opportunity in (
            open_opportunity,
            converted_offer,
            project_converted_offer,
            closed_opportunity,
            closed_offer,
            freelancer_offer,
        ):
            opportunity.refresh_from_db()

        period = open_opportunity.created_at.strftime("%Y%m")
        self.assertEqual(open_opportunity.reference, f"OPP-{period}-00001")
        self.assertEqual(converted_offer.reference, f"OFF-{period}-00002")
        self.assertEqual(
            project_converted_offer.reference,
            f"OFF-{period}-00003",
        )
        self.assertEqual(closed_opportunity.reference, f"OPP-{period}-00004")
        self.assertEqual(closed_offer.reference, f"OFF-{period}-00005")
        freelancer_period = freelancer_offer.created_at.strftime("%Y%m")
        self.assertEqual(
            freelancer_offer.reference,
            f"OFF-{freelancer_period}-00001",
        )
        self.assertEqual(
            OpportunityReferenceCounter.objects.get(
                company_profile=company,
            ).last_sequence,
            5,
        )
        self.assertEqual(
            OpportunityReferenceCounter.objects.get(
                freelancer=freelancer,
            ).last_sequence,
            1,
        )
        self.assertFalse(
            open_opportunity.history.exclude(
                reference=open_opportunity.reference,
            ).exists()
        )


class ConcurrentOpportunityReferenceTests(
    NotificationPreferenceStubMixin,
    TransactionTestCase,
):
    reset_sequences = True

    def setUp(self):
        super().setUp()
        self.freelancer = User.objects.create_user(
            email="concurrent-opportunity-owner@example.com",
            password="password",
            first_name="Concurrent",
            last_name="Owner",
            user_type="freelancer",
        )
        self.customer = ContactCompany.objects.create(
            user=self.freelancer,
            company_name="Concurrent Customer",
            contact_email="concurrent-customer@example.com",
        )

    def _create_opportunity(self, index):
        close_old_connections()
        try:
            freelancer = User.objects.get(pk=self.freelancer.pk)
            customer = ContactCompany.objects.get(pk=self.customer.pk)
            serializer = OpportunitySerializer(
                data={
                    "title": f"Concurrent opportunity {index}",
                    "customer_company": customer.id,
                },
                context={"request": serializer_request(freelancer)},
            )
            serializer.is_valid(raise_exception=True)
            return serializer.save().reference
        finally:
            close_old_connections()

    def test_concurrent_creations_receive_unique_references(self):
        if not connection.features.has_select_for_update:
            self.skipTest("Database does not support row-level locking.")

        with ThreadPoolExecutor(max_workers=2) as executor:
            references = list(executor.map(self._create_opportunity, range(2)))

        period = timezone.localdate().strftime("%Y%m")
        self.assertEqual(
            sorted(references),
            [
                f"OPP-{period}-00001",
                f"OPP-{period}-00002",
            ],
        )
