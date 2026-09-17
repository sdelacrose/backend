from django.test import TestCase
from rest_framework.test import APIClient
from models.models import Permission, RolePermission
from project_management.models import CompanySolutionSpaceItem
from project_management import test_catalog_recommendation as fixtures


class CatalogBatchSelectionTests(TestCase):
    _create_company = staticmethod(fixtures.CatalogRecommendationTests._create_company)
    _create_project = staticmethod(fixtures.CatalogRecommendationTests._create_project)

    def setUp(self):
        fixtures.CatalogRecommendationTests.setUp(self)
        permission, _ = Permission.objects.get_or_create(
            key="company_assembly_catalog.update",
            defaults={"name": "Update company catalog", "module": "company_assembly_catalog"},
        )
        RolePermission.objects.create(role=self.role, permission=permission)
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def test_batch_selects_both_catalog_types_and_is_repeat_safe(self):
        items = [CompanySolutionSpaceItem.objects.create(
            company_profile=self.company, assembly_name=name, assembly_type=kind
        ) for name, kind in [("Motor", "components"), ("Assembly station", "production_lines")]]
        payload = {"project": self.project.pk, "selected_ids": [row.pk for row in items]}
        for _ in range(2):
            response = self.client.post("/api/project_management/company-solution-space-items/select-for-project/", payload, format="json")
            self.assertEqual(response.status_code, 200, response.data)
        listing = self.client.get("/api/project_management/project-solution-space-items/", {"project": self.project.pk})
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.data["count"], 2)

    def test_foreign_candidate_rejects_entire_batch_without_partial_copy(self):
        own = CompanySolutionSpaceItem.objects.create(company_profile=self.company, assembly_name="Motor", assembly_type="components")
        foreign = CompanySolutionSpaceItem.objects.create(company_profile=self.other_company, assembly_name="Private motor", assembly_type="components")
        response = self.client.post("/api/project_management/company-solution-space-items/select-for-project/", {"project": self.project.pk, "selected_ids": [own.pk, foreign.pk]}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        listing = self.client.get("/api/project_management/project-solution-space-items/", {"project": self.project.pk})
        self.assertEqual(listing.data["count"], 0)

    def test_missing_company_update_permission_rejects_batch(self):
        RolePermission.objects.filter(role=self.role, permission__key="company_assembly_catalog.update").delete()
        item = CompanySolutionSpaceItem.objects.create(company_profile=self.company, assembly_name="Motor", assembly_type="components")
        response = self.client.post("/api/project_management/company-solution-space-items/select-for-project/", {"project": self.project.pk, "selected_ids": [item.pk]}, format="json")
        self.assertEqual(response.status_code, 403, response.data)
        listing = self.client.get("/api/project_management/project-solution-space-items/", {"project": self.project.pk})
        self.assertEqual(listing.data["count"], 0)
