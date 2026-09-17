from datetime import date
from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import CompanyProfile, User
from project_management.action_item_conversion import (
    ActionItemConversionConflict,
    convert_action_item_to_sub_action,
    get_action_item_parent_candidate_queryset,
)
from project_management.models import (
    ActionItem,
    ActionItemDependency,
    Milestone,
    Project,
)
from project_management.permissions import ActionItemPermission
from project_management.serializers.action_item import ActionItemSerializer
from project_management.signals import (
    action_item_post_save_operations,
    create_default_folders,
    milestone_post_save_operations,
    notify_on_milestone_save,
    notify_on_project_save,
)
from project_management.views.action_item import ActionItemViewSet


class ActionItemConversionTests(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        post_save.disconnect(
            action_item_post_save_operations,
            sender=ActionItem,
        )
        self.addCleanup(
            post_save.connect,
            action_item_post_save_operations,
            sender=ActionItem,
        )
        for signal, sender in (
            (create_default_folders, Project),
            (notify_on_project_save, Project),
            (milestone_post_save_operations, Milestone),
            (notify_on_milestone_save, Milestone),
        ):
            post_save.disconnect(signal, sender=sender)
            self.addCleanup(post_save.connect, signal, sender=sender)

        self.user = User.objects.create_user(
            email="conversion-user@example.com",
            password="password",
            first_name="Conversion",
            last_name="User",
            user_type="company_user",
        )
        self.company = self.create_company(self.user, "Conversion Company")
        User.objects.filter(id=self.user.id).update(company=self.company)
        self.user.refresh_from_db()

        self.permission_patcher = patch.object(
            ActionItemPermission,
            "can_update_object",
            return_value=True,
        )
        self.permission_patcher.start()
        self.addCleanup(self.permission_patcher.stop)
        self.object_permission_patcher = patch.object(
            ActionItemPermission,
            "has_object_permission",
            return_value=True,
        )
        self.object_permission_patcher.start()
        self.addCleanup(self.object_permission_patcher.stop)
        self.visibility_patcher = patch(
            "project_management.action_item_conversion."
            "filter_dependency_candidates_by_visibility",
            side_effect=lambda queryset, *_args: queryset,
        )
        self.visibility_patcher.start()
        self.addCleanup(self.visibility_patcher.stop)

    def create_company(self, owner, name):
        return CompanyProfile.objects.create(
            admin_user=owner,
            company_name=name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=f"{owner.id}-{name}@example.com",
            company_tax_reference=f"tax-{owner.id}-{name}",
            company_iban=f"iban-{owner.id}-{name}",
        )

    def create_action_item(self, name, **kwargs):
        if not any(
            field in kwargs
            for field in (
                "company_profile",
                "freelancer",
                "milestone",
                "parent_action_item",
            )
        ):
            kwargs["company_profile"] = self.company
        return ActionItem.objects.create(
            name=name,
            start_date=kwargs.pop("start_date", date(2026, 9, 1)),
            target_date=kwargs.pop("target_date", date(2026, 9, 30)),
            status=kwargs.pop("status", "open"),
            created_by=self.user,
            **kwargs,
        )

    def create_project(self, name):
        project = Project.objects.create(
            project_name=name,
            contract_value=0,
            company_profile=self.company,
            user=self.user,
        )
        first_phase = Milestone.objects.create(
            project=project,
            name="First Phase",
            start_date=date(2026, 9, 1),
            due_date=date(2026, 12, 31),
        )
        second_phase = Milestone.objects.create(
            project=project,
            name="Second Phase",
            start_date=date(2026, 9, 1),
            due_date=date(2026, 12, 31),
        )
        return project, first_phase, second_phase

    def convert(self, source, parent, **kwargs):
        return convert_action_item_to_sub_action(
            action_item_id=source.id,
            parent_action_item_id=parent.id,
            user=self.user,
            **kwargs,
        )

    def serializer_context(self):
        request = self.factory.patch(
            "/api/project_management/action_items/",
            {},
            format="json",
        )
        request.user = self.user
        return {"request": request, "user": self.user}

    def test_company_conversion_preserves_fields_and_inherits_scope(self):
        source = self.create_action_item(
            "001",
            labels=["Important"],
            allocated_time=12,
            allocated_budget=500,
        )
        source.assignees.add(self.user)
        parent = self.create_action_item("003")

        converted = self.convert(source, parent)

        converted.refresh_from_db()
        self.assertEqual(converted.parent_action_item_id, parent.id)
        self.assertIsNone(converted.company_profile_id)
        self.assertIsNone(converted.milestone_id)
        self.assertIsNone(converted.freelancer_id)
        self.assertEqual(converted.labels, ["Important"])
        self.assertEqual(converted.allocated_time, 12)
        self.assertEqual(converted.allocated_budget, 500)
        self.assertEqual(
            list(converted.assignees.values_list("id", flat=True)),
            [self.user.id],
        )

    def test_project_conversion_allows_parent_from_another_phase(self):
        project, first_phase, second_phase = self.create_project("Project A")
        source = self.create_action_item("001", milestone=first_phase)
        parent = self.create_action_item("003", milestone=second_phase)

        converted = self.convert(source, parent)

        converted.refresh_from_db()
        self.assertEqual(converted.parent_action_item_id, parent.id)
        self.assertIsNone(converted.milestone_id)
        self.assertEqual(
            converted.parent_action_item.milestone.project_id,
            project.id,
        )

    def test_freelancer_conversion_requires_the_same_freelancer(self):
        freelancer = User.objects.create_user(
            email="freelancer@example.com",
            password="password",
            first_name="First",
            last_name="Freelancer",
            user_type="freelancer",
        )
        other_freelancer = User.objects.create_user(
            email="other-freelancer@example.com",
            password="password",
            first_name="Other",
            last_name="Freelancer",
            user_type="freelancer",
        )
        source = self.create_action_item("001", freelancer=freelancer)
        parent = self.create_action_item("003", freelancer=freelancer)
        outside_parent = self.create_action_item(
            "004",
            freelancer=other_freelancer,
        )

        with self.assertRaises(serializers.ValidationError) as raised:
            self.convert(source, outside_parent)
        self.assertEqual(
            str(raised.exception.detail["code"]),
            "parent_outside_allowed_scope",
        )

        converted = self.convert(source, parent)
        converted.refresh_from_db()
        self.assertEqual(converted.parent_action_item_id, parent.id)
        self.assertIsNone(converted.freelancer_id)

    def test_parent_candidates_include_other_phases_but_not_other_projects(self):
        _project, first_phase, second_phase = self.create_project("Project A")
        _other_project, other_phase, _unused = self.create_project("Project B")
        source = self.create_action_item("001", milestone=first_phase)
        same_phase = self.create_action_item("002", milestone=first_phase)
        cross_phase = self.create_action_item("003", milestone=second_phase)
        other_project_item = self.create_action_item("004", milestone=other_phase)
        closed_item = self.create_action_item(
            "005",
            milestone=second_phase,
            status="closed",
        )
        child = self.create_action_item(
            "006",
            parent_action_item=cross_phase,
        )

        candidate_ids = set(
            get_action_item_parent_candidate_queryset(source).values_list(
                "id",
                flat=True,
            )
        )

        self.assertEqual(candidate_ids, {same_phase.id, cross_phase.id})
        self.assertNotIn(source.id, candidate_ids)
        self.assertNotIn(other_project_item.id, candidate_ids)
        self.assertNotIn(closed_item.id, candidate_ids)
        self.assertNotIn(child.id, candidate_ids)

    def test_conversion_rejects_parent_from_another_project(self):
        _project, first_phase, _second_phase = self.create_project("Project A")
        _other_project, other_phase, _unused = self.create_project("Project B")
        source = self.create_action_item("001", milestone=first_phase)
        parent = self.create_action_item("003", milestone=other_phase)

        with self.assertRaises(serializers.ValidationError) as raised:
            self.convert(source, parent)

        self.assertEqual(
            str(raised.exception.detail["code"]),
            "parent_outside_allowed_scope",
        )

    def test_conversion_rejects_an_item_that_has_children(self):
        source = self.create_action_item("001")
        self.create_action_item("Existing child", parent_action_item=source)
        parent = self.create_action_item("003")

        with self.assertRaises(serializers.ValidationError) as raised:
            self.convert(source, parent)

        self.assertEqual(
            str(raised.exception.detail["code"]),
            "action_item_has_children",
        )

    def test_conversion_rejects_closed_source_and_closed_parent(self):
        closed_source = self.create_action_item("001", status="closed")
        open_parent = self.create_action_item("003")

        with self.assertRaises(serializers.ValidationError) as source_error:
            self.convert(closed_source, open_parent)
        self.assertEqual(
            str(source_error.exception.detail["code"]),
            "closed_action_item",
        )

        open_source = self.create_action_item("002")
        closed_parent = self.create_action_item("004", status="closed")
        with self.assertRaises(serializers.ValidationError) as parent_error:
            self.convert(open_source, closed_parent)
        self.assertEqual(
            str(parent_error.exception.detail["code"]),
            "closed_parent_action_item",
        )

    def test_date_conflict_returns_allowed_range_and_accepts_adjusted_dates(self):
        source = self.create_action_item(
            "001",
            start_date=date(2026, 9, 1),
            target_date=date(2026, 9, 30),
        )
        parent = self.create_action_item(
            "003",
            start_date=date(2026, 9, 5),
            target_date=date(2026, 9, 20),
        )

        with self.assertRaises(ActionItemConversionConflict) as raised:
            self.convert(source, parent)

        detail = raised.exception.detail
        self.assertEqual(str(detail["code"]), "date_adjustment_required")
        self.assertEqual(
            str(detail["allowed_date_range"]["start_date"]),
            "2026-09-05",
        )
        self.assertEqual(
            str(detail["allowed_date_range"]["target_date"]),
            "2026-09-20",
        )

        converted = self.convert(
            source,
            parent,
            start_date=date(2026, 9, 5),
            start_date_provided=True,
            target_date=date(2026, 9, 20),
            target_date_provided=True,
        )
        self.assertEqual(converted.start_date, date(2026, 9, 5))
        self.assertEqual(converted.target_date, date(2026, 9, 20))

    def test_date_conflict_rejects_end_date_before_parent_start(self):
        source = self.create_action_item(
            "001",
            start_date=None,
            target_date=date(2026, 9, 5),
        )
        parent = self.create_action_item(
            "003",
            start_date=date(2026, 9, 10),
            target_date=date(2026, 9, 20),
        )

        with self.assertRaises(ActionItemConversionConflict) as raised:
            self.convert(source, parent)

        detail = raised.exception.detail
        self.assertEqual(str(detail["code"]), "date_adjustment_required")
        self.assertIn("target_date", detail["date_errors"])

    def test_dependencies_require_confirmation_and_both_directions_are_removed(self):
        predecessor = self.create_action_item("Predecessor")
        source = self.create_action_item("001")
        dependent = self.create_action_item("Dependent")
        parent = self.create_action_item("003")
        ActionItemDependency.objects.create(
            predecessor=predecessor,
            dependent=source,
        )
        ActionItemDependency.objects.create(
            predecessor=source,
            dependent=dependent,
        )

        with self.assertRaises(ActionItemConversionConflict) as raised:
            self.convert(source, parent)

        detail = raised.exception.detail
        self.assertEqual(
            str(detail["code"]),
            "dependency_confirmation_required",
        )
        self.assertEqual(int(detail["dependencies"]["predecessors"]), 1)
        self.assertEqual(int(detail["dependencies"]["dependents"]), 1)
        self.assertEqual(ActionItemDependency.objects.count(), 2)

        self.convert(
            source,
            parent,
            confirm_dependency_removal=True,
        )
        self.assertFalse(
            ActionItemDependency.objects.filter(
                predecessor=source,
            ).exists()
        )
        self.assertFalse(
            ActionItemDependency.objects.filter(
                dependent=source,
            ).exists()
        )

    def test_conversion_rechecks_parent_visibility(self):
        source = self.create_action_item("001")
        parent = self.create_action_item("003")

        with patch(
            "project_management.action_item_conversion."
            "filter_parent_candidates_by_visibility",
            side_effect=lambda queryset, *_args: queryset.none(),
        ):
            with self.assertRaises(PermissionDenied):
                self.convert(source, parent)

        source.refresh_from_db()
        self.assertIsNone(source.parent_action_item_id)

    def test_conversion_rechecks_update_permission(self):
        source = self.create_action_item("001")
        parent = self.create_action_item("003")

        with patch.object(
            ActionItemPermission,
            "can_update_object",
            return_value=False,
        ):
            with self.assertRaises(PermissionDenied):
                self.convert(source, parent)

        source.refresh_from_db()
        self.assertIsNone(source.parent_action_item_id)

    def test_generic_update_cannot_bypass_the_conversion_operation(self):
        source = self.create_action_item("001")
        parent = self.create_action_item("003")
        serializer = ActionItemSerializer(
            source,
            data={"parent_action_item": parent.id},
            partial=True,
            context=self.serializer_context(),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("parent_action_item", serializer.errors)

    def test_generic_update_cannot_detach_an_existing_sub_action(self):
        parent = self.create_action_item("003")
        source = self.create_action_item(
            "001",
            parent_action_item=parent,
        )
        serializer = ActionItemSerializer(
            source,
            data={"parent_action_item": None},
            partial=True,
            context=self.serializer_context(),
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("parent_action_item", serializer.errors)

    def test_sub_action_creation_rechecks_locked_parent_state(self):
        parent = self.create_action_item("Parent")
        grandparent = self.create_action_item("Grandparent")
        stale_parent = ActionItem.objects.get(pk=parent.pk)
        ActionItem.objects.filter(pk=parent.pk).update(
            parent_action_item=grandparent,
            company_profile=None,
        )
        serializer = ActionItemSerializer(context=self.serializer_context())

        with self.assertRaises(serializers.ValidationError) as raised:
            serializer.create(
                {
                    "parent_action_item": stale_parent,
                    "name": "Forbidden grandchild",
                    "target_date": date(2026, 9, 20),
                    "status": "open",
                    "assignees": [self.user],
                }
            )

        self.assertIn("parent_action_item", raised.exception.detail)
        self.assertFalse(
            ActionItem.objects.filter(name="Forbidden grandchild").exists()
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    def test_parent_candidate_endpoint_searches_by_id_and_paginates(
        self,
        _license_permission,
    ):
        source = self.create_action_item("001")
        parent = self.create_action_item("Parent candidate")
        self.create_action_item("Other candidate")
        request = self.factory.get(
            (
                f"/action-items/{source.id}/parent-candidates/"
                f"?search={parent.id}&page_size=1"
            )
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "parent_candidates"})

        response = view(request, pk=source.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], parent.id)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    def test_make_sub_action_endpoint_returns_dependency_conflict(
        self,
        _license_permission,
    ):
        predecessor = self.create_action_item("Predecessor")
        source = self.create_action_item("001")
        parent = self.create_action_item("003")
        ActionItemDependency.objects.create(
            predecessor=predecessor,
            dependent=source,
        )
        request = self.factory.post(
            f"/action-items/{source.id}/make-sub-action/",
            {"parent_action_item_id": parent.id},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"post": "make_sub_action"})

        response = view(request, pk=source.id)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            str(response.data["code"]),
            "dependency_confirmation_required",
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    def test_make_sub_action_endpoint_converts_the_item(
        self,
        _license_permission,
    ):
        source = self.create_action_item("001")
        parent = self.create_action_item("003")
        request = self.factory.post(
            f"/action-items/{source.id}/make-sub-action/",
            {"parent_action_item_id": parent.id},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"post": "make_sub_action"})

        response = view(request, pk=source.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["parent_action_item"], parent.id)
        source.refresh_from_db()
        self.assertEqual(source.parent_action_item_id, parent.id)
