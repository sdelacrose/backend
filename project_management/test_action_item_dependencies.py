from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework import serializers, status
from rest_framework.test import (
    APIRequestFactory,
    force_authenticate,
)

from models.models import CompanyProfile, User
from project_management.action_item_dependencies import (
    CIRCULAR_DEPENDENCY_ERROR,
    filter_dependent_candidates_by_update_permission,
    get_dependent_candidate_queryset,
    get_predecessor_candidate_queryset,
    replace_action_item_dependents,
    replace_action_item_predecessors,
    search_dependent_candidates,
    search_predecessor_candidates,
    validate_action_item_dependents,
    validate_action_item_predecessors,
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
from project_management.views.action_item import (
    ActionItemViewSet,
    _dependent_dependencies_prefetch,
    _predecessor_dependencies_prefetch,
)


class ActionItemDependencyTests(TestCase):
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
            email="dependency-user@example.com",
            password="password",
            first_name="Dependency",
            last_name="User",
            user_type="company_user",
        )
        self.company = self.create_company(self.user, "Dependency Company")
        User.objects.filter(id=self.user.id).update(company=self.company)
        self.user.refresh_from_db()
        self.predecessor = self.create_action_item("Predecessor")
        self.replacement = self.create_action_item("Replacement")
        self.dependent = self.create_action_item("Dependent")

    def create_company(self, owner, name):
        return CompanyProfile.objects.create(
            admin_user=owner,
            company_name=name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=f"{owner.id}@example.com",
            company_tax_reference=f"tax-{owner.id}",
            company_iban=f"iban-{owner.id}",
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
            start_date=date(2026, 7, 1),
            target_date=date(2026, 7, 31),
            status="open",
            created_by=self.user,
            **kwargs,
        )

    def create_project(self, name="Dependency Project"):
        project = Project.objects.create(
            project_name=name,
            contract_value=0,
            company_profile=self.company,
            user=self.user,
        )
        first_phase = Milestone.objects.create(
            project=project,
            name="First Phase",
            due_date=date(2026, 12, 31),
        )
        second_phase = Milestone.objects.create(
            project=project,
            name="Second Phase",
            due_date=date(2026, 12, 31),
        )
        return project, first_phase, second_phase

    def test_dependency_requires_a_unique_direction_and_type(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionItemDependency.objects.create(
                predecessor=self.predecessor,
                dependent=self.dependent,
            )

    def test_dependency_cannot_reference_itself(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActionItemDependency.objects.create(
                predecessor=self.dependent,
                dependent=self.dependent,
            )

    def test_serializer_replaces_only_finish_to_start_predecessors(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
            dependency_type="start_to_start",
        )
        request = SimpleNamespace(user=self.user)
        serializer = ActionItemSerializer(
            self.dependent,
            data={"predecessor_ids": [self.replacement.id]},
            partial=True,
            context={"request": request, "user": self.user},
        )

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            self.assertTrue(serializer.is_valid(), serializer.errors)
            serializer.save()

        finish_to_start_ids = list(
            self.dependent.incoming_dependencies.filter(
                dependency_type=(
                    ActionItemDependency.DependencyType.FINISH_TO_START
                )
            ).values_list("predecessor_id", flat=True)
        )
        self.assertEqual(finish_to_start_ids, [self.replacement.id])
        self.assertTrue(
            self.dependent.incoming_dependencies.filter(
                predecessor=self.predecessor,
                dependency_type="start_to_start",
            ).exists()
        )

    def test_serializer_exposes_compact_predecessor_data(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        serializer = ActionItemSerializer(
            context={"include_predecessors": True}
        )

        predecessor_data = serializer.get_predecessors(self.dependent)

        self.assertEqual(
            predecessor_data,
            [
                {
                    "id": self.predecessor.id,
                    "name": "Predecessor",
                    "start_date": date(2026, 7, 1),
                    "target_date": date(2026, 7, 31),
                    "status": "open",
                    "parent_action_item_id": None,
                    "milestone": None,
                    "dependency_type": "finish_to_start",
                }
            ],
        )
        self.assertTrue(serializer.fields["predecessor_ids"].write_only)
        self.assertTrue(serializer.fields["predecessors"].read_only)

    def test_serializer_exposes_compact_dependent_data(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        serializer = ActionItemSerializer(
            context={"include_dependents": True}
        )

        dependent_data = serializer.get_dependents(self.predecessor)

        self.assertEqual(
            dependent_data,
            [
                {
                    "id": self.dependent.id,
                    "name": "Dependent",
                    "start_date": date(2026, 7, 1),
                    "target_date": date(2026, 7, 31),
                    "status": "open",
                    "parent_action_item_id": None,
                    "milestone": None,
                    "dependency_type": "finish_to_start",
                }
            ],
        )
        self.assertTrue(serializer.fields["dependents"].read_only)

    def test_prefetched_predecessors_do_not_query_per_action_item(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        ActionItemDependency.objects.create(
            predecessor=self.replacement,
            dependent=self.dependent,
        )
        queryset = (
            ActionItem.objects.filter(
                id__in=[
                    self.predecessor.id,
                    self.replacement.id,
                    self.dependent.id,
                ]
            )
            .prefetch_related(
                _predecessor_dependencies_prefetch(
                    "incoming_dependencies"
                )
            )
            .order_by("id")
        )

        with self.assertNumQueries(2):
            items = list(queryset)
            serialized_predecessors = [
                ActionItemSerializer(
                    context={"include_predecessors": True}
                ).get_predecessors(item)
                for item in items
            ]

        dependent_index = next(
            index
            for index, item in enumerate(items)
            if item.id == self.dependent.id
        )
        self.assertCountEqual(
            [
                predecessor["id"]
                for predecessor in serialized_predecessors[dependent_index]
            ],
            [self.predecessor.id, self.replacement.id],
        )

    def test_prefetched_dependents_do_not_query_per_action_item(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.replacement,
        )
        queryset = (
            ActionItem.objects.filter(
                id__in=[
                    self.predecessor.id,
                    self.replacement.id,
                    self.dependent.id,
                ]
            )
            .prefetch_related(
                _dependent_dependencies_prefetch(
                    "outgoing_dependencies"
                )
            )
            .order_by("id")
        )

        with self.assertNumQueries(2):
            items = list(queryset)
            serialized_dependents = [
                ActionItemSerializer(
                    context={"include_dependents": True}
                ).get_dependents(item)
                for item in items
            ]

        predecessor_index = next(
            index
            for index, item in enumerate(items)
            if item.id == self.predecessor.id
        )
        self.assertCountEqual(
            [
                dependent["id"]
                for dependent in serialized_dependents[predecessor_index]
            ],
            [self.dependent.id, self.replacement.id],
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    def test_retrieve_response_includes_visible_dependents(
        self,
        _has_object_permission,
        _license_permission,
    ):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        request = self.factory.get(
            f"/action-items/{self.predecessor.id}/"
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view({"get": "retrieve"})

        with patch(
            "project_management.serializers.action_item."
            "filter_dependency_candidates_by_visibility",
            side_effect=lambda queryset, *_args: queryset,
        ):
            response = view(request, pk=self.predecessor.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [dependent["id"] for dependent in response.data["dependents"]],
            [self.dependent.id],
        )

    def test_company_parent_candidates_exclude_children_and_other_companies(self):
        child = self.create_action_item(
            "Child",
            parent_action_item=self.predecessor,
        )
        other_owner = User.objects.create_user(
            email="other-dependency-user@example.com",
            password="password",
            user_type="company_user",
        )
        other_company = self.create_company(other_owner, "Other Company")
        other_item = self.create_action_item(
            "Other company item",
            company_profile=other_company,
        )

        candidate_ids = set(
            get_predecessor_candidate_queryset(
                self.dependent
            ).values_list("id", flat=True)
        )

        self.assertIn(self.predecessor.id, candidate_ids)
        self.assertIn(self.replacement.id, candidate_ids)
        self.assertNotIn(child.id, candidate_ids)
        self.assertNotIn(other_item.id, candidate_ids)
        self.assertNotIn(self.dependent.id, candidate_ids)

    def test_project_parent_candidates_can_cross_phases_in_same_project(self):
        project, first_phase, second_phase = self.create_project()
        dependent = self.create_action_item(
            "Project dependent",
            milestone=first_phase,
        )
        cross_phase = self.create_action_item(
            "Cross-phase predecessor",
            milestone=second_phase,
        )
        child = self.create_action_item(
            "Project child",
            parent_action_item=cross_phase,
        )
        _, other_phase, _ = self.create_project("Other Project")
        other_project_item = self.create_action_item(
            "Other project item",
            milestone=other_phase,
        )

        candidate_ids = set(
            get_predecessor_candidate_queryset(
                dependent
            ).values_list("id", flat=True)
        )

        self.assertIn(cross_phase.id, candidate_ids)
        self.assertNotIn(child.id, candidate_ids)
        self.assertNotIn(other_project_item.id, candidate_ids)
        self.assertEqual(cross_phase.milestone.project_id, project.id)

    def test_sub_action_candidates_are_limited_to_siblings(self):
        first_sibling = self.create_action_item(
            "First sibling",
            parent_action_item=self.predecessor,
        )
        dependent_sibling = self.create_action_item(
            "Dependent sibling",
            parent_action_item=self.predecessor,
        )
        other_parent = self.create_action_item("Other parent")
        other_child = self.create_action_item(
            "Other child",
            parent_action_item=other_parent,
        )

        candidate_ids = set(
            get_predecessor_candidate_queryset(
                dependent_sibling
            ).values_list("id", flat=True)
        )

        self.assertEqual(candidate_ids, {first_sibling.id})
        self.assertNotIn(other_child.id, candidate_ids)

    def test_dependent_candidates_use_the_same_company_and_level_scope(self):
        child = self.create_action_item(
            "Child",
            parent_action_item=self.predecessor,
        )
        other_owner = User.objects.create_user(
            email="other-dependent-user@example.com",
            password="password",
            user_type="company_user",
        )
        other_company = self.create_company(other_owner, "Other Company")
        other_item = self.create_action_item(
            "Other company item",
            company_profile=other_company,
        )

        candidate_ids = set(
            get_dependent_candidate_queryset(
                self.predecessor
            ).values_list("id", flat=True)
        )

        self.assertIn(self.dependent.id, candidate_ids)
        self.assertIn(self.replacement.id, candidate_ids)
        self.assertNotIn(child.id, candidate_ids)
        self.assertNotIn(other_item.id, candidate_ids)
        self.assertNotIn(self.predecessor.id, candidate_ids)

    def test_dependent_candidates_with_assigned_update_are_filtered(self):
        other_user = User.objects.create_user(
            email="other-candidate-user@example.com",
            password="password",
            user_type="company_user",
            company=self.company,
        )
        self.dependent.created_by = other_user
        self.dependent.save(update_fields=["created_by"])

        queryset = get_dependent_candidate_queryset(self.predecessor)

        def permission_result(_user, modules_actions, _scope, _context):
            return "update_assigned_created" in next(
                iter(modules_actions.values())
            )

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            side_effect=permission_result,
        ):
            candidate_ids = set(
                filter_dependent_candidates_by_update_permission(
                    queryset,
                    self.user,
                    self.predecessor,
                ).values_list("id", flat=True)
            )

        self.assertIn(self.replacement.id, candidate_ids)
        self.assertNotIn(self.dependent.id, candidate_ids)

    def test_validation_rejects_duplicate_self_and_cross_level_dependencies(self):
        child = self.create_action_item(
            "Child candidate",
            parent_action_item=self.predecessor,
        )

        with self.assertRaises(serializers.ValidationError):
            validate_action_item_predecessors(
                self.dependent,
                [self.predecessor, self.predecessor],
                self.user,
            )
        with self.assertRaises(serializers.ValidationError):
            validate_action_item_predecessors(
                self.dependent,
                [self.dependent],
                self.user,
            )
        with self.assertRaises(serializers.ValidationError):
            validate_action_item_predecessors(
                self.dependent,
                [child],
                self.user,
            )

    def test_validation_rejects_invisible_predecessors(self):
        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=False,
            ),
            self.assertRaises(serializers.ValidationError) as error,
        ):
            validate_action_item_predecessors(
                self.dependent,
                [self.predecessor],
                self.user,
            )

        self.assertIn("permission", str(error.exception).lower())

    def test_validation_rejects_direct_and_indirect_cycles(self):
        middle = self.create_action_item("Middle")
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=middle,
        )
        ActionItemDependency.objects.create(
            predecessor=middle,
            dependent=self.replacement,
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError),
        ):
            validate_action_item_predecessors(
                self.dependent,
                [middle],
                self.user,
            )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError),
        ):
            validate_action_item_predecessors(
                self.dependent,
                [self.replacement],
                self.user,
            )

    def test_predecessor_cycle_uses_the_shared_validation_message(self):
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=self.predecessor,
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError) as error,
        ):
            validate_action_item_predecessors(
                self.dependent,
                [self.predecessor],
                self.user,
            )

        self.assertEqual(
            str(error.exception.detail["predecessor_ids"]),
            CIRCULAR_DEPENDENCY_ERROR,
        )

    def test_dependent_validation_rejects_direct_and_indirect_cycles(self):
        middle = self.create_action_item("Middle dependent")
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=middle,
        )
        ActionItemDependency.objects.create(
            predecessor=middle,
            dependent=self.predecessor,
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError) as error,
        ):
            validate_action_item_dependents(
                self.predecessor,
                [self.dependent],
                self.user,
            )

        self.assertEqual(
            str(error.exception.detail["dependent_ids"]),
            CIRCULAR_DEPENDENCY_ERROR,
        )

    def test_dependent_replacement_is_atomic_and_preserves_other_types(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
            dependency_type="start_to_start",
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            patch.object(
                ActionItemPermission,
                "can_update_object",
                return_value=True,
            ),
        ):
            replace_action_item_dependents(
                self.predecessor,
                [self.dependent, self.replacement],
                self.user,
            )
            replace_action_item_dependents(
                self.predecessor,
                [self.replacement],
                self.user,
            )

        self.assertEqual(
            list(
                self.predecessor.outgoing_dependencies.filter(
                    dependency_type="finish_to_start",
                ).values_list("dependent_id", flat=True)
            ),
            [self.replacement.id],
        )
        self.assertTrue(
            self.predecessor.outgoing_dependencies.filter(
                dependent=self.dependent,
                dependency_type="start_to_start",
            ).exists()
        )

    def test_dependent_replacement_rejects_unauthorized_unlink(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )

        def can_update(_permission, _user, action_item):
            return action_item.id != self.dependent.id

        with (
            patch.object(
                ActionItemPermission,
                "can_update_object",
                autospec=True,
                side_effect=can_update,
            ),
            self.assertRaises(serializers.ValidationError) as error,
        ):
            replace_action_item_dependents(
                self.predecessor,
                [],
                self.user,
            )

        self.assertIn("permission", str(error.exception).lower())
        self.assertTrue(
            self.predecessor.outgoing_dependencies.filter(
                dependent=self.dependent,
                dependency_type="finish_to_start",
            ).exists()
        )

    def test_failed_dependent_replacement_keeps_existing_dependencies(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.replacement,
        )
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=self.predecessor,
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            patch.object(
                ActionItemPermission,
                "can_update_object",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError),
        ):
            replace_action_item_dependents(
                self.predecessor,
                [self.dependent],
                self.user,
            )

        self.assertEqual(
            list(
                self.predecessor.outgoing_dependencies.filter(
                    dependency_type="finish_to_start",
                ).values_list("dependent_id", flat=True)
            ),
            [self.replacement.id],
        )

    def test_failed_replacement_keeps_existing_dependencies(self):
        ActionItemDependency.objects.create(
            predecessor=self.predecessor,
            dependent=self.dependent,
        )
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=self.replacement,
        )

        with (
            patch(
                "project_management.action_item_dependencies."
                "require_any_permission_multiple_modules",
                return_value=True,
            ),
            self.assertRaises(serializers.ValidationError),
        ):
            replace_action_item_predecessors(
                self.dependent,
                [self.replacement],
                self.user,
            )

        self.assertEqual(
            list(
                self.dependent.incoming_dependencies.filter(
                    dependency_type="finish_to_start",
                ).values_list("predecessor_id", flat=True)
            ),
            [self.predecessor.id],
        )

    def test_date_mismatch_is_accepted(self):
        early_dependent = self.create_action_item("Early dependent")
        early_dependent.start_date = date(2026, 7, 1)
        early_dependent.save(update_fields=["start_date"])
        self.predecessor.target_date = date(2026, 8, 1)
        self.predecessor.save(update_fields=["target_date"])

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            replace_action_item_predecessors(
                early_dependent,
                [self.predecessor],
                self.user,
            )

        self.assertTrue(
            early_dependent.incoming_dependencies.filter(
                predecessor=self.predecessor,
            ).exists()
        )

    def test_multiple_predecessors_are_persisted(self):
        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            replace_action_item_predecessors(
                self.dependent,
                [self.predecessor, self.replacement],
                self.user,
            )

        self.assertCountEqual(
            self.dependent.incoming_dependencies.values_list(
                "predecessor_id",
                flat=True,
            ),
            [self.predecessor.id, self.replacement.id],
        )

    def test_candidate_search_matches_name_phase_and_id(self):
        project, first_phase, second_phase = self.create_project()
        dependent = self.create_action_item(
            "Project dependent",
            milestone=first_phase,
        )
        candidate = self.create_action_item(
            "Alpha predecessor",
            milestone=second_phase,
        )
        queryset = get_predecessor_candidate_queryset(dependent)

        self.assertEqual(
            list(
                search_predecessor_candidates(
                    queryset,
                    "Alpha",
                ).values_list("id", flat=True)
            ),
            [candidate.id],
        )
        self.assertIn(
            candidate.id,
            search_predecessor_candidates(
                queryset,
                "Second Phase",
            ).values_list("id", flat=True),
        )
        self.assertEqual(
            list(
                search_predecessor_candidates(
                    queryset,
                    str(candidate.id),
                ).values_list("id", flat=True)
            ),
            [candidate.id],
        )
        self.assertEqual(candidate.milestone.project_id, project.id)

    def test_dependent_candidate_search_matches_name_phase_and_id(self):
        project, first_phase, second_phase = self.create_project()
        predecessor = self.create_action_item(
            "Project predecessor",
            milestone=first_phase,
        )
        candidate = self.create_action_item(
            "Alpha dependent",
            milestone=second_phase,
        )
        queryset = get_dependent_candidate_queryset(predecessor)

        self.assertEqual(
            list(
                search_dependent_candidates(
                    queryset,
                    "Alpha",
                ).values_list("id", flat=True)
            ),
            [candidate.id],
        )
        self.assertIn(
            candidate.id,
            search_dependent_candidates(
                queryset,
                "Second Phase",
            ).values_list("id", flat=True),
        )
        self.assertEqual(
            list(
                search_dependent_candidates(
                    queryset,
                    str(candidate.id),
                ).values_list("id", flat=True)
            ),
            [candidate.id],
        )
        self.assertEqual(candidate.milestone.project_id, project.id)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=True,
    )
    def test_candidate_endpoint_searches_and_paginates(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.get(
            (
                f"/action-items/{self.dependent.id}/"
                "predecessor-candidates/?search=Predecessor&page_size=1"
            )
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"get": "predecessor_candidates"}
        )

        with patch(
            "project_management.views.action_item."
            "filter_predecessor_candidates_by_visibility",
            side_effect=lambda queryset, *_args: queryset,
        ):
            response = view(request, pk=self.dependent.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            self.predecessor.id,
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=False,
    )
    def test_candidate_endpoint_requires_update_permission(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.get(
            (
                f"/action-items/{self.dependent.id}/"
                "predecessor-candidates/"
            )
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"get": "predecessor_candidates"}
        )

        response = view(request, pk=self.dependent.id)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=True,
    )
    def test_dependent_candidate_endpoint_searches_and_paginates(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.get(
            (
                f"/action-items/{self.predecessor.id}/"
                "dependent-candidates/?search=Dependent&page_size=1"
            )
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"get": "dependent_candidates"}
        )

        with (
            patch(
                "project_management.views.action_item."
                "filter_dependency_candidates_by_visibility",
                side_effect=lambda queryset, *_args: queryset,
            ),
            patch(
                "project_management.views.action_item."
                "filter_dependent_candidates_by_update_permission",
                side_effect=lambda queryset, *_args: queryset,
            ),
        ):
            response = view(request, pk=self.predecessor.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["id"],
            self.dependent.id,
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=False,
    )
    def test_dependent_candidate_endpoint_requires_update_permission(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.get(
            (
                f"/action-items/{self.predecessor.id}/"
                "dependent-candidates/"
            )
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"get": "dependent_candidates"}
        )

        response = view(request, pk=self.predecessor.id)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=True,
    )
    def test_action_item_update_replaces_both_dependency_directions(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.patch(
            f"/action-items/{self.dependent.id}/",
            {
                "predecessor_ids": [self.predecessor.id],
                "dependent_ids": [self.replacement.id],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"patch": "partial_update"}
        )

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            response = view(request, pk=self.dependent.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["predecessors"]],
            [self.predecessor.id],
        )
        self.assertEqual(
            [item["id"] for item in response.data["dependents"]],
            [self.replacement.id],
        )
        self.assertTrue(
            ActionItemDependency.objects.filter(
                predecessor=self.predecessor,
                dependent=self.dependent,
                dependency_type="finish_to_start",
            ).exists()
        )
        self.assertTrue(
            ActionItemDependency.objects.filter(
                predecessor=self.dependent,
                dependent=self.replacement,
                dependency_type="finish_to_start",
            ).exists()
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=True,
    )
    def test_action_item_update_replaces_dependents_and_returns_links(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        request = self.factory.patch(
            f"/action-items/{self.predecessor.id}/",
            {"dependent_ids": [self.dependent.id, self.replacement.id]},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"patch": "partial_update"}
        )

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            response = view(request, pk=self.predecessor.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [dependent["id"] for dependent in response.data["dependents"]],
            [self.dependent.id, self.replacement.id],
        )
        self.assertCountEqual(
            self.predecessor.outgoing_dependencies.filter(
                dependency_type="finish_to_start",
            ).values_list("dependent_id", flat=True),
            [self.dependent.id, self.replacement.id],
        )

    @patch(
        "ecpmpapp.permissions.LicensePermission.has_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "has_object_permission",
        return_value=True,
    )
    @patch.object(
        ActionItemPermission,
        "can_update_object",
        return_value=True,
    )
    def test_action_item_update_returns_dependent_circular_error(
        self,
        _can_update,
        _has_object_permission,
        _license_permission,
    ):
        ActionItemDependency.objects.create(
            predecessor=self.dependent,
            dependent=self.predecessor,
        )
        request = self.factory.patch(
            f"/action-items/{self.predecessor.id}/",
            {"dependent_ids": [self.dependent.id]},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ActionItemViewSet.as_view(
            {"patch": "partial_update"}
        )

        with patch(
            "project_management.action_item_dependencies."
            "require_any_permission_multiple_modules",
            return_value=True,
        ):
            response = view(request, pk=self.predecessor.id)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.data["dependent_ids"]),
            CIRCULAR_DEPENDENCY_ERROR,
        )
