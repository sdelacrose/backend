from datetime import date
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import User
from project_management.models import ECPMPConfig, Milestone, Project
from project_management.permissions import PhasePermission
from project_management.serializers.milestone import MilestoneSerializer
from project_management.signals import (
    milestone_post_save_operations,
    notify_on_milestone_save,
)
from project_management.views.milestone import MilestoneViewSet


class MilestoneOrderingTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User(
            id=999999,
            email="phase-ordering@example.com",
        )
        post_save.disconnect(milestone_post_save_operations, sender=Milestone)
        self.addCleanup(
            post_save.connect,
            milestone_post_save_operations,
            sender=Milestone,
        )
        post_save.disconnect(notify_on_milestone_save, sender=Milestone)
        self.addCleanup(
            post_save.connect,
            notify_on_milestone_save,
            sender=Milestone,
        )

        self.project = Project(
            project_name="Ordered phases",
            contract_value=0,
        )
        self.other_project = Project(
            project_name="Other ordered phases",
            contract_value=0,
        )
        Project.objects.bulk_create([self.project, self.other_project])
        ECPMPConfig.objects.bulk_create(
            [
                ECPMPConfig(project=self.project),
                ECPMPConfig(project=self.other_project),
            ]
        )

    def create_milestone(self, project, name):
        return Milestone.objects.create(
            project=project,
            name=name,
            due_date=date(2026, 12, 31),
        )

    def move_milestone(self, milestone, action, *, allowed=True):
        url_path = action.replace("_", "-")
        request = self.factory.post(
            f"/milestones/{milestone.id}/{url_path}/",
        )
        force_authenticate(request, user=self.user)
        view = MilestoneViewSet.as_view({"post": action})

        with (
            patch(
                "project_management.permissions."
                "check_company_project_permission",
                return_value=False,
            ),
            patch.object(
                PhasePermission,
                "_check_permission",
                return_value=allowed,
            ),
            patch(
                "project_management.serializers.milestone."
                "require_any_permission_multiple_modules",
                return_value=False,
            ),
        ):
            return view(request, pk=milestone.id)

    def test_new_milestones_append_within_their_project(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")
        other_first = self.create_milestone(self.other_project, "Other first")

        self.assertEqual(first.sort_order, 1)
        self.assertEqual(second.sort_order, 2)
        self.assertEqual(other_first.sort_order, 1)

    def test_default_ordering_uses_sort_order_then_id(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")
        Milestone.objects.filter(pk=first.pk).update(sort_order=2)
        Milestone.objects.filter(pk=second.pk).update(sort_order=1)

        names = list(
            Milestone.objects.filter(project=self.project).values_list(
                "name",
                flat=True,
            )
        )

        self.assertEqual(names, ["Second", "First"])

    def test_sort_order_is_read_only_in_the_api_serializer(self):
        serializer = MilestoneSerializer()

        self.assertTrue(serializer.fields["sort_order"].read_only)

    def test_data_migration_backfills_each_project_by_id(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")
        other_first = self.create_milestone(self.other_project, "Other first")
        Milestone.objects.filter(
            pk__in=[first.pk, second.pk, other_first.pk]
        ).update(sort_order=0)

        migration = import_module(
            "project_management.migrations.0139_milestone_sort_order"
        )
        migration.backfill_milestone_sort_order(apps, None)

        project_orders = list(
            Milestone.objects.filter(project=self.project)
            .order_by("id")
            .values_list("sort_order", flat=True)
        )
        other_project_orders = list(
            Milestone.objects.filter(project=self.other_project)
            .order_by("id")
            .values_list("sort_order", flat=True)
        )

        self.assertEqual(project_orders, [1, 2])
        self.assertEqual(other_project_orders, [1])

    def test_move_up_swaps_adjacent_phases_without_touching_other_projects(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")
        third = self.create_milestone(self.project, "Third")
        other = self.create_milestone(self.other_project, "Other")

        response = self.move_milestone(second, "move_up")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sort_order"], 1)
        ordered_ids = list(
            Milestone.objects.filter(project=self.project).values_list(
                "id",
                flat=True,
            )
        )
        self.assertEqual(ordered_ids, [second.id, first.id, third.id])
        other.refresh_from_db()
        self.assertEqual(other.sort_order, 1)

    def test_move_actions_are_no_op_at_sequence_boundaries(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")

        move_up_response = self.move_milestone(first, "move_up")
        move_down_response = self.move_milestone(second, "move_down")

        self.assertEqual(move_up_response.status_code, status.HTTP_200_OK)
        self.assertEqual(move_down_response.status_code, status.HTTP_200_OK)
        orders = list(
            Milestone.objects.filter(project=self.project).values_list(
                "sort_order",
                flat=True,
            )
        )
        self.assertEqual(orders, [1, 2])

    def test_reorder_requires_phase_update_permission(self):
        first = self.create_milestone(self.project, "First")
        second = self.create_milestone(self.project, "Second")

        response = self.move_milestone(second, "move_up", allowed=False)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        ordered_ids = list(
            Milestone.objects.filter(project=self.project).values_list(
                "id",
                flat=True,
            )
        )
        self.assertEqual(ordered_ids, [first.id, second.id])

    def test_phase_permission_maps_reorder_post_to_update(self):
        request = self.factory.post("/milestones/1/move-up/")
        request.user = self.user
        view = SimpleNamespace(action="move_up")
        phase = self.create_milestone(self.project, "First")
        permission = PhasePermission(action="update", scope="project")

        def check_permission(user, module, action, scope, context):
            return action == "update"

        with (
            patch(
                "project_management.permissions."
                "check_company_project_permission",
                return_value=False,
            ),
            patch.object(
                permission,
                "_check_permission",
                side_effect=check_permission,
            ) as permission_check,
        ):
            has_general_permission = permission.has_permission(request, view)
            has_object_permission = permission.has_object_permission(
                request,
                view,
                phase,
            )

        checked_actions = [
            call.args[2] for call in permission_check.call_args_list
        ]
        self.assertTrue(has_general_permission)
        self.assertTrue(has_object_permission)
        self.assertIn("update", checked_actions)
        self.assertNotIn("create", checked_actions)
