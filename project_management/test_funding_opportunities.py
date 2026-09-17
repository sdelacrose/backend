from datetime import timedelta
import socket
from unittest.mock import Mock

import requests
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User
from project_management.models import Project, ProjectTeamMembers
from project_management.services.funding_opportunities import (
    FundingSourcePayloadError,
    FundingSourceSecurityError,
    FundingSourceUnavailable,
    OfficialFundingSourceAdapter,
    evaluate_funding_match,
    ingest_funding_opportunity,
    sync_project_funding_match,
    validate_official_source_url,
)
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_funding_save,
    notify_on_project_save,
)
from project_management.models import ProjectFunding
from project_management.views.funding_opportunity import FundingOpportunityMatchViewSet


class FundingOpportunityTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        post_save.disconnect(notify_on_project_funding_save, sender=ProjectFunding)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, create_related_objects, sender=ProjectTeamMembers)
        self.addCleanup(post_save.connect, notify_on_project_funding_save, sender=ProjectFunding)
        self.user = User.objects.create_user(email="funding-owner@example.com", password="password", user_type="company_user")
        self.company = CompanyProfile.objects.create(
            admin_user=self.user, company_name="Funding Company", company_address="Street 1",
            company_town="Town", company_tell_no="123", company_mob_no="456",
            company_web_address="https://example.com", company_email_address=self.user.email,
            company_tax_reference="tax", company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Low carbon servo production line", project_description="Efficient automation",
            country="DE", contract_value=1000, company_profile=self.company, user=self.user, created_by=self.user,
        )
        self.funding_role = Role.objects.create(
            name="Funding Opportunity Editor",
            company=self.company,
            type="solution",
            scope="project",
        )
        for permission_key in ("funding.view", "funding.update", "funding.create"):
            permission, _created = Permission.objects.get_or_create(
                key=permission_key,
                defaults={"name": permission_key, "module": "funding"},
            )
            RolePermission.objects.create(role=self.funding_role, permission=permission)
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="Funding editor",
            project_role=self.funding_role,
            project_access="RW",
            team_member=self.user,
        )
        self.factory = APIRequestFactory()

    def test_official_source_validation_rejects_private_wrong_host_and_non_https_urls(self):
        self.assertEqual(
            validate_official_source_url("https://funding.example.gov/program/1", ["funding.example.gov"]),
            "https://funding.example.gov/program/1",
        )
        for url in ("http://funding.example.gov/program/1", "https://127.0.0.1/secret", "https://evil.example/program"):
            with self.assertRaises(FundingSourceSecurityError):
                validate_official_source_url(url, ["funding.example.gov"])

    def test_matching_separates_relevance_from_eligibility_and_staleness(self):
        result = evaluate_funding_match(
            self.project,
            {
                "title": "Low carbon production automation",
                "summary": "Funding for efficient servo production lines",
                "eligibility_rules": {"regions": ["DE"], "minimum_budget": 5000},
                "retrieved_at": timezone.now() - timedelta(days=40),
            },
            freshness_days=30,
        )
        self.assertGreaterEqual(result["relevance_score"], 60)
        self.assertEqual(result["eligibility_state"], "disqualified")
        self.assertTrue(result["is_stale"])
        self.assertIn("minimum_budget", [condition["key"] for condition in result["conditions"]])

    def test_adapter_constrains_redirects_content_resolution_size_and_timeout(self):
        from project_management.models import FundingSourceConfig

        source = FundingSourceConfig.objects.create(
            authority="Example Authority", region="DE", base_url="https://funding.example.gov",
            allowed_hosts=["funding.example.gov"], is_active=True,
        )

        def response(status=200, content_type="text/html", chunks=(b"ok",)):
            result = Mock(status_code=status, headers={"Content-Type": content_type}, encoding="utf-8")
            result.iter_content.return_value = iter(chunks)
            return result

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(get=Mock(return_value=response(status=302))),
            resolver=lambda _host: ["93.184.216.34"],
        )
        with self.assertRaises(FundingSourceSecurityError):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "source_redirect_rejected")

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(get=Mock(return_value=response(content_type="application/pdf"))),
            resolver=lambda _host: ["93.184.216.34"],
        )
        with self.assertRaises(FundingSourcePayloadError):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "unsupported_content_type")

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(get=Mock(return_value=response(chunks=(b"1234", b"5678")))),
            resolver=lambda _host: ["93.184.216.34"],
            max_bytes=5,
        )
        with self.assertRaises(FundingSourcePayloadError):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "source_content_too_large")

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(get=Mock(side_effect=requests.Timeout)),
            resolver=lambda _host: ["93.184.216.34"],
        )
        with self.assertRaises(FundingSourceUnavailable):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "source_timeout")

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(get=Mock(return_value=response())),
            resolver=lambda _host: ["93.184.216.34"],
        )
        self.assertEqual(adapter.fetch("https://funding.example.gov/program/1"), "ok")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "")
        self.assertIsNotNone(source.last_success_at)

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(),
            resolver=lambda _host: ["127.0.0.1"],
        )
        with self.assertRaises(FundingSourceSecurityError):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "private_resolved_address")

        adapter = OfficialFundingSourceAdapter(
            source,
            requester=Mock(),
            resolver=Mock(side_effect=socket.gaierror),
        )
        with self.assertRaises(FundingSourceUnavailable):
            adapter.fetch("https://funding.example.gov/program/1")
        source.refresh_from_db()
        self.assertEqual(source.last_error, "source_resolution_failed")

    def test_normalization_versions_records_rejects_unsafe_evidence_and_suppresses_low_relevance(self):
        from project_management.models import FundingSourceConfig

        source = FundingSourceConfig.objects.create(
            authority="Example Authority", region="DE", base_url="https://funding.example.gov",
            allowed_hosts=["funding.example.gov"], is_active=True,
        )
        adapter = OfficialFundingSourceAdapter(source)
        base_payload = {
            "title": "Low carbon production automation",
            "summary": "Funding for efficient servo production lines",
            "source_url": "https://funding.example.gov/program/1",
            "retrieved_at": timezone.now(),
            "application_start": "2026-09-01",
            "application_end": "2026-12-31",
            "eligibility_rules": {"regions": ["DE"]},
            "evidence": [{"excerpt": "Official programme page", "source_url": "https://funding.example.gov/program/1"}],
        }
        first = ingest_funding_opportunity(source, "program-1", adapter.normalize(base_payload))
        repeated = ingest_funding_opportunity(source, "program-1", adapter.normalize(base_payload))
        self.assertEqual(first.id, repeated.id)
        self.assertEqual(first.opportunity.current_revision_id, first.id)

        malformed = {**base_payload, "application_end": "31/12/2026"}
        with self.assertRaises(FundingSourcePayloadError):
            adapter.normalize(malformed)
        unsafe = {**base_payload, "evidence": [{"excerpt": "Claim", "source_url": "https://evil.example/claim"}]}
        with self.assertRaises(FundingSourceSecurityError):
            adapter.normalize(unsafe)

        low_relevance = {**base_payload, "title": "Marine biodiversity grant", "summary": "Coastal habitat research"}
        low_revision = ingest_funding_opportunity(source, "program-2", adapter.normalize(low_relevance))
        self.assertIsNone(sync_project_funding_match(self.project, low_revision, self.user))

    @override_settings(FUNDING_OPPORTUNITY_MATCHING_ENABLED=True)
    def test_api_is_tenant_filtered_paginated_and_conversion_is_explicit(self):
        from project_management.models import FundingOpportunity, FundingOpportunityRevision, FundingSourceConfig, ProjectFundingMatch

        source = FundingSourceConfig.objects.create(
            authority="Example Authority", region="DE", base_url="https://funding.example.gov",
            allowed_hosts=["funding.example.gov"], is_active=True,
        )
        opportunity = FundingOpportunity.objects.create(source=source, external_id="program-1")
        revision = FundingOpportunityRevision.objects.create(
            opportunity=opportunity, source_url="https://funding.example.gov/program/1",
            retrieved_at=timezone.now(), title="Low carbon production automation",
            summary="Funding for efficient servo production lines", application_start=None,
            application_end=None, eligibility_rules={"regions": ["DE"]}, evidence=[{"excerpt":"official evidence"}],
            content_hash="revision-hash",
        )
        opportunity.current_revision = revision
        opportunity.save(update_fields=["current_revision"])
        match = ProjectFundingMatch.objects.create(
            project=self.project, revision=revision, relevance_score=85,
            eligibility_state="unknown", match_reasons=["low carbon"],
            eligibility_conditions=[{"key":"organization_type","state":"unknown"}],
        )
        other_user = User.objects.create_user(
            email="other-funding-owner@example.com",
            password="password",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_user, company_name="Other Funding Company", company_address="Street 2",
            company_town="Town", company_tell_no="321", company_mob_no="654",
            company_web_address="https://other.example.com", company_email_address=other_user.email,
            company_tax_reference="other-tax", company_iban="other-iban",
        )
        other_user.company = other_company
        other_user.save(update_fields=["company"])
        other_project = Project.objects.create(
            project_name="Other tenant project", country="DE", contract_value=1000,
            company_profile=other_company, user=other_user, created_by=other_user,
        )
        ProjectFundingMatch.objects.create(
            project=other_project, revision=revision, relevance_score=99,
            eligibility_state="eligible", match_reasons=["production"],
        )
        list_view = FundingOpportunityMatchViewSet.as_view({"get": "list"})
        request = self.factory.get("/api/project_management/funding-opportunity-matches/", {"project": self.project.id, "page_size": 1})
        force_authenticate(request, self.user)
        response = list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertFalse(response.data["results"][0]["eligible"])

        update_view = FundingOpportunityMatchViewSet.as_view({"patch": "partial_update"})
        request = self.factory.patch(
            f"/api/project_management/funding-opportunity-matches/{match.id}/",
            {"status": "saved"},
            format="json",
        )
        force_authenticate(request, self.user)
        updated = update_view(request, pk=match.id)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["status"], "saved")

        convert_view = FundingOpportunityMatchViewSet.as_view({"post": "convert"})
        request = self.factory.post(f"/api/project_management/funding-opportunity-matches/{match.id}/convert/", {}, format="json")
        force_authenticate(request, self.user)
        converted = convert_view(request, pk=match.id)
        self.assertEqual(converted.status_code, 201)
        match.refresh_from_db()
        self.assertEqual(match.status, "converted")
        self.assertEqual(match.converted_funding.name, revision.title)

        request = self.factory.post(f"/api/project_management/funding-opportunity-matches/{match.id}/convert/", {}, format="json")
        force_authenticate(request, self.user)
        repeated = convert_view(request, pk=match.id)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.data["converted_funding"], match.converted_funding_id)

    @override_settings(FUNDING_OPPORTUNITY_MATCHING_ENABLED=True)
    def test_api_reuses_funding_permissions_for_status_changes_and_conversion(self):
        from project_management.models import FundingOpportunity, FundingOpportunityRevision, FundingSourceConfig, ProjectFundingMatch

        viewer = User.objects.create_user(
            email="funding-viewer@example.com",
            password="password",
            user_type="company_user",
            company=self.company,
        )
        viewer_role = Role.objects.create(
            name="Funding Opportunity Viewer",
            company=self.company,
            type="solution",
            scope="project",
        )
        view_permission, _created = Permission.objects.get_or_create(
            key="funding.view",
            defaults={"name": "funding.view", "module": "funding"},
        )
        RolePermission.objects.create(role=viewer_role, permission=view_permission)
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="Funding viewer",
            project_role=viewer_role,
            project_access="RO",
            team_member=viewer,
        )
        source = FundingSourceConfig.objects.create(
            authority="Permission Authority",
            region="DE",
            base_url="https://funding.example.gov",
            allowed_hosts=["funding.example.gov"],
            is_active=True,
        )
        opportunity = FundingOpportunity.objects.create(source=source, external_id="permission-program")
        revision = FundingOpportunityRevision.objects.create(
            opportunity=opportunity,
            source_url="https://funding.example.gov/program/permission",
            retrieved_at=timezone.now(),
            title="Permission tested programme",
            summary="Funding for efficient production automation",
            eligibility_rules={"regions": ["DE"]},
            evidence=[],
            content_hash="permission-revision",
        )
        opportunity.current_revision = revision
        opportunity.save(update_fields=["current_revision"])
        match = ProjectFundingMatch.objects.create(
            project=self.project,
            revision=revision,
            relevance_score=85,
            eligibility_state="unknown",
        )

        list_view = FundingOpportunityMatchViewSet.as_view({"get": "list"})
        request = self.factory.get(
            "/api/project_management/funding-opportunity-matches/",
            {"project": self.project.id},
        )
        force_authenticate(request, viewer)
        self.assertEqual(list_view(request).status_code, 200)

        update_view = FundingOpportunityMatchViewSet.as_view({"patch": "partial_update"})
        request = self.factory.patch(
            f"/api/project_management/funding-opportunity-matches/{match.id}/",
            {"status": "saved"},
            format="json",
        )
        force_authenticate(request, viewer)
        self.assertEqual(update_view(request, pk=match.id).status_code, 403)

        convert_view = FundingOpportunityMatchViewSet.as_view({"post": "convert"})
        request = self.factory.post(
            f"/api/project_management/funding-opportunity-matches/{match.id}/convert/",
            {},
            format="json",
        )
        force_authenticate(request, viewer)
        self.assertEqual(convert_view(request, pk=match.id).status_code, 403)

    def test_api_is_default_off(self):
        view = FundingOpportunityMatchViewSet.as_view({"get": "list"})
        request = self.factory.get(
            "/api/project_management/funding-opportunity-matches/",
            {"project": self.project.id},
        )
        force_authenticate(request, self.user)
        response = view(request)
        self.assertEqual(response.status_code, 404)
