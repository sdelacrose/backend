from datetime import date
from unittest.mock import patch

from django.db.models.signals import m2m_changed, post_save, pre_delete
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, Permission, Role, RolePermission, User
from project_management.models import (
    ActionItem,
    Milestone,
    Project,
    ProjectTeamMembers,
    Requirement,
)
from project_management.permissions import ActionItemPermission, RequirementPermission
from project_management.signals import (
    action_item_assignees_post_save_operations,
    action_item_post_save_operations,
    action_item_pre_delete_operations,
    create_default_folders,
    create_related_objects,
    milestone_post_save_operations,
    notify_on_milestone_save,
    notify_on_project_save,
)
from project_management.views.action_item import ActionItemViewSet


class ActionItemRequirementApiTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        for signal, sender in (
            (action_item_post_save_operations, ActionItem),
            (create_default_folders, Project),
            (notify_on_project_save, Project),
            (milestone_post_save_operations, Milestone),
            (notify_on_milestone_save, Milestone),
            (create_related_objects, ProjectTeamMembers),
        ):
            post_save.disconnect(signal, sender=sender)
            self.addCleanup(post_save.connect, signal, sender=sender)

        m2m_changed.disconnect(
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )
        pre_delete.disconnect(action_item_pre_delete_operations, sender=ActionItem)
        self.addCleanup(
            pre_delete.connect,
            action_item_pre_delete_operations,
            sender=ActionItem,
        )
        self.addCleanup(
            m2m_changed.connect,
            action_item_assignees_post_save_operations,
            sender=ActionItem.assignees.through,
        )

        self.user = User.objects.create_user(
            email="action-item-requirements@example.com",
            password="password",
            first_name="Action",
            last_name="Editor",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Action Item Requirements Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="requirements@example.com",
            company_tax_reference="tax-action-requirements",
            company_iban="iban-action-requirements",
        )
        User.objects.filter(id=self.user.id).update(company=self.company)
        self.user.refresh_from_db()
        self.project = Project.objects.create(
            project_name="Acceptance Project",
            solution="Energy Consultant Solution",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.project_role = Role.objects.create(
            name="Action Item Requirement Editor",
            company=self.company,
            type="solution",
            scope="project",
        )
        for permission_key in (
            "action_item.create",
            "action_item.update",
            "action_item.view",
            "requirements.view",
        ):
            permission, _created = Permission.objects.get_or_create(
                key=permission_key,
                defaults={
                    "name": permission_key,
                    "module": permission_key.split(".")[0],
                },
            )
            role_permission = RolePermission.objects.create(
                role=self.project_role,
                permission=permission,
            )
            if permission_key == "requirements.view":
                self.requirement_view_role_permission = role_permission
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="Editor",
            project_role=self.project_role,
            project_access="RW",
            team_member=self.user,
        )
        self.phase = Milestone.objects.create(
            project=self.project,
            name="Factory Acceptance Test",
            start_date=date(2026, 9, 7),
            due_date=date(2026, 9, 13),
            status="open",
        )
        self.requirement = Requirement.objects.create(
            project=self.project,
            name="Verify sorter throughput",
            object_number="REQ-100",
            created_by=self.user,
            modified_by=self.user,
        )
        self.other_project = Project.objects.create(
            project_name="Other Project",
            solution="Energy Consultant Solution",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.other_phase = Milestone.objects.create(
            project=self.other_project,
            name="Other Project Phase",
            start_date=date(2026, 10, 1),
            due_date=date(2026, 10, 7),
            status="open",
        )

    def create_action_item(self, name="Acceptance Action Item"):
        action_item = ActionItem.objects.create(
            milestone=self.phase,
            name=name,
            notes="Objective acceptance criterion",
            start_date=date(2026, 9, 7),
            target_date=date(2026, 9, 13),
            forecast_date=date(2026, 9, 13),
            status="open",
            reminder="none",
            allocated_budget=0,
            allocated_time=0,
            created_by=self.user,
        )
        action_item.assignees.set([self.user])
        return action_item

    def action_item_payload(self, requirements=None):
        payload = {
            "milestone": self.phase.id,
            "name": "Measure sorter throughput",
            "notes": "Pass when throughput is at least 2,400 parcels/hour.",
            "start_date": "2026-09-07",
            "target_date": "2026-09-13",
            "forecast_date": "2026-09-13",
            "status": "open",
            "reminder": "none",
            "allocated_budget": 0,
            "allocated_time": 0,
            "labels": [],
            "assignees": [self.user.id],
        }
        if requirements is not None:
            payload["requirements"] = requirements
        return payload

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_create_project_action_item_links_and_returns_requirement(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([self.requirement.id]),
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            response.data["requirements"],
            [
                {
                    "id": self.requirement.id,
                    "name": "Verify sorter throughput",
                    "object_number": "REQ-100",
                    "sequence": "1",
                }
            ],
        )
        self.assertEqual(
            list(
                ActionItem.objects.get(id=response.data["id"])
                .requirements.values_list("id", flat=True)
            ),
            [self.requirement.id],
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_update_rejects_moving_linked_requirements_to_another_project(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])
        request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {"milestone": self.other_phase.id},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requirements", response.data)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    def test_update_preserves_an_existing_empty_assignee_set(
        self,
        _action_item_permission,
        _license_permission,
    ):
        action_item = self.create_action_item()
        action_item.assignees.clear()
        action_item.assignee = None
        action_item.save(update_fields=["assignee", "updated_at"])
        request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {
                "name": "Reviewed acceptance Action Item",
                "assignees": [],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        action_item.refresh_from_db()
        self.assertEqual(action_item.name, "Reviewed acceptance Action Item")
        self.assertEqual(list(action_item.assignees.all()), [])

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_update_replaces_and_unlinks_requirements_without_deleting_records(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        replacement = Requirement.objects.create(
            project=self.project,
            name="Verify emergency stop",
            object_number="REQ-101",
            created_by=self.user,
            modified_by=self.user,
        )
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])

        replace_request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {"requirements": [replacement.id]},
            format="json",
        )
        force_authenticate(replace_request, user=self.user)
        view = ActionItemViewSet.as_view({"patch": "partial_update"})
        replace_response = view(replace_request, pk=action_item.id)

        self.assertEqual(replace_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in replace_response.data["requirements"]],
            [replacement.id],
        )

        unlink_request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {"requirements": []},
            format="json",
        )
        force_authenticate(unlink_request, user=self.user)
        unlink_response = view(unlink_request, pk=action_item.id)

        self.assertEqual(unlink_response.status_code, status.HTTP_200_OK)
        self.assertEqual(unlink_response.data["requirements"], [])
        self.assertTrue(ActionItem.objects.filter(id=action_item.id).exists())
        self.assertTrue(Requirement.objects.filter(id=self.requirement.id).exists())
        self.assertTrue(Requirement.objects.filter(id=replacement.id).exists())

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=False,
    )
    def test_update_cannot_unlink_requirements_without_requirement_visibility(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])
        request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {"requirements": []},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            list(action_item.requirements.values_list("id", flat=True)),
            [self.requirement.id],
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=False,
    )
    def test_create_allows_empty_requirements_without_requirement_visibility(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([]),
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["requirements"], [])

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=False,
    )
    def test_update_allows_empty_requirements_when_no_links_are_hidden(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        action_item = self.create_action_item()
        request = self.factory.patch(
            f"/api/project_management/action_items/{action_item.id}/",
            {"requirements": []},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"patch": "partial_update"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["requirements"], [])

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_create_rejects_requirement_from_another_project(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        other_requirement = Requirement.objects.create(
            project=self.other_project,
            name="Other project's requirement",
            object_number="REQ-OTHER",
            created_by=self.user,
            modified_by=self.user,
        )
        request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([other_requirement.id]),
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requirements", response.data)
        self.assertFalse(
            ActionItem.objects.filter(name="Measure sorter throughput").exists()
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_create_rejects_requirement_from_another_company(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        foreign_user = User.objects.create_user(
            email="foreign-requirements@example.com",
            password="password",
            first_name="Foreign",
            last_name="Owner",
            user_type="company_user",
        )
        foreign_company = CompanyProfile.objects.create(
            admin_user=foreign_user,
            company_name="Foreign Requirements Company",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="789",
            company_mob_no="012",
            company_web_address="https://foreign.example.com",
            company_email_address="foreign-requirements@example.com",
            company_tax_reference="tax-foreign-requirements",
            company_iban="iban-foreign-requirements",
        )
        User.objects.filter(id=foreign_user.id).update(company=foreign_company)
        foreign_user.refresh_from_db()
        foreign_project = Project.objects.create(
            project_name="Foreign Project",
            solution="Energy Consultant Solution",
            contract_value=1000,
            company_profile=foreign_company,
            user=foreign_user,
            created_by=foreign_user,
        )
        foreign_requirement = Requirement.objects.create(
            project=foreign_project,
            name="Foreign requirement",
            object_number="REQ-FOREIGN",
            created_by=foreign_user,
            modified_by=foreign_user,
        )
        request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([foreign_requirement.id]),
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requirements", response.data)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=False,
    )
    def test_create_rejects_requirement_without_requirement_view_permission(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([self.requirement.id]),
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("requirements", response.data)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=False,
    )
    def test_read_hides_requirements_without_requirement_view_permission(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])
        request = self.factory.get(
            f"/api/project_management/action_items/{action_item.id}/"
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"get": "retrieve"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["requirements"], [])
        self.assertEqual(action_item.requirements.count(), 1)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    def test_read_filters_cross_project_requirement_links(
        self,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        other_requirement = Requirement.objects.create(
            project=self.other_project,
            name="Other project's requirement",
            object_number="REQ-OTHER",
            created_by=self.user,
            modified_by=self.user,
        )
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement, other_requirement])
        request = self.factory.get(
            f"/api/project_management/action_items/{action_item.id}/"
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"get": "retrieve"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["requirements"]],
            [self.requirement.id],
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(ActionItemPermission, "has_object_permission", return_value=True)
    @patch.object(
        RequirementPermission,
        "_check_view_permission_project",
        return_value=True,
    )
    @patch.object(
        Requirement,
        "get_sequence",
        side_effect=AssertionError("Action Item serialization must use a bulk sequence map"),
    )
    def test_read_bulk_builds_nested_requirement_sequences(
        self,
        _get_sequence,
        _requirement_permission,
        _action_item_permission,
        _license_permission,
    ):
        child = Requirement.objects.create(
            project=self.project,
            parent=self.requirement,
            name="Verify alarm acknowledgement",
            object_number="REQ-101",
            created_by=self.user,
            modified_by=self.user,
        )
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement, child])
        request = self.factory.get(
            f"/api/project_management/action_items/{action_item.id}/"
        )
        force_authenticate(request, user=self.user)

        response = ActionItemViewSet.as_view({"get": "retrieve"})(
            request,
            pk=action_item.id,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {
                requirement["object_number"]: requirement["sequence"]
                for requirement in response.data["requirements"]
            },
            {"REQ-100": "1", "REQ-101": "1.1"},
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    def test_real_project_role_controls_linking_visibility_and_tenant_scope(
        self,
        _license_permission,
    ):
        create_request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([self.requirement.id]),
            format="json",
        )
        force_authenticate(create_request, user=self.user)
        create_response = ActionItemViewSet.as_view({"post": "create"})(
            create_request
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED)

        other_requirement = Requirement.objects.create(
            project=self.other_project,
            name="Other project requirement",
            object_number="REQ-OTHER-ROLE",
            created_by=self.user,
            modified_by=self.user,
        )
        cross_project_request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([other_requirement.id]),
            format="json",
        )
        force_authenticate(cross_project_request, user=self.user)
        cross_project_response = ActionItemViewSet.as_view({"post": "create"})(
            cross_project_request
        )
        self.assertEqual(cross_project_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.requirement_view_role_permission.delete()
        retrieve_request = self.factory.get(
            f"/api/project_management/action_items/{create_response.data['id']}/"
        )
        force_authenticate(retrieve_request, user=self.user)
        retrieve_response = ActionItemViewSet.as_view({"get": "retrieve"})(
            retrieve_request,
            pk=create_response.data["id"],
        )
        self.assertEqual(retrieve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(retrieve_response.data["requirements"], [])

        denied_link_request = self.factory.post(
            "/api/project_management/action_items/",
            self.action_item_payload([self.requirement.id]),
            format="json",
        )
        force_authenticate(denied_link_request, user=self.user)
        denied_link_response = ActionItemViewSet.as_view({"post": "create"})(
            denied_link_request
        )
        self.assertEqual(denied_link_response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_deleting_requirement_only_removes_action_item_link(self):
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])

        self.requirement.delete()

        self.assertTrue(ActionItem.objects.filter(id=action_item.id).exists())
        self.assertFalse(action_item.requirements.exists())

    def test_deleting_action_item_does_not_delete_requirement(self):
        action_item = self.create_action_item()
        action_item.requirements.set([self.requirement])

        action_item.delete()

        self.assertTrue(Requirement.objects.filter(id=self.requirement.id).exists())
