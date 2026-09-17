from datetime import date
from types import SimpleNamespace

from django.contrib.auth.models import Group
from django.db import transaction
from django.test import TestCase
from rest_framework import serializers

from companyprofileapis.models import Opportunity
from companyprofileapis.serializers.company import CompanyConfigSerializer
from companyprofileapis.services.offer_validity import (
    calculate_offer_expiry_date,
    validate_offer_validity_period_removal,
)
from companyprofileapis.views.company import CompanyConfigViewSet
from ecpmpapp.constants import UserType
from models.configuration_templates import get_company_configuration_defaults
from models.models import CompanyConfig, CompanyProfile, GlobalConfig, User


class OfferValidityConfigurationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="offer-validity@example.com",
            password="password",
            first_name="Offer",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Offer Validity Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="validity@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.user.groups.add(Group.objects.get_or_create(name=UserType.ADMIN.value)[0])
        configuration_defaults = get_company_configuration_defaults()
        configuration_defaults["offer_validity_periods"] = [
            "15:day",
            "1:month",
        ]
        self.configuration = CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            **configuration_defaults,
        )

    def test_serializer_normalizes_validity_periods(self):
        serializer = CompanyConfigSerializer(
            self.configuration,
            data={"offer_validity_periods": ["15 Days", "1:months"]},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["offer_validity_periods"],
            ["15:day", "1:month"],
        )

    def test_used_validity_period_cannot_be_removed(self):
        Opportunity.objects.bulk_create(
            [
                Opportunity(
                    company_profile=self.company,
                    created_by=self.user,
                    title="Used validity period",
                    status=Opportunity.Status.DRAFT,
                    offer_expiry_period="1:month",
                )
            ]
        )
        serializer = CompanyConfigSerializer(
            self.configuration,
            data={"offer_validity_periods": ["15:day"]},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        error = serializer.errors["offer_validity_periods"]
        self.assertEqual(error["code"], "configuration_value_in_use")
        self.assertEqual(error["values"], ["1:month"])

    def test_unused_validity_period_can_be_removed(self):
        serializer = CompanyConfigSerializer(
            self.configuration,
            data={"offer_validity_periods": ["15:day"]},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_configuration_response_marks_used_validity_periods(self):
        self._create_offer_using_month_period()
        request = SimpleNamespace(query_params={"include_defaults": "true"})

        representation = CompanyConfigSerializer(
            self.configuration,
            context={"request": request},
        ).data

        periods = {
            item["value"]: item["is_in_use"]
            for item in representation["offer_validity_periods"]
        }
        self.assertEqual(periods, {"15:day": False, "1:month": True})

    def test_another_company_does_not_block_removal(self):
        other_user = User.objects.create_user(
            email="other-offer-validity@example.com",
            password="password",
            first_name="Other",
            last_name="Owner",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_user,
            company_name="Other Offer Validity Company",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://other.example.com",
            company_email_address="other-validity@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        Opportunity.objects.bulk_create(
            [
                Opportunity(
                    company_profile=other_company,
                    created_by=other_user,
                    title="Other company Offer",
                    status=Opportunity.Status.DRAFT,
                    offer_expiry_period="1:month",
                )
            ]
        )

        normalized = validate_offer_validity_period_removal(
            self.configuration,
            ["15:day"],
        )

        self.assertEqual(normalized, ["15:day"])

    def test_cross_solution_propagation_rejects_removing_used_period(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Core Solution",
            offer_validity_periods=["1:month"],
        )
        Opportunity.objects.bulk_create(
            [
                Opportunity(
                    company_profile=self.company,
                    created_by=self.user,
                    title="Used by another solution configuration",
                    status=Opportunity.Status.DRAFT,
                    offer_expiry_period="1:month",
                )
            ]
        )
        self.configuration.offer_validity_periods = ["15:day"]

        with self.assertRaises(serializers.ValidationError) as context:
            with transaction.atomic():
                CompanyConfigViewSet()._propagate_changes_to_other_solutions(
                    self.configuration,
                    {"offer_validity_periods"},
                )

        error = context.exception.detail["offer_validity_periods"]
        self.assertEqual(error["code"], "configuration_value_in_use")
        self.assertEqual(error["values"], ["1:month"])

    def test_cross_solution_propagation_updates_unused_periods(self):
        other_configuration = CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Core Solution",
            offer_validity_periods=["1:month"],
        )
        self.configuration.offer_validity_periods = ["15:day"]

        with transaction.atomic():
            CompanyConfigViewSet()._propagate_changes_to_other_solutions(
                self.configuration,
                {"offer_validity_periods"},
            )

        other_configuration.refresh_from_db()
        self.assertEqual(
            other_configuration.offer_validity_periods,
            ["15:day"],
        )

    def _configured_view(self, data=None):
        request = SimpleNamespace(user=self.user, data=data or {})
        view = CompanyConfigViewSet()
        view.request = request
        view.format_kwarg = None
        view.kwargs = {}
        return view, request

    def _create_offer_using_month_period(self):
        return Opportunity.objects.bulk_create(
            [
                Opportunity(
                    company_profile=self.company,
                    created_by=self.user,
                    title="Offer using protected validity period",
                    status=Opportunity.Status.DRAFT,
                    offer_expiry_period="1:month",
                )
            ]
        )[0]

    def test_reset_rejects_removing_used_period_and_rolls_back(self):
        self._create_offer_using_month_period()
        view, request = self._configured_view()

        with self.assertRaises(serializers.ValidationError):
            view.reset_config(request)

        self.configuration.refresh_from_db()
        self.assertEqual(
            self.configuration.offer_validity_periods,
            ["15:day", "1:month"],
        )

    def test_replace_sync_rejects_removing_used_period_and_rolls_back(self):
        self._create_offer_using_month_period()
        global_defaults = get_company_configuration_defaults()
        global_defaults["offer_validity_periods"] = ["15:day"]
        GlobalConfig.objects.create(
            company_profile=self.company,
            **global_defaults,
        )
        view, request = self._configured_view({"strategy": "replace"})

        with self.assertRaises(serializers.ValidationError):
            view.sync_with_global(request)

        self.configuration.refresh_from_db()
        self.assertEqual(
            self.configuration.offer_validity_periods,
            ["15:day", "1:month"],
        )

    def test_preserve_sync_keeps_used_period_and_appends_template_values(self):
        self._create_offer_using_month_period()
        global_defaults = get_company_configuration_defaults()
        global_defaults["offer_validity_periods"] = ["6:week"]
        GlobalConfig.objects.create(
            company_profile=self.company,
            **global_defaults,
        )
        view, request = self._configured_view({"strategy": "preserve_local"})

        response = view.sync_with_global(request)

        self.assertEqual(response.status_code, 200)
        self.configuration.refresh_from_db()
        self.assertEqual(
            self.configuration.offer_validity_periods,
            ["15:day", "1:month", "6:week"],
        )


class OfferExpiryCalculationTests(TestCase):
    def test_calculates_day_week_month_and_year_periods(self):
        self.assertEqual(
            calculate_offer_expiry_date(date(2026, 9, 15), "15:day"),
            date(2026, 9, 30),
        )
        self.assertEqual(
            calculate_offer_expiry_date(date(2026, 9, 15), "6:week"),
            date(2026, 10, 27),
        )
        self.assertEqual(
            calculate_offer_expiry_date(date(2026, 1, 31), "1:month"),
            date(2026, 2, 28),
        )
        self.assertEqual(
            calculate_offer_expiry_date(date(2024, 2, 29), "1:year"),
            date(2025, 2, 28),
        )
