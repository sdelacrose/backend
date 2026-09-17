from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.test_action_item_query_services import (
    ActionItemQueryFixtureMixin,
)
from project_management.permissions import ActionItemPermission
from project_management.views.action_item import ActionItemViewSet
from django.test import TestCase


class ActionItemListApiTests(ActionItemQueryFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.factory = APIRequestFactory()

    def request(self, action="list", **params):
        path = "/action-items/"
        if action != "list":
            path += f"{action.replace('_', '-')}/"
        request = self.factory.get(path, {"project": self.project.id, **params})
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": action})
        with (
            patch(
                "project_management.views.action_item."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            patch(
                "project_management.permissions.ActionItemPermission.has_permission",
                return_value=True,
            ),
        ):
            return view(request)

    def test_list_paginates_parents_and_only_embeds_their_children(self):
        first = self.create_parent("First")
        self.create_parent("Second")
        child = self.create_parent("Temporary")
        child.milestone = None
        child.parent_action_item = first
        child.save()

        response = self.request(page=1, page_size=1, show_closed="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["id"], first.id)
        self.assertEqual(
            response.data["results"][0]["sub_action_items"][0]["id"],
            child.id,
        )

    def test_list_hides_closed_by_default_and_can_include_them(self):
        self.create_parent("Open", status="open")
        self.create_parent("Closed", status="closed")

        default_response = self.request()
        inclusive_response = self.request(show_closed="true")

        self.assertEqual(default_response.data["count"], 1)
        self.assertEqual(inclusive_response.data["count"], 2)

    def test_explicit_ordering_is_global_and_phase_context_is_compact(self):
        self.create_parent("Zulu")
        alpha = self.create_parent("Alpha")

        response = self.request(ordering="name", show_closed="true")

        self.assertEqual(response.data["results"][0]["id"], alpha.id)
        self.assertEqual(
            set(response.data["results"][0]["milestone"]),
            {
                "id",
                "name",
                "colour",
                "sort_order",
                "start_date",
                "due_date",
                "project",
            },
        )

    def test_filter_options_are_scope_stable_and_do_not_apply_list_narrowing(self):
        item = self.create_parent(
            "Visible",
            status="in progress",
            priority_level="high",
            labels=["urgent"],
        )
        item.assignees.add(self.user)

        response = self.request(
            "filter_options",
            status="closed",
            search="no-result",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            {"id": self.user.id, "name": self.user.full_name},
            response.data["assignees"],
        )
        self.assertIn(
            {
                "id": self.phase.id,
                "name": self.phase.name,
                "colour": self.phase.colour,
                "sort_order": 1,
            },
            response.data["phases"],
        )
        self.assertIn(
            {"value": "urgent", "label": "urgent"},
            response.data["labels"],
        )

    def test_board_is_flat_status_scoped_and_paginates_parents_and_children(self):
        parent = self.create_parent("Parent", status="open")
        child = self.create_parent("Temporary", status="open")
        child.milestone = None
        child.parent_action_item = parent
        child.save()
        self.create_parent("Closed", status="closed")

        response = self.request("board", status="open", page_size=1, page=2)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        card = response.data["results"][0]
        self.assertTrue(card["is_sub_action_item"])
        self.assertEqual(card["parent_action_item_id"], parent.id)
        self.assertEqual(card["parent_action_item_name"], parent.name)
        self.assertEqual(card["milestone"]["id"], self.phase.id)

    def test_time_bucket_uses_monday_to_sunday_boundaries(self):
        today = timezone.localdate()
        remaining_days = 6 - today.weekday()
        target = today + timedelta(days=remaining_days or 1)
        bucket = "this_week" if remaining_days else "next_week"
        item = self.create_parent("Bucket item", target_date=target)
        self.create_parent("Later", target_date=today + timedelta(days=30))

        response = self.request("time", bucket=bucket, show_closed="true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], item.id)

    def test_calendar_returns_every_flat_interval_overlap_without_pagination(self):
        parent = self.create_parent(
            "Spanning",
            start_date=timezone.localdate() - timedelta(days=10),
            target_date=timezone.localdate() + timedelta(days=10),
        )
        child = self.create_parent("Temporary")
        child.milestone = None
        child.parent_action_item = parent
        child.start_date = timezone.localdate()
        child.target_date = timezone.localdate() + timedelta(days=1)
        child.save()
        self.create_parent(
            "Outside",
            start_date=timezone.localdate() + timedelta(days=30),
            target_date=timezone.localdate() + timedelta(days=31),
        )

        response = self.request(
            "calendar",
            range_start=timezone.localdate().isoformat(),
            range_end=(timezone.localdate() + timedelta(days=7)).isoformat(),
            show_closed="true",
            page_size=1,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["id"] for item in response.data}, {parent.id, child.id})
        child_data = next(item for item in response.data if item["id"] == child.id)
        self.assertTrue(child_data["is_sub_action_item"])
        self.assertEqual(child_data["parent_action_item_name"], parent.name)

    def test_gantt_denies_project_metadata_without_action_item_view_permission(self):
        request = self.factory.get(
            "/action-items/gantt/",
            {"project": self.project.id},
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "gantt"})

        with patch.object(
            ActionItemPermission,
            "_check_view_permission",
            return_value=False,
        ), patch.object(
            ActionItemPermission,
            "_check_view_assigned_created_permission",
            return_value=False,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 403)

    def test_gantt_uses_project_permissions_when_company_scope_is_also_sent(self):
        request = self.factory.get(
            "/action-items/gantt/",
            {
                "company_profile": self.company.id,
                "project": self.project.id,
            },
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "gantt"})

        def has_view_permission(permission, _user, _obj, _context):
            return permission.scope == "company"

        with patch.object(
            ActionItemPermission,
            "_check_view_permission",
            autospec=True,
            side_effect=has_view_permission,
        ), patch.object(
            ActionItemPermission,
            "_check_view_assigned_created_permission",
            return_value=False,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 403)

    def test_gantt_returns_metadata_with_action_item_view_permission(self):
        request = self.factory.get(
            "/action-items/gantt/",
            {"project": self.project.id},
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "gantt"})

        with patch.object(
            ActionItemPermission,
            "_check_view_permission",
            return_value=True,
        ), patch(
            "project_management.views.action_item."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["project"]["id"], self.project.id)
        self.assertEqual(
            [phase["id"] for phase in response.data["phases"]],
            [self.phase.id],
        )

    def test_gantt_does_not_expose_project_from_another_solution(self):
        self.project.solution = "Unrelated Solution"
        self.project.save(update_fields=["solution"])
        request = self.factory.get(
            "/action-items/gantt/",
            {"project": self.project.id},
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "gantt"})

        with patch.object(
            ActionItemPermission,
            "_check_view_permission",
            return_value=True,
        ), patch(
            "project_management.views.action_item."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            response = view(request)

        self.assertEqual(response.status_code, 404)
