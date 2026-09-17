from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from models.models import CompanyProfile, User


class CompanySearchOrderingTests(TestCase):
    def setUp(self):
        self.license_permission_patcher = patch(
            "ecpmpapp.permissions.LicensePermission.has_permission",
            return_value=True,
        )
        self.license_permission_patcher.start()
        self.addCleanup(self.license_permission_patcher.stop)

        self.client = APIClient()
        request_user = User.objects.create_user(
            email="company-search-requester@example.com",
            password="password",
            user_type="company_user",
        )
        request_company = self.create_company(
            request_user,
            "Request Company",
            "999",
            searchable=False,
        )
        request_user.company = request_company
        request_user.save(update_fields=["company"])

        twenty_admin = User.objects.create_user(
            email="twenty-company-admin@example.com",
            password="password",
            user_type="company_user",
        )
        three_admin = User.objects.create_user(
            email="three-company-admin@example.com",
            password="password",
            user_type="company_user",
        )
        self.twenty_company = self.create_company(
            twenty_admin,
            "Telephone Sorting Twenty",
            "20",
        )
        self.three_company = self.create_company(
            three_admin,
            "Telephone Sorting Three",
            "3",
        )
        self.client.force_authenticate(request_user)

    @staticmethod
    def create_company(admin_user, name, telephone, *, searchable=True):
        return CompanyProfile.objects.create(
            admin_user=admin_user,
            company_name=name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no=telephone,
            company_mob_no="",
            company_web_address="https://example.com",
            company_email_address=admin_user.email,
            company_tax_reference="tax",
            company_iban="iban",
            company_searchable=searchable,
        )

    def test_search_results_can_be_ordered_by_company_telephone(self):
        response = self.client.get(
            "/api/company/company-search",
            {"search": "Telephone Sorting", "ordering": "company_tell_no"},
        )

        self.assertEqual(response.status_code, 200)
        result_ids = [item["id"] for item in response.data["results"]]
        self.assertEqual(
            result_ids,
            [self.twenty_company.id, self.three_company.id],
        )
