from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from aas_integration.models import AASResourceIdentity
from models.models import Permission, Role, RolePermission, UserRole

from project_management.models import (
    CompanySolutionSpaceItem,
    ProjectSolutionSpaceItem,
    SolutionDemand,
)
from project_management import test_solution_space as fixtures


@override_settings(
    PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY=True,
    PROJECT_AI_JOB_VIEW_ENABLED=True,
    PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=True,
)
class CatalogRecommendationTests(TestCase):
    _create_company = staticmethod(fixtures.SolutionSpaceMatrixTests._create_company)
    _create_project = staticmethod(fixtures.SolutionSpaceMatrixTests._create_project)

    def setUp(self):
        fixtures.SolutionSpaceMatrixTests.setUp(self)
        self.role = Role.objects.create(
            name="Catalog reviewer",
            company=self.company,
            type="solution",
            scope="company",
        )
        for key in (
            "company_assembly_catalog.view",
            "projects.full_access",
            "documents.view",
        ):
            permission, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0]}
            )
            RolePermission.objects.create(role=self.role, permission=permission)
        UserRole.objects.create(user=self.owner, role=self.role, company=self.company)

    def test_proposal_does_not_select_catalog_or_create_requirements(self):
        client, response, candidate = self.proposal()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "review_ready")
        self.assertEqual(
            response.data["result"]["recommendations"][0]["catalog_id"], candidate.pk
        )
        recommendation = response.data["result"]["recommendations"][0]
        self.assertEqual(recommendation["catalog_revision"], str(candidate.updated_at))
        self.assertEqual(recommendation["review_category"], "needs_engineering_review")
        for endpoint in ("project-solution-space-items", "requirements"):
            listing = client.get(
                f"/api/project_management/{endpoint}/", {"project": self.project.pk}
            )
            self.assertEqual(listing.status_code, 200, listing.data)
            self.assertEqual(listing.data["count"], 0)

    def test_reselecting_existing_root_adds_missing_current_catalog_children(self):
        client, response, candidate = self.proposal()
        path = f"/api/project_management/catalog-recommendations/{response.data['id']}/confirm/"
        client.post(
            path, {"selected_ids": [candidate.pk], "acknowledged": True}, format="json"
        )
        identity = AASResourceIdentity.objects.create(
            company=self.company,
            source_type=AASResourceIdentity.SourceType.PROJECT_ASSEMBLY,
            project_assembly=ProjectSolutionSpaceItem.objects.get(
                project=self.project, source_company_item=candidate
            ),
            out_of_date=False,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=candidate,
            level=2,
            assembly_name="New approved child",
            assembly_type="components",
        )
        with patch("requests.post") as provider:
            provider.return_value.status_code = 200
            provider.return_value.json.return_value = response.data["result"]
            fresh = client.post(
                "/api/project_management/catalog-recommendations/",
                {"project": self.project.pk},
                format="json",
            )
        confirmed = client.post(
            f"/api/project_management/catalog-recommendations/{fresh.data['id']}/confirm/",
            {"selected_ids": [candidate.pk], "acknowledged": True},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        listing = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        )
        self.assertEqual(listing.data["count"], 2)
        identity.refresh_from_db()
        self.assertTrue(identity.out_of_date)

    def test_disabled_feature_hides_jobs_and_prevents_retry(self):
        client, response, _ = self.proposal()
        with self.settings(PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=False):
            listing = client.get(
                "/api/project_management/ai-jobs/", {"project": self.project.pk}
            )
            self.assertEqual(listing.data["count"], 0)
            path = f"/api/project_management/ai-jobs/catalog_recommendation:{response.data['id']}/"
            self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(
                client.post(path + "retry/", {}, format="json").status_code, 404
            )

    def test_broker_failure_is_visible_and_retryable_instead_of_stuck_queued(self):
        with (
            self.settings(
                PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY=False,
                CELERY_BROKER_URL="memory://",
            ),
            patch(
                "kombu.messaging.Producer.publish",
                side_effect=OSError("broker unavailable"),
            ),
        ):
            client, response, _ = self.proposal()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "failed")
        listing = client.get(
            "/api/project_management/ai-jobs/", {"project": self.project.pk}
        )
        self.assertTrue(listing.data["results"][0]["can_retry"])

    def proposal(self, *, child=False):
        demand = SolutionDemand.objects.create(
            project=self.project,
            subject="Battery module",
            custom_fields={"voltage": "48 V"},
        )
        candidate = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Battery module",
            assembly_type="components",
            custom_fields={"voltage": "48 V"},
        )
        if child:
            child_row = CompanySolutionSpaceItem.objects.create(
                company_profile=self.company,
                parent=candidate,
                level=2,
                assembly_name="Battery cell",
                assembly_type="components",
            )
            ProjectSolutionSpaceItem.objects.create(
                project=self.project,
                source_company_item=child_row,
                assembly_name="Existing project choice",
                custom_fields={"note": "Keep my choice"},
                assembly_type="components",
            )
        client = APIClient()
        client.force_authenticate(self.owner)
        result = {
            "recommendations": [
                {
                    "catalog_id": candidate.pk,
                    "reason": "Matching voltage",
                    "evidence": [
                        {
                            "source_type": "demand",
                            "source_id": demand.pk,
                            "quote": "48 V",
                        }
                    ],
                    "catalog_evidence": [{"source_id": candidate.pk, "quote": "48 V"}],
                    "gaps": [],
                    "conflicts": [],
                }
            ],
            "unresolved_items": [],
        }
        with patch("requests.post") as provider:
            provider.return_value.status_code = 200
            provider.return_value.json.return_value = result
            response = client.post(
                "/api/project_management/catalog-recommendations/",
                {"project": self.project.pk},
                format="json",
            )
        return client, response, candidate

    def test_confirmation_selects_catalog_once_and_repeated_confirmation_is_safe(self):
        client, response, candidate = self.proposal()
        path = f"/api/project_management/catalog-recommendations/{response.data['id']}/confirm/"
        for _ in range(2):
            confirmed = client.post(
                path,
                {"selected_ids": [candidate.pk], "acknowledged": True},
                format="json",
            )
            self.assertEqual(
                confirmed.status_code, 200, getattr(confirmed, "data", None)
            )
            self.assertEqual(confirmed.data["status"], "applied")
        listing = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        )
        self.assertEqual(listing.data["count"], 1)
        self.assertEqual(
            listing.data["results"][0]["source_company_item"], candidate.pk
        )

    def test_changed_demands_invalidate_review_without_selecting_anything(self):
        client, response, candidate = self.proposal()
        SolutionDemand.objects.create(project=self.project, subject="New product")
        confirmed = client.post(
            f"/api/project_management/catalog-recommendations/{response.data['id']}/confirm/",
            {"selected_ids": [candidate.pk], "acknowledged": True},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 409)
        self.assertEqual(confirmed.data["status"], "stale")
        listing = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        )
        self.assertEqual(listing.data["count"], 0)

    def test_confirmation_requires_acknowledgement_and_rejects_other_company_ids(self):
        client, response, candidate = self.proposal()
        other = CompanySolutionSpaceItem.objects.create(
            company_profile=self.other_company,
            assembly_name="Foreign battery",
            assembly_type="components",
        )
        path = f"/api/project_management/catalog-recommendations/{response.data['id']}/confirm/"
        for body in (
            {"selected_ids": [candidate.pk]},
            {"selected_ids": [other.pk], "acknowledged": True},
            {"selected_ids": [], "acknowledged": True},
        ):
            self.assertEqual(client.post(path, body, format="json").status_code, 400)
        listing = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        )
        self.assertEqual(listing.data["count"], 0)

    def test_other_company_cannot_read_or_confirm_run(self):
        client, response, candidate = self.proposal()
        client.force_authenticate(self.other_owner)
        path = f"/api/project_management/catalog-recommendations/{response.data['id']}/"
        self.assertEqual(client.get(path).status_code, 404)
        self.assertEqual(
            client.post(
                path + "confirm/",
                {"selected_ids": [candidate.pk], "acknowledged": True},
                format="json",
            ).status_code,
            404,
        )
        self.assertEqual(
            client.post(
                "/api/project_management/catalog-recommendations/",
                {"project": self.project.pk},
                format="json",
            ).status_code,
            404,
        )

    def test_cancelled_review_cannot_be_confirmed(self):
        client, response, candidate = self.proposal()
        path = f"/api/project_management/catalog-recommendations/{response.data['id']}/"
        self.assertEqual(
            client.post(path + "cancel/", {}, format="json").status_code, 200
        )
        self.assertEqual(
            client.post(
                path + "confirm/",
                {"selected_ids": [candidate.pk], "acknowledged": True},
                format="json",
            ).status_code,
            400,
        )

    def test_proposal_is_available_in_ai_jobs_after_leaving_catalog(self):
        client, response, _ = self.proposal()
        listing = client.get(
            "/api/project_management/ai-jobs/", {"project": self.project.pk}
        )
        self.assertEqual(listing.status_code, 200, listing.data)
        self.assertIn(
            f"catalog_recommendation:{response.data['id']}",
            [row["id"] for row in listing.data["results"]],
        )

    def test_selecting_parent_preserves_existing_project_child_selection(self):
        client, response, candidate = self.proposal(child=True)
        before = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        ).data["results"][0]
        confirmed = client.post(
            f"/api/project_management/catalog-recommendations/{response.data['id']}/confirm/",
            {"selected_ids": [candidate.pk], "acknowledged": True},
            format="json",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.data)
        listing = client.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.pk},
        )
        self.assertEqual(listing.data["count"], 2)
        child = next(
            row for row in listing.data["results"] if row["id"] == before["id"]
        )
        parent = next(
            row
            for row in listing.data["results"]
            if row["source_company_item"] == candidate.pk
        )
        self.assertEqual(child["source_company_item"], before["source_company_item"])
        self.assertEqual(child["parent"], parent["id"])
