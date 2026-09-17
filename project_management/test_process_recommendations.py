from decimal import Decimal
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.models import (
    CompanySolutionSpaceItem,
    ProcessAssemblyRecommendationRun,
    Project,
    ProjectTeamMembers,
    Requirement,
)
from project_management.services.process_recommendation import (
    RecommendationCandidate,
    create_process_recommendation_run,
    decide_automatic_selection,
    override_process_recommendation,
    rank_process_assemblies,
)
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
)
from project_management.views.process_recommendation import ProcessAssemblyRecommendationRunViewSet


class ProcessRecommendationPolicyTests(TestCase):
    def candidate(self, score, *, grounded=True, conflicts=None, item_id=1):
        return RecommendationCandidate(
            process_assembly_id=item_id,
            name=f"Candidate {item_id}",
            score=Decimal(str(score)),
            evidence=[{"requirement": 1, "matched_tokens": ["servo"]}],
            missing_evidence=[],
            grounded_decisive_evidence=grounded,
            conflicts=conflicts or [],
        )

    def test_threshold_and_margin_boundaries_are_deterministic(self):
        at_boundary = decide_automatic_selection(
            [self.candidate("0.85"), self.candidate("0.70", item_id=2)],
            minimum_score=Decimal("0.85"),
            minimum_margin=Decimal("0.15"),
        )
        self.assertTrue(at_boundary.automatic)

        below_score = decide_automatic_selection(
            [self.candidate("0.849"), self.candidate("0.60", item_id=2)],
            minimum_score=Decimal("0.85"),
            minimum_margin=Decimal("0.15"),
        )
        self.assertFalse(below_score.automatic)
        self.assertEqual(below_score.reason, "below_score_threshold")

        below_margin = decide_automatic_selection(
            [self.candidate("0.90"), self.candidate("0.751", item_id=2)],
            minimum_score=Decimal("0.85"),
            minimum_margin=Decimal("0.15"),
        )
        self.assertFalse(below_margin.automatic)
        self.assertEqual(below_margin.reason, "ambiguous_margin")

    def test_missing_grounding_conflicts_ties_and_empty_candidates_require_review(self):
        self.assertEqual(decide_automatic_selection([]).reason, "no_candidates")
        self.assertEqual(
            decide_automatic_selection(
                [self.candidate("0.95", grounded=False)],
            ).reason,
            "missing_decisive_evidence",
        )
        self.assertEqual(
            decide_automatic_selection(
                [self.candidate("0.95", conflicts=["voltage mismatch"])],
            ).reason,
            "hard_conflict",
        )
        self.assertEqual(
            decide_automatic_selection(
                [self.candidate("0.90"), self.candidate("0.90", item_id=2)],
            ).reason,
            "ambiguous_margin",
        )


class ProcessRecommendationServiceTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_related_objects,
            sender=ProjectTeamMembers,
        )
        self.user = User.objects.create_user(
            email="recommend-owner@example.com",
            password="password",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Recommendation Company",
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
            project_name="Recommendation Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.requirement = Requirement.objects.create(
            project=self.project,
            name="High speed servo packaging",
            skill="servo packaging",
            status="closed",
            created_by=self.user,
        )
        self.best = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="High speed servo packaging",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            required_skills=["servo packaging"],
        )
        self.other = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Manual welding station",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        self.factory = APIRequestFactory()

    def test_ranking_uses_reviewed_project_evidence_and_never_crosses_tenants(self):
        foreign_user = User.objects.create_user(
            email="foreign-recommend@example.com",
            password="password",
            user_type="company_user",
        )
        foreign_company = CompanyProfile.objects.create(
            admin_user=foreign_user,
            company_name="Foreign Recommendation Company",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="789",
            company_mob_no="987",
            company_web_address="https://foreign.example.com",
            company_email_address=foreign_user.email,
            company_tax_reference="foreign-tax",
            company_iban="foreign-iban",
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=foreign_company,
            assembly_name="High speed servo packaging secret",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )

        candidates, input_hash = rank_process_assemblies(self.project)

        self.assertTrue(input_hash)
        self.assertEqual(candidates[0].process_assembly_id, self.best.id)
        self.assertNotIn("secret", str(candidates).lower())
        self.assertTrue(candidates[0].grounded_decisive_evidence)
        self.assertEqual(candidates[0].evidence[0]["requirement"], self.requirement.id)

    def test_ranking_requires_grounding_for_every_reviewed_requirement(self):
        missing = Requirement.objects.create(
            project=self.project,
            name="Laser guarding certification",
            skill="laser guarding",
            status="approved",
            created_by=self.user,
        )

        candidates, _ = rank_process_assemblies(self.project)
        best = next(candidate for candidate in candidates if candidate.process_assembly_id == self.best.id)

        self.assertFalse(best.grounded_decisive_evidence)
        self.assertEqual(
            best.missing_evidence,
            [{"requirement": missing.id, "requirement_name": missing.name}],
        )

    def test_automatic_selection_is_idempotent_and_manual_override_is_audited(self):
        strong = RecommendationCandidate(
            process_assembly_id=self.best.id,
            name=self.best.assembly_name,
            score=Decimal("0.95"),
            evidence=[{"requirement": self.requirement.id, "matched_tokens": ["servo"]}],
            missing_evidence=[],
            grounded_decisive_evidence=True,
            conflicts=[],
        )
        weak = RecommendationCandidate(
            process_assembly_id=self.other.id,
            name=self.other.assembly_name,
            score=Decimal("0.50"),
            evidence=[],
            missing_evidence=[
                {"requirement": self.requirement.id, "requirement_name": self.requirement.name},
            ],
            grounded_decisive_evidence=False,
            conflicts=[],
        )
        with override_settings(
            AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=True,
            AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_TENANT_IDS=[self.company.id],
            PROCESS_RECOMMENDATION_MINIMUM_SCORE="0.85",
            PROCESS_RECOMMENDATION_MINIMUM_MARGIN="0.15",
        ), patch(
            "project_management.services.process_recommendation.rank_process_assemblies",
            return_value=([strong, weak], "stable-input-hash"),
        ):
            run = create_process_recommendation_run(self.project, self.user)
            retry = create_process_recommendation_run(self.project, self.user)

        self.assertEqual(run.id, retry.id)
        self.assertEqual(run.selection_outcome, ProcessAssemblyRecommendationRun.OUTCOME_AUTOMATIC)
        self.assertEqual(run.selected_candidate_id, self.best.id)
        self.assertIsNotNone(run.created_workstation_id)
        self.assertEqual(self.project.project_workstations.count(), 1)

        overridden = override_process_recommendation(
            run=run,
            candidate_id=self.other.id,
            actor=self.user,
            reason="Client selected the welding alternative",
        )
        self.assertEqual(overridden.selection_outcome, ProcessAssemblyRecommendationRun.OUTCOME_MANUAL)
        self.assertEqual(overridden.selected_candidate_id, self.other.id)
        self.assertEqual(overridden.override_history[-1]["previous_candidate"], self.best.id)
        self.assertEqual(overridden.override_history[-1]["reason"], "Client selected the welding alternative")
        self.assertEqual(self.project.project_workstations.filter(status="active").count(), 1)
        self.assertEqual(self.project.project_workstations.filter(status="archived").count(), 1)
        self.assertEqual(
            self.project.project_workstations.get(status="archived").source_process_assembly_id,
            self.best.id,
        )

    @override_settings(
        AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=True,
        AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_TENANT_IDS=[],
    )
    def test_tenant_policy_keeps_a_strong_match_reviewable(self):
        strong = RecommendationCandidate(
            process_assembly_id=self.best.id,
            name=self.best.assembly_name,
            score=Decimal("0.95"),
            evidence=[{"requirement": self.requirement.id, "matched_tokens": ["servo"]}],
            missing_evidence=[],
            grounded_decisive_evidence=True,
            conflicts=[],
        )
        with patch(
            "project_management.services.process_recommendation.rank_process_assemblies",
            return_value=([strong], "tenant-policy-input-hash"),
        ):
            run = create_process_recommendation_run(self.project, self.user)
        self.assertEqual(run.selection_outcome, ProcessAssemblyRecommendationRun.OUTCOME_REVIEW)
        self.assertEqual(run.decision_reason, "tenant_policy_disabled")
        self.assertFalse(run.policy["tenant_policy_enabled"])
        self.assertIsNone(run.created_workstation_id)

    @override_settings(
        PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=True,
        AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED=True,
    )
    @patch.object(ProcessAssemblyRecommendationRunViewSet, "get_permissions", return_value=[])
    def test_api_creates_and_lists_a_reviewable_run_and_audits_manual_selection(
        self,
        _permissions,
    ):
        create_view = ProcessAssemblyRecommendationRunViewSet.as_view({"post": "create"})
        request = self.factory.post(
            "/api/project_management/process-assembly-recommendations/",
            {"project": self.project.id},
            format="json",
        )
        force_authenticate(request, self.user)
        response = create_view(request)
        self.assertEqual(response.status_code, 201)
        self.assertGreaterEqual(len(response.data["ranked_candidates"]), 2)

        select_view = ProcessAssemblyRecommendationRunViewSet.as_view({"post": "select"})
        request = self.factory.post(
            f"/api/project_management/process-assembly-recommendations/{response.data['id']}/select/",
            {"candidate": self.other.id, "reason": "Manual engineering choice"},
            format="json",
        )
        force_authenticate(request, self.user)
        selected = select_view(request, pk=response.data["id"])
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.data["selected_candidate"], self.other.id)
        self.assertEqual(selected.data["selection_outcome"], "manual")
