from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch

from models.models import CompanyProfile, User
from project_management.models import (
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
    ProjectTeamMembers,
    ProjectWorkstationNode,
)
from project_management.services.workstation_composition import (
    create_project_workstation,
)
from project_management.services.workstation_revision import (
    WorkstationRevisionConflict,
    commit_workstation_revision,
    preview_workstation_revision,
)
from project_management.views.workstation import ProjectWorkstationViewSet
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
)


class WorkstationRevisionTests(TestCase):
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
            email="revision-owner@example.com",
            password="password",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Revision Company",
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
            project_name="Revision Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Baseline workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            custom_fields={"quantity": 1, "unit": "station"},
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=self.template,
            assembly_name="Baseline assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            level=2,
        )
        self.workstation = create_project_workstation(
            project=self.project,
            source=self.template,
            actor=self.user,
            idempotency_key="revision-baseline",
        ).workstation
        self.product = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Servo kit",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            custom_fields={"quantity": 1, "unit": "kit"},
        )
        self.product_child = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            parent=self.product,
            assembly_name="Servo drive",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            level=2,
            custom_fields={"quantity": 2, "unit": "piece"},
        )
        self.factory = APIRequestFactory()

    def active_nodes(self):
        return self.workstation.nodes.filter(
            revision__number=self.workstation.active_revision,
        ).order_by("level", "sort_order", "id")

    def test_preview_and_commit_add_a_product_subtree_without_mutating_sources(self):
        root = self.active_nodes().get(parent=None)
        operations = [
            {
                "op": "add",
                "source_project_item": self.product.id,
                "parent_node": root.id,
                "include_subtree": True,
                "quantity": "3",
            }
        ]

        preview = preview_workstation_revision(
            workstation=self.workstation,
            operations=operations,
            expected_revision=1,
        )

        self.assertEqual(preview.summary["added"], 2)
        preview_by_name = {node["name"]: node for node in preview.nodes}
        self.assertEqual(preview_by_name["Servo kit"]["effective_quantity"], "3")
        self.assertEqual(preview_by_name["Servo drive"]["effective_quantity"], "6")
        self.assertEqual(
            preview_by_name["Servo drive"]["path"],
            "Baseline workstation / Servo kit / Servo drive",
        )
        self.assertEqual(self.workstation.revisions.count(), 1)
        result = commit_workstation_revision(
            workstation=self.workstation,
            operations=operations,
            expected_revision=1,
            actor=self.user,
            reason="Add upgraded servo kit",
            idempotency_key="add-servo-kit",
        )
        self.workstation.refresh_from_db()
        self.assertTrue(result.created)
        self.assertEqual(self.workstation.active_revision, 2)
        added = self.active_nodes().filter(role=ProjectWorkstationNode.ROLE_ADDED)
        self.assertEqual(added.count(), 2)
        self.assertEqual(added.get(source_project_item=self.product).quantity, 3)
        self.assertEqual(self.product.parent_id, None)
        self.assertEqual(self.product_child.parent_id, self.product.id)

        exclusion = preview_workstation_revision(
            workstation=self.workstation,
            operations=[{
                "op": "exclude",
                "node": added.get(source_project_item=self.product).id,
            }],
            expected_revision=2,
        )
        before = {node["name"]: node for node in exclusion.before_nodes}
        after = {node["name"]: node for node in exclusion.nodes}
        self.assertEqual(before["Servo drive"]["effective_quantity"], "6")
        self.assertEqual(after["Servo kit"]["effective_quantity"], "0")
        self.assertEqual(after["Servo drive"]["effective_quantity"], "0")
        self.assertEqual(self.workstation.revisions.count(), 2)

    def test_same_product_can_be_added_twice_as_distinct_occurrences(self):
        root = self.active_nodes().get(parent=None)
        first = commit_workstation_revision(
            workstation=self.workstation,
            operations=[
                {
                    "op": "add",
                    "source_project_item": self.product.id,
                    "parent_node": root.id,
                    "include_subtree": False,
                }
            ],
            expected_revision=1,
            actor=self.user,
            reason="First servo",
            idempotency_key="first-servo",
        )
        self.workstation.refresh_from_db()
        root = self.active_nodes().get(parent=None)
        second = commit_workstation_revision(
            workstation=self.workstation,
            operations=[
                {
                    "op": "add",
                    "source_project_item": self.product.id,
                    "parent_node": root.id,
                    "include_subtree": False,
                }
            ],
            expected_revision=2,
            actor=self.user,
            reason="Second servo",
            idempotency_key="second-servo",
        )

        self.workstation.refresh_from_db()
        occurrences = self.active_nodes().filter(source_project_item=self.product)
        self.assertEqual(occurrences.count(), 2)
        self.assertEqual(len(set(occurrences.values_list("occurrence_id", flat=True))), 2)
        self.assertNotEqual(first.revision.id, second.revision.id)

    def test_duplicate_occurrence_ids_are_rejected_before_applying_operations(self):
        root = self.active_nodes().get(parent=None)
        self.active_nodes().update(occurrence_id=root.occurrence_id)

        with self.assertRaisesMessage(
            ValidationError,
            "The active workstation contains duplicate occurrence identities.",
        ):
            preview_workstation_revision(
                workstation=self.workstation,
                operations=[
                    {
                        "op": "add",
                        "source_project_item": self.product.id,
                        "parent_node": root.id,
                        "include_subtree": True,
                    }
                ],
                expected_revision=1,
            )

    def test_replace_exclude_and_restore_preserve_revision_history_and_lineage(self):
        baseline = self.active_nodes().get(assembly_type="production_lines", level=2)
        replaced = commit_workstation_revision(
            workstation=self.workstation,
            operations=[
                {
                    "op": "replace",
                    "node": baseline.id,
                    "source_project_item": self.product.id,
                    "include_subtree": False,
                }
            ],
            expected_revision=1,
            actor=self.user,
            reason="Replace obsolete baseline",
            idempotency_key="replace-baseline",
        )
        self.workstation.refresh_from_db()
        replacement = self.active_nodes().get(
            role=ProjectWorkstationNode.ROLE_REPLACEMENT,
        )
        excluded = self.active_nodes().get(occurrence_id=baseline.occurrence_id)
        self.assertFalse(excluded.is_active)
        self.assertEqual(excluded.role, ProjectWorkstationNode.ROLE_EXCLUDED)
        self.assertEqual(replacement.supersedes_id, excluded.id)
        self.assertEqual(replaced.revision.reason, "Replace obsolete baseline")

        restored = commit_workstation_revision(
            workstation=self.workstation,
            operations=[{"op": "restore", "node": excluded.id}],
            expected_revision=2,
            actor=self.user,
            reason="Restore baseline for comparison",
            idempotency_key="restore-baseline",
        )
        self.workstation.refresh_from_db()
        restored_node = self.active_nodes().get(occurrence_id=baseline.occurrence_id)
        self.assertTrue(restored_node.is_active)
        self.assertEqual(restored_node.role, ProjectWorkstationNode.ROLE_BASELINE)
        self.assertEqual(restored.revision.number, 3)
        self.assertTrue(
            self.workstation.nodes.filter(revision__number=2, is_active=False).exists()
        )

    def test_move_reorder_and_quantity_are_applied_atomically(self):
        root = self.active_nodes().get(parent=None)
        baseline = self.active_nodes().get(level=2)
        commit_workstation_revision(
            workstation=self.workstation,
            operations=[
                {
                    "op": "add",
                    "source_project_item": self.product.id,
                    "parent_node": root.id,
                    "include_subtree": False,
                }
            ],
            expected_revision=1,
            actor=self.user,
            reason="Add movable part",
            idempotency_key="movable-part",
        )
        self.workstation.refresh_from_db()
        root = self.active_nodes().get(parent=None)
        added = self.active_nodes().get(source_project_item=self.product)
        commit_workstation_revision(
            workstation=self.workstation,
            operations=[
                {"op": "set_quantity", "node": added.id, "quantity": "4.5", "unit": "kit"},
                {"op": "move", "node": added.id, "parent_node": root.id, "sort_order": 0},
                {"op": "move", "node": baseline.id, "parent_node": root.id, "sort_order": 1},
            ],
            expected_revision=2,
            actor=self.user,
            reason="Tune quantities and order",
            idempotency_key="quantity-order",
        )

        self.workstation.refresh_from_db()
        current_added = self.active_nodes().get(occurrence_id=added.occurrence_id)
        self.assertEqual(current_added.quantity, 4.5)
        self.assertEqual(current_added.unit, "kit")
        self.assertEqual(current_added.sort_order, 0)

    def test_conflict_and_idempotent_retry_do_not_create_extra_revisions(self):
        root = self.active_nodes().get(parent=None)
        operations = [{"op": "set_quantity", "node": root.id, "quantity": "2"}]
        first = commit_workstation_revision(
            workstation=self.workstation,
            operations=operations,
            expected_revision=1,
            actor=self.user,
            reason="Two stations",
            idempotency_key="two-stations",
        )
        retry = commit_workstation_revision(
            workstation=self.workstation,
            operations=operations,
            expected_revision=1,
            actor=self.user,
            reason="Two stations",
            idempotency_key="two-stations",
        )
        self.assertTrue(first.created)
        self.assertFalse(retry.created)
        self.assertEqual(first.revision.id, retry.revision.id)
        self.assertEqual(self.workstation.revisions.count(), 2)

        with self.assertRaises(WorkstationRevisionConflict) as conflict:
            preview_workstation_revision(
                workstation=self.workstation,
                operations=operations,
                expected_revision=1,
            )
        self.assertEqual(conflict.exception.latest_revision, 2)

    def test_invalid_batches_and_cross_project_sources_roll_back(self):
        root = self.active_nodes().get(parent=None)
        baseline = self.active_nodes().get(level=2)
        with self.assertRaises(ValidationError):
            commit_workstation_revision(
                workstation=self.workstation,
                operations=[
                    {"op": "set_quantity", "node": baseline.id, "quantity": "2"},
                    {"op": "move", "node": root.id, "parent_node": baseline.id},
                ],
                expected_revision=1,
                actor=self.user,
                reason="Invalid cycle",
                idempotency_key="invalid-cycle",
            )
        self.assertEqual(self.workstation.revisions.count(), 1)

        other_project = Project.objects.create(
            project_name="Other project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
        )
        foreign_product = ProjectSolutionSpaceItem.objects.create(
            project=other_project,
            assembly_name="Foreign product",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        with self.assertRaises(ValidationError):
            preview_workstation_revision(
                workstation=self.workstation,
                operations=[
                    {
                        "op": "add",
                        "source_project_item": foreign_product.id,
                        "parent_node": root.id,
                    }
                ],
                expected_revision=1,
            )

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        side_effect=lambda *_args: bool(_args[-1].get("project")),
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_revision_detail_infers_project_context_without_query_parameter(
        self,
        _permissions,
        permission_lookup,
    ):
        root = self.active_nodes().get(parent=None)
        request = self.factory.post(
            "/preview-revision/",
            {
                "expected_revision": 1,
                "operations": [
                    {"op": "set_quantity", "node": root.id, "quantity": "2"},
                ],
            },
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = ProjectWorkstationViewSet.as_view(
            {"post": "preview_revision"},
        )(request, pk=self.workstation.id)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["summary"]["quantity_changed"], 1)
        before = {node["name"]: node for node in response.data["before_nodes"]}
        after = {node["name"]: node for node in response.data["nodes"]}
        self.assertEqual(before["Baseline workstation"]["quantity"], "1")
        self.assertEqual(after["Baseline workstation"]["quantity"], "2")
        self.assertEqual(before["Baseline assembly"]["effective_quantity"], "1")
        self.assertEqual(after["Baseline assembly"]["effective_quantity"], "2")
        permission_lookup.assert_any_call(
            self.user,
            {"project_assembly_catalog": ["view", "full_access"]},
            "project",
            {"project": self.project.id},
        )

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_inferred_detail_context_preserves_tenant_isolation(
        self,
        _permissions,
        _permission_lookup,
    ):
        other_user = User.objects.create_user(
            email="other-revision-owner@example.com",
            password="password",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_user,
            company_name="Other Revision Company",
            company_address="Street 2",
            company_town="Elsewhere",
            company_tell_no="789",
            company_mob_no="012",
            company_web_address="https://other.example.com",
            company_email_address=other_user.email,
            company_tax_reference="other-tax",
            company_iban="other-iban",
        )
        other_user.company = other_company
        other_user.save(update_fields=["company"])
        other_project = Project.objects.create(
            project_name="Other Revision Project",
            contract_value=1000,
            company_profile=other_company,
            user=other_user,
            created_by=other_user,
        )
        other_template = CompanySolutionSpaceItem.objects.create(
            company_profile=other_company,
            assembly_name="Other workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        other_workstation = create_project_workstation(
            project=other_project,
            source=other_template,
            actor=other_user,
            idempotency_key="other-revision-baseline",
        ).workstation
        request = self.factory.get("/revisions/")
        force_authenticate(request, user=self.user)

        response = ProjectWorkstationViewSet.as_view(
            {"get": "revisions"},
        )(request, pk=other_workstation.id)

        self.assertEqual(response.status_code, 404)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_revision_api_previews_commits_retries_and_reports_conflicts(
        self,
        _permissions,
        _permission_lookup,
    ):
        root = self.active_nodes().get(parent=None)
        payload = {
            "expected_revision": 1,
            "reason": "Add servo through the API",
            "operations": [
                {
                    "op": "add",
                    "source_project_item": self.product.id,
                    "parent_node": root.id,
                    "include_subtree": True,
                }
            ],
        }
        preview_request = self.factory.post("/preview-revision/", payload, format="json")
        force_authenticate(preview_request, user=self.user)
        preview_response = ProjectWorkstationViewSet.as_view(
            {"post": "preview_revision"},
        )(preview_request, pk=self.workstation.id)
        self.assertEqual(preview_response.status_code, 200, preview_response.data)
        self.assertEqual(preview_response.data["resulting_revision"], 2)
        self.assertEqual(preview_response.data["summary"]["added"], 2)
        self.assertEqual(self.workstation.revisions.count(), 1)

        commit_payload = {**payload, "idempotency_key": "api-add-servo"}
        commit_view = ProjectWorkstationViewSet.as_view({"post": "commit_revision"})
        commit_request = self.factory.post(
            "/commit-revision/",
            commit_payload,
            format="json",
        )
        force_authenticate(commit_request, user=self.user)
        commit_response = commit_view(commit_request, pk=self.workstation.id)
        retry_request = self.factory.post(
            "/commit-revision/",
            commit_payload,
            format="json",
        )
        force_authenticate(retry_request, user=self.user)
        retry_response = commit_view(retry_request, pk=self.workstation.id)
        self.assertEqual(commit_response.status_code, 201, commit_response.data)
        self.assertEqual(retry_response.status_code, 200, retry_response.data)
        self.assertEqual(commit_response.data["revision"]["id"], retry_response.data["revision"]["id"])

        conflict_request = self.factory.post(
            "/preview-revision/",
            {**payload, "operations": [{"op": "set_quantity", "node": root.id, "quantity": 2}]},
            format="json",
        )
        force_authenticate(conflict_request, user=self.user)
        conflict_response = ProjectWorkstationViewSet.as_view(
            {"post": "preview_revision"},
        )(conflict_request, pk=self.workstation.id)
        self.assertEqual(conflict_response.status_code, 409, conflict_response.data)
        self.assertEqual(conflict_response.data["latest_revision"], 2)
