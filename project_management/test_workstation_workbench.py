from django.core.exceptions import ValidationError
from django.test import override_settings
from unittest.mock import patch
from rest_framework.test import force_authenticate
from project_management.models import Project
from project_management.views.workstation import ProjectWorkstationViewSet

from project_management.test_workstation_revisions import WorkstationRevisionTests
from project_management.services.workstation_composition import create_project_workstation
from project_management.services.workstation_revision import WorkstationRevisionConflict
from project_management.services.workstation_workbench import workbench_edit


class WorkbenchTests(WorkstationRevisionTests):
    def request_payload(self, **overrides):
        root = self.active_nodes().get(parent=None)
        child = self.active_nodes().get(parent=root)
        return {"mode": "copy", "source_node": child.pk, "expected_revision": 1,
                "targets": [{"id": self.workstation.pk, "expected_revision": 1, "parent_node": root.pk}],
                "reason": "Duplicate reviewed assembly", "idempotency_key": "copy-test", "commit": False,
                **overrides}

    def test_copy_preview_commit_and_retry_preserve_source_and_reset_approval(self):
        self.active_nodes().update(approval_status="agreed")
        payload = self.request_payload()
        preview = workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(preview[0]["summary"]["added"], 1)
        self.assertEqual(self.workstation.revisions.count(), 1)
        payload["commit"] = True
        workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.workstation.refresh_from_db()
        nodes = self.active_nodes()
        self.assertEqual(nodes.count(), 3)
        self.assertEqual(nodes.filter(approval_status="open").count(), 3)
        copied = nodes.get(role="added")
        self.assertIn("_copied_from", copied.custom_fields)
        self.assertNotEqual(str(copied.occurrence_id), copied.custom_fields["_copied_from"]["occurrence"])
        workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(self.workstation.revisions.count(), 2)
        payload["reason"] = "Different request"
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)

    def test_cross_workstation_copy_and_same_source_target_are_atomic(self):
        other = create_project_workstation(project=self.project, source=self.template,
            actor=self.user, idempotency_key="other").workstation
        payload = self.request_payload(commit=True)
        payload["targets"].append({"id": other.pk, "expected_revision": 1,
            "parent_node": other.nodes.get(parent=None).pk})
        workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        for station in (self.workstation, other):
            station.refresh_from_db()
            self.assertEqual(station.active_revision, 2)
            self.assertEqual(station.nodes.filter(revision__number=2).count(), 3)

    def test_stale_target_does_not_commit_any_workstation(self):
        other = create_project_workstation(project=self.project, source=self.template,
            actor=self.user, idempotency_key="other").workstation
        payload = self.request_payload(commit=True)
        payload["targets"].append({"id": other.pk, "expected_revision": 99,
            "parent_node": other.nodes.get(parent=None).pk})
        with self.assertRaises(WorkstationRevisionConflict):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(self.workstation.revisions.count(), 1)
        self.assertEqual(other.revisions.count(), 1)

    def test_failure_during_second_commit_rolls_back_first_target(self):
        from project_management.services.workstation_revision import commit_workstation_revision
        other = create_project_workstation(project=self.project, source=self.template,
            actor=self.user, idempotency_key="rollback-target").workstation
        payload = self.request_payload(commit=True)
        payload["targets"].append({"id": other.pk, "expected_revision": 1,
            "parent_node": other.nodes.get(parent=None).pk})
        calls = []
        def fail_second(**kwargs):
            calls.append(kwargs["workstation"].pk)
            if len(calls) == 2:
                raise ValidationError("Concurrent catalog validation failure")
            return commit_workstation_revision(**kwargs)
        with patch("project_management.services.workstation_workbench.commit_workstation_revision", side_effect=fail_second):
            with self.assertRaises(ValidationError):
                workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(len(calls), 2)
        for station in (self.workstation, other):
            station.refresh_from_db()
            self.assertEqual(station.active_revision, 1)
            self.assertEqual(station.revisions.count(), 1)

    def test_duplicate_targets_and_leaf_destinations_are_rejected(self):
        payload = self.request_payload(commit=True)
        payload["targets"].append(dict(payload["targets"][0]))
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        payload = self.request_payload(commit=True)
        payload["targets"][0]["parent_node"] = payload["source_node"]
        self.active_nodes().filter(pk=payload["source_node"]).update(assembly_type="components")
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(self.workstation.revisions.count(), 1)

    def test_copy_rejects_excluded_source_invalid_parent_and_archived_target(self):
        payload = self.request_payload()
        payload["targets"][0]["parent_node"] = 999999
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        payload = self.request_payload()
        self.active_nodes().filter(pk=payload["source_node"]).update(is_active=False)
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.workstation.status = "archived"
        self.workstation.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)

    def test_bulk_replace_matches_catalog_identity_and_keeps_occurrence_quantity(self):
        payload = self.request_payload(mode="replace", replacement=self.product.pk, commit=True)
        original = self.active_nodes().get(pk=payload["source_node"])
        original.quantity = 3
        original.save(update_fields=["quantity"])
        workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.workstation.refresh_from_db()
        self.assertEqual(self.active_nodes().get(role="replacement").quantity, 3)
        self.assertFalse(self.active_nodes().get(occurrence_id=original.occurrence_id).is_active)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch("project_management.views.workstation.require_any_permission_multiple_modules", return_value=True)
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_workbench_api_preview_commit_and_invalid_request(self, *_mocks):
        view = ProjectWorkstationViewSet.as_view({"post": "workbench"})
        payload = self.request_payload()
        request = self.factory.post("/workbench/", payload, format="json")
        force_authenticate(request, user=self.user)
        response = view(request, pk=self.workstation.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data["committed"])
        payload["commit"] = True
        request = self.factory.post("/workbench/", payload, format="json")
        force_authenticate(request, user=self.user)
        response = view(request, pk=self.workstation.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["committed"])
        payload["targets"] = []
        request = self.factory.post("/workbench/", payload, format="json")
        force_authenticate(request, user=self.user)
        self.assertEqual(view(request, pk=self.workstation.pk).status_code, 400)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch("project_management.views.workstation.require_any_permission_multiple_modules", return_value=True)
    @patch("project_management.permissions.MatrixModulePermission._check_update_permission_project", return_value=False)
    def test_workbench_api_requires_update_permission(self, *_mocks):
        request = self.factory.post("/workbench/", self.request_payload(), format="json")
        force_authenticate(request, user=self.user)
        response = ProjectWorkstationViewSet.as_view({"post": "workbench"})(request, pk=self.workstation.pk)
        self.assertEqual(response.status_code, 403)

    def test_cross_project_target_is_rejected_without_writes(self):
        project = Project.objects.create(project_name="Other project", contract_value=1000,
            company_profile=self.company, user=self.user, created_by=self.user)
        other = create_project_workstation(project=project, source=self.template, actor=self.user,
            idempotency_key="other-project").workstation
        payload = self.request_payload(commit=True)
        payload["targets"] = [{"id": other.pk, "expected_revision": 1,
            "parent_node": other.nodes.get(parent=None).pk}]
        with self.assertRaises(ValidationError):
            workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.assertEqual(other.revisions.count(), 1)
        self.assertEqual(self.workstation.revisions.count(), 1)

    def test_duplicate_child_of_product_assembly_preserves_hierarchy(self):
        from project_management.services.workstation_revision import commit_workstation_revision
        root = self.active_nodes().get(parent=None)
        commit_workstation_revision(workstation=self.workstation,
            operations=[{"op": "add", "source_project_item": self.product.pk, "parent_node": root.pk}],
            expected_revision=1, actor=self.user, reason="Add composite product", idempotency_key="composite")
        self.workstation.refresh_from_db()
        parent = self.active_nodes().get(source_project_item=self.product)
        child = self.active_nodes().get(source_project_item=self.product_child)
        payload = {"mode": "copy", "source_node": child.pk, "expected_revision": 2,
            "targets": [{"id": self.workstation.pk, "expected_revision": 2, "parent_node": parent.pk}],
            "reason": "Duplicate nested part", "idempotency_key": "nested-copy", "commit": True}
        workbench_edit(source=self.workstation, payload=payload, actor=self.user)
        self.workstation.refresh_from_db()
        self.assertEqual(self.active_nodes().filter(source_project_item=self.product_child).count(), 2)
