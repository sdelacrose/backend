from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch

from models.models import CompanyProfile, Permission, Role, RolePermission, User
from project_management.models import (
    ProcessAssemblyExtractionRun,
    ProcessAttributeExtractionRun,
    ProductAssemblyExtractionRun,
    ProductAttributeExtractionRun,
    Project,
    ProjectTeamMembers,
    Milestone,
)
from project_management.services.ai_jobs import adapt_ai_run, list_project_ai_jobs
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
)
from project_management.views.ai_jobs import ProjectAIJobViewSet
from project_content.models import (
    AcceptanceGenerationRun,
    GeneratedProjectDocument,
    ProjectDocumentGenerationRun,
)


class ProjectAIJobTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, create_related_objects, sender=ProjectTeamMembers)
        self.user = User.objects.create_user(
            email="ai-job-owner@example.com",
            password="password",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="AI Job Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Durable AI Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.factory = APIRequestFactory()

    @override_settings(PROJECT_AI_JOB_VIEW_ENABLED=True)
    def test_phase_bound_acceptance_api_links_to_requirements_workflow(self):
        role = Role.objects.create(
            name="Acceptance reviewer", company=self.company,
            type="solution", scope="project",
        )
        for key in ("action_item.create", "action_item.generate_acceptance_tests",
                    "phases_management.view"):
            permission, _ = Permission.objects.get_or_create(
                key=key, defaults={"name": key, "module": key.split(".")[0]},
            )
            RolePermission.objects.create(role=role, permission=permission)
        ProjectTeamMembers.objects.create(
            project=self.project, team_member=self.user,
            project_role=role, project_access="RW",
        )
        phase = Milestone(
            project=self.project, name="FAT",
            start_date="2026-10-05", due_date="2026-10-11", sort_order=1,
        )
        # Seed scheduling data without sending fixture notifications.
        Milestone.objects.bulk_create([phase])
        run = AcceptanceGenerationRun.objects.create(
            project=self.project, created_by=self.user, schema_version="2",
            fat_phase=phase, source_hash="reviewed-battery", result={"proposals": []},
        )
        list_view = ProjectAIJobViewSet.as_view({"get": "list"})
        detail_view = ProjectAIJobViewSet.as_view({"get": "retrieve"})
        for state in ("generating", "review_ready", "applied"):
            with self.subTest(state=state):
                run.status = state
                run.save(update_fields=["status"])
                request = self.factory.get("/api/project_management/ai-jobs/",
                                           {"project": self.project.pk})
                force_authenticate(request, self.user)
                response = list_view(request)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.data["results"]), 1)
                job = response.data["results"][0]
                self.assertEqual(job["initiating_url"],
                                 "/project-system-requirements/technical-requirements")
                self.assertEqual(job["result_url"],
                                 "/project-system-requirements/technical-requirements")
                detail = self.factory.get("/api/project_management/ai-jobs/detail/")
                force_authenticate(detail, self.user)
                self.assertEqual(detail_view(detail, pk=job["id"]).data["result_url"],
                                 "/project-system-requirements/technical-requirements")

    def create_runs(self):
        return [
            ProductAssemblyExtractionRun.objects.create(
                project=self.project,
                status="queued",
                source_snapshot_hash="a" * 64,
                created_by=self.user,
            ),
            ProductAttributeExtractionRun.objects.create(
                project=self.project,
                status="extracting",
                source_snapshot_hash="b" * 64,
                matrix_snapshot_hash="c" * 64,
                created_by=self.user,
            ),
            ProcessAssemblyExtractionRun.objects.create(
                project=self.project,
                status="review_ready",
                input_snapshot_hash="d" * 64,
                created_by=self.user,
            ),
            ProcessAttributeExtractionRun.objects.create(
                project=self.project,
                status="applied",
                input_snapshot_hash="e" * 64,
                created_by=self.user,
            ),
        ]

    def test_adapter_normalizes_every_extraction_run_type_and_state(self):
        runs = self.create_runs()
        jobs = [adapt_ai_run(run) for run in runs]

        self.assertEqual(
            [job["state"] for job in jobs],
            ["queued", "running", "review_ready", "completed"],
        )
        self.assertEqual(
            [job["kind"] for job in jobs],
            [
                "product_assembly",
                "product_attribute",
                "process_assembly",
                "process_attribute",
            ],
        )
        self.assertTrue(all(job["initiating_url"] for job in jobs))
        self.assertTrue(all(job["result_url"] for job in jobs))
        self.assertFalse(any(job["can_retry"] for job in jobs))
        self.assertFalse(any(job["can_cancel"] for job in jobs))

        runs[0].status = "failed"
        runs[0].error_code = "provider_error"
        runs[0].error_message = "Provider was unavailable"
        runs[0].save()
        failed = adapt_ai_run(runs[0])
        self.assertTrue(failed["can_retry"])
        self.assertIn("retry", failed["retry_url"])
        self.assertEqual(failed["error"]["code"], "provider_error")

    def test_list_deduplicates_active_kind_and_keeps_history(self):
        old = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status="extracting",
            source_snapshot_hash="f" * 64,
            created_by=self.user,
        )
        latest = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status="review_ready",
            source_snapshot_hash="g" * 64,
            created_by=self.user,
        )
        completed = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status="applied",
            source_snapshot_hash="h" * 64,
            created_by=self.user,
        )

        active = list_project_ai_jobs(self.user, self.project.id, active_only=True)
        history = list_project_ai_jobs(self.user, self.project.id, active_only=False)

        self.assertEqual([job["id"] for job in active], [f"product_assembly:{latest.id}"])
        self.assertEqual(len(history), 3)
        self.assertIn(f"product_assembly:{old.id}", [job["id"] for job in history])
        self.assertIn(f"product_assembly:{completed.id}", [job["id"] for job in history])

    def test_adapter_includes_document_and_acceptance_generation_runs(self):
        generated = GeneratedProjectDocument.objects.create(
            project=self.project,
            kind="requirements_specification",
        )
        technical = ProjectDocumentGenerationRun.objects.create(
            project=self.project,
            generated_document=generated,
            kind="requirements_specification",
            status="generating",
            source_hash="technical-hash",
            created_by=self.user,
        )
        acceptance = AcceptanceGenerationRun.objects.create(
            project=self.project,
            status="applied",
            source_hash="acceptance-hash",
            created_by=self.user,
        )

        self.assertEqual(adapt_ai_run(technical)["state"], "running")
        self.assertEqual(adapt_ai_run(technical)["label"], "Technical Specification generation")
        self.assertEqual(adapt_ai_run(acceptance)["state"], "completed")
        self.assertEqual(
            adapt_ai_run(acceptance)["result_url"],
            "/project-execution/acceptance-tests",
        )

    @override_settings(PROJECT_AI_JOB_VIEW_ENABLED=True)
    @patch("project_management.services.ai_jobs.has_ai_job_access", return_value=True)
    def test_api_is_paginated_and_tenant_filtered(self, _access):
        self.create_runs()
        outsider = User.objects.create_user(
            email="ai-job-outsider@example.com",
            password="password",
            user_type="company_user",
        )
        list_view = ProjectAIJobViewSet.as_view({"get": "list"})

        request = self.factory.get(
            "/api/project_management/ai-jobs/",
            {"project": self.project.id, "page": 1, "page_size": 2},
        )
        force_authenticate(request, self.user)
        response = list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertTrue(response.data["next"])

        request = self.factory.get(
            "/api/project_management/ai-jobs/", {"project": self.project.id}
        )
        force_authenticate(request, outsider)
        forbidden = list_view(request)
        self.assertEqual(forbidden.status_code, 404)

    @override_settings(PROJECT_AI_JOB_VIEW_ENABLED=True)
    @patch("project_management.services.ai_jobs.has_ai_job_access", return_value=True)
    def test_retry_returns_existing_active_replacement_idempotently(self, _access):
        failed = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status="failed",
            source_snapshot_hash="i" * 64,
            created_by=self.user,
        )
        active = ProductAssemblyExtractionRun.objects.create(
            project=self.project,
            status="extracting",
            source_snapshot_hash="j" * 64,
            created_by=self.user,
        )
        retry_view = ProjectAIJobViewSet.as_view({"post": "retry"})
        request = self.factory.post(
            f"/api/project_management/ai-jobs/product_assembly:{failed.id}/retry/",
            {},
            format="json",
        )
        force_authenticate(request, self.user)
        response = retry_view(request, pk=f"product_assembly:{failed.id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], f"product_assembly:{active.id}")
        self.assertTrue(response.data["reused"])

    @override_settings(PROJECT_AI_JOB_VIEW_ENABLED=True)
    @patch("project_management.services.ai_jobs.has_ai_job_access", return_value=True)
    def test_retry_reports_current_input_precondition_without_server_error(self, _access):
        failed = ProductAttributeExtractionRun.objects.create(
            project=self.project,
            status="failed",
            source_snapshot_hash="k" * 64,
            matrix_snapshot_hash="l" * 64,
            created_by=self.user,
        )
        retry_view = ProjectAIJobViewSet.as_view({"post": "retry"})
        request = self.factory.post(
            f"/api/project_management/ai-jobs/product_attribute:{failed.id}/retry/",
            {},
            format="json",
        )
        force_authenticate(request, self.user)

        response = retry_view(request, pk=f"product_attribute:{failed.id}")

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.data,
            {
                "error": {
                    "code": "no_customer_demands",
                    "message": (
                        "Create or generate Customer Demands before extracting "
                        "product attributes."
                    ),
                }
            },
        )

    @override_settings(PROJECT_AI_JOB_VIEW_ENABLED=True)
    @patch("project_management.services.ai_jobs.has_ai_job_access")
    def test_api_filters_jobs_and_retry_by_originating_module_permissions(self, access):
        product, _, process, _ = self.create_runs()
        product.status = "failed"
        product.save(update_fields=["status", "updated_at"])
        access.side_effect = lambda _request, adapter, _run, write=False: (
            adapter.kind == "process_assembly" and not write
        )
        list_view = ProjectAIJobViewSet.as_view({"get": "list"})
        request = self.factory.get(
            "/api/project_management/ai-jobs/",
            {"project": self.project.id},
        )
        force_authenticate(request, self.user)
        response = list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [job["id"] for job in response.data["results"]],
            [f"process_assembly:{process.id}"],
        )

        retry_view = ProjectAIJobViewSet.as_view({"post": "retry"})
        request = self.factory.post(
            f"/api/project_management/ai-jobs/product_assembly:{product.id}/retry/",
            {},
            format="json",
        )
        force_authenticate(request, self.user)
        retry = retry_view(request, pk=f"product_assembly:{product.id}")
        self.assertEqual(retry.status_code, 404)
