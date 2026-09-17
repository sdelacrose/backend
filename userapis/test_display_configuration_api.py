from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from models.display_feature_registry import (
    DISPLAY_FEATURE_REGISTRY_REVISION,
    DISPLAY_FEATURE_REGISTRY_VERSION,
    DISPLAY_FEATURE_SCHEMA_MAJOR,
)
from models.models import CompanyProfile, SolutionDisplayConfiguration, User


class DisplayConfigurationApiTests(TestCase):
    endpoint = "/api/user/display-configuration"

    def setUp(self):
        self.license_permission_patcher = patch(
            "ecpmpapp.permissions.LicensePermission.has_permission",
            return_value=True,
        )
        self.license_permission_patcher.start()
        self.addCleanup(self.license_permission_patcher.stop)
        self.client = APIClient()
        self.company, self.user = self._create_company(
            "runtime_company_a",
            "Runtime Company A",
        )

    def _create_user(self, email, company=None, user_type="company_user"):
        return User.objects.create_user(
            email=email,
            password="password",
            first_name="Runtime",
            last_name="User",
            company=company,
            user_type=user_type,
            sharemundo_links_id=f"linksuid_{email.split('@')[0]}",
        )

    def _create_company(self, slug, name):
        owner = self._create_user(f"owner-{slug}@example.com")
        company = CompanyProfile.objects.create(
            sharemundo_links_id=f"linkscid_{slug}",
            company_name=name,
            company_address="Address",
            post_code="1000",
            company_town="Town",
            company_country="Country",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=owner.email,
            company_tax_reference="tax",
            company_iban="iban",
            admin_user=owner,
        )
        owner.company = company
        owner.save(update_fields=["company"])
        return company, owner

    def _get(self, solution=None):
        params = {"solution": solution} if solution else {}
        return self.client.get(self.endpoint, params)

    def test_missing_solution_uses_energy_consultant_defaults(self):
        self.client.force_authenticate(self.user)

        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["solution"], "energy_consultant_solution")
        self.assertEqual(
            response.data["registry_version"],
            DISPLAY_FEATURE_REGISTRY_VERSION,
        )
        self.assertEqual(
            response.data["schema_major"], DISPLAY_FEATURE_SCHEMA_MAJOR
        )
        self.assertEqual(
            response.data["registry_revision"],
            DISPLAY_FEATURE_REGISTRY_REVISION,
        )
        self.assertNotIn(
            "company.dashboard",
            response.data["permission_rules"],
        )
        self.assertEqual(
            response.data["permission_rules"]["company.action_items"][0],
            {
                "scope": "company",
                "module": "action_item",
                "actions": ["view"],
                "require_all": False,
            },
        )
        self.assertEqual(
            {
                rule["module"]
                for rule in response.data["permission_rules"][
                    "company.human_resources.time_booking"
                ]
            },
            {"time_booking", "leave_request"},
        )
        self.assertEqual(
            {
                rule["module"]
                for rule in response.data["permission_rules"][
                    "company.product_services.assembly_catalog"
                ]
            },
            {"company_assembly_catalog", "aas_models"},
        )
        self.assertEqual(
            {
                rule["module"]
                for rule in response.data["permission_rules"][
                    "project.product_services.assembly_catalog"
                ]
            },
            {"project_assembly_catalog", "aas_models"},
        )
        self.assertEqual(
            response.data["permission_rules"][
                "project.product_services.assembly_catalog.tabs.aas_models"
            ][0]["module"],
            "aas_models",
        )
        visibility = response.data["effective_visibility"]
        self.assertEqual(len(visibility), 193)
        self.assertTrue(
            visibility["company.action_items.linked.dependents"]
        )
        self.assertTrue(
            visibility["project.action_items.linked.dependents"]
        )
        self.assertTrue(all(visibility.values()))

    def test_returns_solution_specific_catalogs_for_titles_and_ids(self):
        self.client.force_authenticate(self.user)
        cases = (
            ("Energy Consultant Solution", "energy_consultant_solution", 193),
            ("enterprise_solution", "enterprise_solution", 192),
            ("Core Solution", "core_solution", 102),
        )

        for requested_solution, expected_solution, expected_count in cases:
            with self.subTest(solution=requested_solution):
                response = self._get(requested_solution)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["solution"], expected_solution)
                visibility = response.data["effective_visibility"]
                self.assertEqual(len(visibility), expected_count)
                if expected_solution == "core_solution":
                    self.assertFalse(
                        any(key.startswith("project.") for key in visibility)
                    )
                    self.assertFalse(
                        any(
                            key.startswith("project.")
                            for key in response.data["permission_rules"]
                        )
                    )
                if expected_solution == "enterprise_solution":
                    self.assertNotIn("project.funding", visibility)

    def test_applies_parent_and_cross_module_dependencies(self):
        SolutionDisplayConfiguration.objects.create(
            company=self.company,
            solution="energy_consultant_solution",
            overrides={
                "company.action_items": False,
                "company.documents": False,
            },
        )
        self.client.force_authenticate(self.user)

        response = self._get("Energy Consultant Solution")

        self.assertEqual(response.status_code, 200)
        visibility = response.data["effective_visibility"]
        self.assertFalse(visibility["company.action_items.tabs.board"])
        self.assertFalse(visibility["company.action_items.linked.documents"])
        self.assertFalse(
            visibility[
                "company.product_services.machines.linked.structure"
            ]
        )
        self.assertTrue(visibility["company.calendar"])

    def test_reads_only_the_authenticated_users_company(self):
        other_company, _other_user = self._create_company(
            "runtime_company_b",
            "Runtime Company B",
        )
        SolutionDisplayConfiguration.objects.create(
            company=self.company,
            solution="energy_consultant_solution",
            overrides={"company.documents": False},
        )
        SolutionDisplayConfiguration.objects.create(
            company=other_company,
            solution="energy_consultant_solution",
            overrides={"company.action_items": False},
        )
        self.client.force_authenticate(self.user)

        response = self._get("Energy Consultant Solution")

        visibility = response.data["effective_visibility"]
        self.assertFalse(visibility["company.documents"])
        self.assertTrue(visibility["company.action_items"])

    def test_freelancer_without_company_receives_defaults(self):
        freelancer = self._create_user(
            "runtime-freelancer@example.com",
            user_type="freelancer",
        )
        self.client.force_authenticate(freelancer)

        response = self._get("Enterprise Solution")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["solution"], "enterprise_solution")
        self.assertTrue(all(response.data["effective_visibility"].values()))

    def test_invalid_solution_is_rejected(self):
        self.client.force_authenticate(self.user)

        response = self._get("unsupported")

        self.assertEqual(response.status_code, 400)
        self.assertIn("solution", response.data)

    def test_endpoint_is_authenticated_and_read_only(self):
        unauthenticated_response = self._get("Energy Consultant Solution")
        self.client.force_authenticate(self.user)
        write_response = self.client.post(
            self.endpoint,
            {"effective_visibility": {}},
            format="json",
        )

        self.assertIn(unauthenticated_response.status_code, (401, 403))
        self.assertEqual(write_response.status_code, 405)
