from decimal import Decimal
from unittest.mock import Mock

from django.test import TestCase
from django.db.models.signals import post_save

from models.models import CompanyProfile, User
from project_management.models import Milestone, Project
from project_management.serializers.project import (
    PROJECT_BUDGET_BELOW_PHASES_WARNING,
    ProjectSerializer,
)
from project_management.signals import create_default_folders, notify_on_project_save
from project_management.views.milestone import MilestoneViewSet


class ProjectBudgetWarningTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.user = User.objects.create_user(
            email="project-budget-warning@example.com",
            password="password",
            first_name="Budget",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Budget Warning Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax-budget-warning",
            company_iban="iban-budget-warning",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Budget Warning Project",
            contract_value=Decimal("1000.00"),
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        Milestone.objects.bulk_create(
            [
                Milestone(
                    project=self.project,
                    name="Phase 1",
                    allocated_budget=500,
                    due_date="2026-08-01",
                ),
                Milestone(
                    project=self.project,
                    name="Phase 2",
                    allocated_budget=400,
                    due_date="2026-08-02",
                ),
            ]
        )

    def _update_budget(self, value):
        serializer = ProjectSerializer(
            self.project,
            data={"contract_value": value},
            partial=True,
            context={"request": Mock(user=self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.project.refresh_from_db()
        return getattr(serializer, "_response_warnings", [])

    def test_reduction_below_phase_total_saves_and_warns(self):
        warnings = self._update_budget("800.00")

        self.assertEqual(self.project.contract_value, Decimal("800.00"))
        self.assertEqual(
            warnings[0]["code"],
            PROJECT_BUDGET_BELOW_PHASES_WARNING,
        )

    def test_reduction_equal_to_phase_total_does_not_warn(self):
        warnings = self._update_budget("900.00")

        self.assertEqual(warnings, [])

    def test_reduction_above_phase_total_does_not_warn(self):
        warnings = self._update_budget("950.00")

        self.assertEqual(warnings, [])

    def test_further_reduction_while_already_over_budget_warns(self):
        self.project.contract_value = Decimal("850.00")
        self.project.save(update_fields=["contract_value"])

        warnings = self._update_budget("800.00")

        self.assertEqual(
            warnings[0]["code"],
            PROJECT_BUDGET_BELOW_PHASES_WARNING,
        )

    def test_increase_while_still_below_phase_total_warns(self):
        self.project.contract_value = Decimal("700.00")
        self.project.save(update_fields=["contract_value"])

        warnings = self._update_budget("800.00")

        self.assertEqual(
            warnings[0]["code"],
            PROJECT_BUDGET_BELOW_PHASES_WARNING,
        )

    def test_unchanged_budget_below_phase_total_does_not_warn(self):
        self.project.contract_value = Decimal("800.00")
        self.project.save(update_fields=["contract_value"])

        warnings = self._update_budget("800.00")

        self.assertEqual(warnings, [])

    def test_phase_budget_sum_returns_all_matching_phases(self):
        queryset = self.project.milestones.all()
        view = MilestoneViewSet()
        view.get_queryset = Mock(return_value=queryset)
        view.filter_queryset = Mock(return_value=queryset)

        response = view.budget_sum(Mock())

        self.assertEqual(response.data["total_budget"], 900)

    def test_non_budget_update_does_not_warn(self):
        self.project.contract_value = Decimal("800.00")
        self.project.save(update_fields=["contract_value"])
        serializer = ProjectSerializer(
            self.project,
            data={"project_description": "Updated"},
            partial=True,
            context={"request": Mock(user=self.user)},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()

        self.assertEqual(getattr(serializer, "_response_warnings", []), [])
