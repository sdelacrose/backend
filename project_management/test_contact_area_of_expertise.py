from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db.models import Case
from django.test import SimpleTestCase
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from models.hierarchical_configurations import AREA_OF_EXPERTISE_KEY
from ecpmpapp.filters import ArrayOverlapFilter
from project_management.filters import (
    ContactCompanyFilter,
    ContactUserFilter,
)
from project_management.models import ContactCompany, ContactUser
from project_management.serializers.contact import (
    ContactCompanySerializer,
    ContactUserSerializer,
)
from project_management.views.contact import (
    ContactCompanyViewSet,
    ContactUserViewSet,
)
from companyprofileapis.filters import CompanySearchFilter
from companyprofileapis.serializers.company import (
    CompanyProfileSearchSerializer,
    UserSerializer,
)
from companyprofileapis.services.area_of_expertise import (
    get_company_config_for_request,
)
from companyprofileapis.views.company import CompanySearchView
from ecpmpapp.ordering import TableOrderingFilter
from models.models import User
from userapis.filters import UserSearchFilter
from userapis.serializers import PublicUserSerializer
from userapis.views.user import UserSearchView


class ContactAreaOfExpertiseTests(SimpleTestCase):
    def setUp(self):
        self.request = SimpleNamespace(user=SimpleNamespace())

    def test_contact_filters_accept_comma_separated_main_and_sub_filters(self):
        for filterset in (ContactUserFilter, ContactCompanyFilter):
            filters = filterset.get_filters()
            self.assertEqual(
                filters["main_area_of_expertise"].lookup_expr,
                "in",
            )
            self.assertEqual(
                filters["sub_area_of_expertise"].field_name,
                "sub_area_of_expertise",
            )
            self.assertIsInstance(
                filters["sub_area_of_expertise"],
                ArrayOverlapFilter,
            )

    def test_sub_area_filter_matches_any_selected_value(self):
        queryset = Mock()
        filtered_queryset = Mock()
        queryset.filter.return_value = filtered_queryset
        filter_ = ArrayOverlapFilter(field_name="sub_area_of_expertise")

        result = filter_.filter(
            queryset,
            "program-management,portfolio-management-ppm",
        )

        queryset.filter.assert_called_once_with(
            sub_area_of_expertise__overlap=[
                "program-management",
                "portfolio-management-ppm",
            ]
        )
        self.assertIs(result, filtered_queryset)

    def test_public_network_filters_and_serializers_expose_both_fields(self):
        for filterset in (UserSearchFilter, CompanySearchFilter):
            filters = filterset.get_filters()
            self.assertEqual(
                filters["main_area_of_expertise"].lookup_expr,
                "in",
            )
            self.assertIsInstance(
                filters["sub_area_of_expertise"],
                ArrayOverlapFilter,
            )

        for serializer in (PublicUserSerializer(), CompanyProfileSearchSerializer()):
            self.assertIn("main_area_of_expertise", serializer.fields)
            self.assertIn("sub_area_of_expertise", serializer.fields)

    def test_expertise_fields_use_localized_value_ordering(self):
        for view in (
            ContactUserViewSet,
            ContactCompanyViewSet,
            UserSearchView,
            CompanySearchView,
        ):
            for field in (
                "main_area_of_expertise",
                "sub_area_of_expertise",
            ):
                self.assertIn(field, view.ordering_fields)
                self.assertIn(field, view.ordering_localized_fields)
                self.assertNotIn(field, view.ordering_natural_fields)
            self.assertIn(
                "sub_area_of_expertise",
                view.ordering_localized_array_fields,
            )

    def test_localized_order_values_preserve_client_label_order(self):
        request = SimpleNamespace(
            query_params={
                "main_area_of_expertise_order": "operations,engineering,operations"
            }
        )
        view = SimpleNamespace(
            ordering_localized_fields={"main_area_of_expertise"}
        )

        values = TableOrderingFilter._localized_value_order(
            request,
            view,
            "main_area_of_expertise",
        )

        self.assertEqual(values, ["operations", "engineering"])

    @patch(
        "project_management.serializers.contact.get_company_config_for_request",
        return_value=None,
    )
    def test_contact_user_accepts_matching_main_and_sub_ids(self, _config):
        contact = ContactUser(name="Test", surname="User", email="test@example.com")
        serializer = ContactUserSerializer(
            contact,
            data={
                "main_area_of_expertise": "project-program-management",
                "sub_area_of_expertise": "program-management",
            },
            partial=True,
            context={"request": self.request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["sub_area_of_expertise"],
            ["program-management"],
        )

    @patch(
        "project_management.serializers.contact.get_company_config_for_request",
        return_value=None,
    )
    def test_contact_user_accepts_multiple_matching_sub_area_ids(self, _config):
        contact = ContactUser(name="Test", surname="User", email="test@example.com")
        serializer = ContactUserSerializer(
            contact,
            data={
                "main_area_of_expertise": "project-program-management",
                "sub_area_of_expertise": [
                    "program-management",
                    "portfolio-management-ppm",
                ],
            },
            partial=True,
            context={"request": self.request},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(
            serializer.validated_data["sub_area_of_expertise"],
            ["program-management", "portfolio-management-ppm"],
        )

    @patch(
        "project_management.serializers.contact.get_company_config_for_request"
    )
    def test_contact_company_rejects_disabled_sub_area(self, get_config):
        get_config.return_value = SimpleNamespace(
            nested_configurations={},
            configuration_visibility={
                AREA_OF_EXPERTISE_KEY: {
                    "disabled_child_ids": ["program-management"],
                }
            },
        )
        company = ContactCompany(
            company_name="Test Company",
            contact_email="company@example.com",
        )
        serializer = ContactCompanySerializer(
            company,
            data={
                "main_area_of_expertise": "project-program-management",
                "sub_area_of_expertise": ["program-management"],
            },
            partial=True,
            context={"request": self.request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("area_of_expertise", serializer.errors)


class ContactAreaOfExpertiseOrderingTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.request = SimpleNamespace(user=SimpleNamespace())

    def _ordered_queryset(self, ordering):
        request = Request(
            self.factory.get(
                "/",
                {
                    "ordering": ordering,
                    "main_area_of_expertise_order": (
                        "engineering,operations-maintenance"
                    ),
                },
            )
        )
        return TableOrderingFilter().filter_queryset(
            request,
            ContactUser.objects.all(),
            ContactUserViewSet(),
        )

    def test_localized_rank_is_used_in_both_sort_directions(self):
        ascending = self._ordered_queryset("main_area_of_expertise")
        descending = self._ordered_queryset("-main_area_of_expertise")

        annotation = ascending.query.annotations["_ordering_localized_0"]
        self.assertIsInstance(annotation, Case)
        self.assertEqual(len(annotation.cases), 2)
        self.assertIsNone(annotation.default.value)

        ascending_order = ascending.query.order_by[0]
        descending_order = descending.query.order_by[0]
        self.assertFalse(ascending_order.descending)
        self.assertTrue(descending_order.descending)
        self.assertTrue(ascending_order.nulls_last)
        self.assertTrue(descending_order.nulls_last)

    def test_localized_sub_area_ordering_checks_array_membership(self):
        request = Request(
            self.factory.get(
                "/",
                {
                    "ordering": "sub_area_of_expertise",
                    "sub_area_of_expertise_order": (
                        "program-management,portfolio-management-ppm"
                    ),
                },
            )
        )

        queryset = TableOrderingFilter().filter_queryset(
            request,
            ContactUser.objects.all(),
            ContactUserViewSet(),
        )

        self.assertIn(
            '"project_management_contactuser"."sub_area_of_expertise" @>',
            str(queryset.query),
        )

    @patch(
        "companyprofileapis.services.area_of_expertise.CompanyConfig.objects.filter"
    )
    def test_company_config_lookup_uses_the_exact_requested_solution(self, filter_):
        company = object()
        request = SimpleNamespace(
            user=SimpleNamespace(is_freelancer=False, company=company),
            query_params={"solution": "Enterprise Solution"},
        )
        filter_.return_value.only.return_value.first.return_value = object()

        get_company_config_for_request(request)

        filter_.assert_called_once_with(
            company_profile=company,
            solution="Enterprise Solution",
        )

    @patch(
        "companyprofileapis.serializers.company.get_company_config_for_request"
    )
    def test_user_profile_rejects_disabled_sub_area(self, get_config):
        get_config.return_value = SimpleNamespace(
            nested_configurations={},
            configuration_visibility={
                AREA_OF_EXPERTISE_KEY: {
                    "disabled_child_ids": ["program-management"],
                }
            },
        )
        user = User(username="test-user", email="test@example.com")
        serializer = UserSerializer(
            user,
            data={
                "main_area_of_expertise": "project-program-management",
                "sub_area_of_expertise": "program-management",
            },
            partial=True,
            context={"request": self.request},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("area_of_expertise", serializer.errors)
