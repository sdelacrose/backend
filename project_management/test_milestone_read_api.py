from datetime import date
from unittest.mock import patch

from django.db.models.signals import m2m_changed, post_save
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.models import ActionItem, ECPMPConfig, Milestone, Project
from project_management.serializers import MilestoneDetailSerializer, MilestoneListSerializer
from project_management.services.milestone_queries import MilestoneQueryService
from project_management.signals import (
    action_item_assignees_post_save_operations,
    action_item_post_save_operations,
    create_default_folders,
    milestone_post_save_operations,
    notify_on_milestone_save,
    notify_on_project_save,
)
from project_management.views.milestone import MilestoneViewSet


class MilestoneReadApiTests(TestCase):
    def setUp(self):
        for signal, sender in (
            (action_item_post_save_operations, ActionItem),
            (create_default_folders, Project),
            (notify_on_project_save, Project),
            (milestone_post_save_operations, Milestone),
            (notify_on_milestone_save, Milestone),
        ):
            post_save.disconnect(signal, sender=sender)
            self.addCleanup(post_save.connect, signal, sender=sender)
        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )

        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="phase-read@example.com",
            password="password",
            first_name="Phase",
            last_name="Reader",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Phase Read Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="phase-read@example.com",
            company_tax_reference="tax-phase-read",
            company_iban="iban-phase-read",
        )
        User.objects.filter(pk=self.user.pk).update(company=self.company)
        self.user.refresh_from_db()
        self.project = Project.objects.create(
            project_name="Phase Read Project",
            solution="Energy Consultant Solution",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
            project_start_date=date(2026, 1, 1),
            project_end_date=date(2026, 12, 31),
        )
        ECPMPConfig.objects.create(
            project=self.project,
            status_options=["open:#00ff00", "closed:#999999"],
        )

    def create_phase(self, name, *, status="open"):
        return Milestone.objects.create(
            project=self.project,
            name=name,
            status=status,
            start_date=date(2026, 1, 1),
            due_date=date(2026, 6, 30),
        )

    def test_projection_exposes_counts_and_flags_without_nested_actions(self):
        phase = self.create_phase("Design")
        parent = ActionItem.objects.create(
            milestone=phase,
            created_by=self.user,
            name="Parent",
            status="closed",
            target_date=date(2026, 2, 1),
        )
        ActionItem.objects.create(
            parent_action_item=parent,
            created_by=self.user,
            name="Open child",
            status="open",
            target_date=date(2026, 2, 1),
        )

        projected = list(MilestoneQueryService.with_read_projection(Milestone.objects.all()))
        with self.assertNumQueries(0):
            data = MilestoneListSerializer(projected, many=True).data

        self.assertEqual(data[0]["num_action_items"], 1)
        self.assertEqual(data[0]["num_sub_action_items"], 1)
        self.assertTrue(data[0]["has_open_action_items"])
        self.assertFalse(data[0]["all_closed"])
        self.assertNotIn("action_items", data[0])

    def test_detail_serializer_keeps_edit_fields_without_nested_actions(self):
        phase = self.create_phase("Detail")
        projected = MilestoneQueryService.with_read_projection(
            Milestone.objects.filter(pk=phase.pk), detail=True
        ).get()

        with self.assertNumQueries(0):
            data = MilestoneDetailSerializer(projected).data

        self.assertEqual(data["project"]["id"], self.project.id)
        self.assertIn("linked_folder_details", data)
        self.assertNotIn("action_items", data)

    def test_list_paginates_querysets_before_serialization(self):
        for index in range(12):
            self.create_phase(f"Phase {index:02d}")
        request = self.factory.get(
            "/milestones/",
            {"project": self.project.id, "page": 2, "page_size": 10},
        )
        force_authenticate(request, user=self.user)
        view = MilestoneViewSet.as_view({"get": "list"})

        with (
            patch(
                "project_management.views.milestone.require_any_permission_multiple_modules",
                return_value=True,
            ),
            patch(
                "project_management.permissions.check_company_project_permission",
                return_value=True,
            ),
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 12)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertNotIn("action_items", response.data["results"][0])

    def test_timeline_returns_complete_lightweight_phase_list(self):
        self.create_phase("Design")
        self.create_phase("Delivery")
        request = self.factory.get(
            "/milestones/timeline/",
            {"project": self.project.id},
        )
        force_authenticate(request, user=self.user)
        view = MilestoneViewSet.as_view({"get": "timeline"})

        with (
            patch(
                "project_management.views.milestone.require_any_permission_multiple_modules",
                return_value=True,
            ),
            patch(
                "project_management.permissions.check_company_project_permission",
                return_value=True,
            ),
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([phase["name"] for phase in response.data], ["Design", "Delivery"])
        self.assertEqual(
            set(response.data[0]),
            {"id", "name", "colour", "status", "sort_order", "start_date", "due_date"},
        )
