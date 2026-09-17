import uuid
from datetime import timedelta

from django.db.models.signals import post_save
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch

from models.models import CompanyProfile, User
from project_management.models import (
    BillOfMaterialPart,
    BomGenerationRun,
    BomPlanOperation,
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
    ProjectTeamMembers,
)
from project_management.services.workstation_bom import (
    BomPlanConflict,
    apply_bom_plan,
    create_bom_plan,
    process_queued_bom_plan,
    resolve_bom_plan_conflicts,
)
from project_management.services.workstation_composition import create_project_workstation
from project_management.services.workstation_revision import commit_workstation_revision
from project_management.views.bom import BomGenerationRunViewSet
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
)


class WorkstationBomPlanTests(TestCase):
    agree_initial_fixture = True

    def approve_station(self, workstation):
        workstation.refresh_from_db()
        root = workstation.nodes.get(revision__number=workstation.active_revision, parent=None)
        commit_workstation_revision(
            workstation=workstation, actor=self.user,
            expected_revision=workstation.active_revision,
            idempotency_key=str(uuid.uuid4()), reason="Fixture engineering review",
            operations=[{"op": "set_approval", "node": root.id,
                         "approval_status": "agreed", "include_subtree": True}],
        )
        workstation.refresh_from_db()

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
            email="bom-owner@example.com",
            password="password",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="BoM Company",
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
            project_name="BoM Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        self.template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Packaging workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            custom_fields={"quantity": 1, "unit": "station"},
        )
        self.assembly = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=self.template,
            assembly_name="Frame assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            level=2,
            custom_fields={"quantity": 2, "unit": "set"},
        )
        self.leaf = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=self.assembly,
            assembly_name="Mounting plate",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            level=3,
            custom_fields={"quantity": 3, "unit": "piece"},
        )
        self.workstations = []
        for index in range(1, 6):
            workstation = create_project_workstation(
                project=self.project,
                source=self.template,
                actor=self.user,
                idempotency_key=f"station-{index}",
            ).workstation
            workstation.name = f"Station {index}"
            workstation.save(update_fields=["name", "updated_at"])
            self.workstations.append(workstation)
            if self.agree_initial_fixture:
                self.approve_station(workstation)
        self.factory = APIRequestFactory()

    def test_plan_preserves_five_hierarchies_and_multiplies_leaf_quantities(self):
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id for workstation in self.workstations],
            actor=self.user,
            idempotency_key="five-stations-preview",
        )

        self.assertEqual(plan.summary["add"], 15)
        self.assertEqual(plan.summary["detach"], 0)
        root_operations = plan.operations.filter(after_values__level=1)
        self.assertEqual(root_operations.count(), 5)
        leaf_operations = plan.operations.filter(after_values__purchasable=True)
        self.assertEqual(leaf_operations.count(), 5)
        self.assertEqual(
            {operation.after_values["engineering_quantity"] for operation in leaf_operations},
            {"6"},
        )

        result = apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
            confirm_detach=False,
        )
        self.assertTrue(result.created)
        roots = BillOfMaterialPart.objects.filter(
            project=self.project,
            parent=None,
            source_workstation__isnull=False,
        )
        self.assertEqual(roots.count(), 5)
        self.assertEqual(
            BillOfMaterialPart.objects.filter(
                project=self.project,
                is_purchasable=True,
                engineering_quantity=6,
            ).count(),
            5,
        )
        self.assertEqual(
            list(
                BillOfMaterialPart.objects.filter(project=self.project)
                .order_by("sort_order", "id")
                .values_list("name", flat=True)
            ),
            [
                name
                for index in range(1, 6)
                for name in (f"Station {index}", "Frame assembly", "Mounting plate")
            ],
        )

    def test_regeneration_detaches_rows_from_a_deselected_workstation(self):
        selected, deselected = self.workstations[:2]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[selected.id, deselected.id],
            actor=self.user,
            idempotency_key="two-stations-initial",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )

        regeneration = create_bom_plan(
            project=self.project,
            workstation_ids=[selected.id],
            actor=self.user,
            idempotency_key="one-station-regeneration",
        )

        self.assertEqual(regeneration.summary["detach"], 3)
        self.assertEqual(
            regeneration.operations.filter(
                operation=BomPlanOperation.OPERATION_DETACH,
                source_workstation=deselected,
            ).count(),
            3,
        )
        with self.assertRaises(BomPlanConflict):
            apply_bom_plan(
                plan=regeneration,
                actor=self.user,
                expected_target_hash=regeneration.target_hash,
            )

        apply_bom_plan(
            plan=regeneration,
            actor=self.user,
            expected_target_hash=regeneration.target_hash,
            confirm_detach=True,
        )
        self.assertFalse(
            BillOfMaterialPart.objects.filter(
                project=self.project,
                source_workstation=selected,
                engineering_detached=True,
            ).exists(),
        )
        self.assertEqual(
            BillOfMaterialPart.objects.filter(
                project=self.project,
                source_workstation=deselected,
                engineering_detached=True,
            ).count(),
            3,
        )

    def test_repeated_part_occurrences_remain_distinct_and_reapply_is_idempotent(self):
        workstation = self.workstations[0]
        product = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Servo drive",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            custom_fields={"quantity": 2, "unit": "piece"},
        )
        root = workstation.nodes.get(revision__number=1, parent=None)
        commit_workstation_revision(
            workstation=workstation,
            operations=[
                {
                    "op": "add",
                    "source_project_item": product.id,
                    "parent_node": root.id,
                    "include_subtree": False,
                },
                {
                    "op": "add",
                    "source_project_item": product.id,
                    "parent_node": root.id,
                    "include_subtree": False,
                },
            ],
            expected_revision=workstation.active_revision,
            actor=self.user,
            reason="Two servo occurrences",
            idempotency_key="two-servos",
        )
        workstation.refresh_from_db()
        self.approve_station(workstation)
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="repeated-parts-preview",
        )
        apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
        )
        retry = apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
        )

        self.assertFalse(retry.created)
        occurrences = BillOfMaterialPart.objects.filter(
            project=self.project,
            name="Servo drive",
        )
        self.assertEqual(occurrences.count(), 2)
        self.assertEqual(
            len(set(occurrences.values_list("source_occurrence_id", flat=True))),
            2,
        )

    def test_regeneration_preserves_procurement_and_manual_rows_and_requires_detach_confirmation(self):
        workstation = self.workstations[0]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="initial-plan",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )
        generated_leaf = BillOfMaterialPart.objects.get(
            project=self.project,
            name="Mounting plate",
        )
        generated_leaf.custom_fields = {
            "order_quantity": 99,
            "supplier": "Chosen Supplier",
            "price": "12.50",
            "sourcing_status": "ordered",
            "notes": "Do not overwrite",
        }
        generated_leaf.status = "in progress"
        generated_leaf.part_number = "PROC-001"
        generated_leaf.save()
        manual = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Manual procurement row",
            custom_fields={"notes": "manual"},
            created_by=self.user,
        )
        workstation.refresh_from_db()
        leaf_node = workstation.nodes.get(
            revision__number=workstation.active_revision,
            name="Mounting plate",
        )
        commit_workstation_revision(
            workstation=workstation,
            operations=[{"op": "exclude", "node": leaf_node.id}],
            expected_revision=workstation.active_revision,
            actor=self.user,
            reason="Remove old plate",
            idempotency_key="exclude-old-plate",
        )
        self.approve_station(workstation)
        regeneration = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="regeneration-plan",
        )
        self.assertEqual(regeneration.summary["detach"], 1)
        with self.assertRaises(BomPlanConflict):
            apply_bom_plan(
                plan=regeneration,
                actor=self.user,
                expected_target_hash=regeneration.target_hash,
                confirm_detach=False,
            )
        apply_bom_plan(
            plan=regeneration,
            actor=self.user,
            expected_target_hash=regeneration.target_hash,
            confirm_detach=True,
        )

        generated_leaf.refresh_from_db()
        manual.refresh_from_db()
        self.assertTrue(generated_leaf.engineering_detached)
        self.assertEqual(generated_leaf.custom_fields["order_quantity"], 99)
        self.assertEqual(generated_leaf.custom_fields["supplier"], "Chosen Supplier")
        self.assertEqual(generated_leaf.status, "in progress")
        self.assertEqual(generated_leaf.part_number, "PROC-001")
        self.assertEqual(manual.custom_fields, {"notes": "manual"})

    def test_duplicate_lineage_conflict_requires_and_applies_explicit_resolution(self):
        workstation = self.workstations[0]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="conflict-initial",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )
        original = BillOfMaterialPart.objects.get(
            project=self.project,
            name="Mounting plate",
        )
        original.custom_fields = {"supplier": "keep-this-procurement"}
        original.save(update_fields=["custom_fields", "updated_at"])
        duplicate = BillOfMaterialPart.objects.create(
            project=self.project,
            parent=original.parent,
            name="Duplicate old plate",
            level=original.level,
            sort_order=original.sort_order,
            source_workstation=original.source_workstation,
            source_workstation_snapshot=original.source_workstation_snapshot,
            source_occurrence_id=original.source_occurrence_id,
            engineering_quantity=original.engineering_quantity,
            engineering_unit=original.engineering_unit,
            is_purchasable=original.is_purchasable,
            custom_fields={"supplier": "detach-this-procurement"},
            created_by=self.user,
        )
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="conflict-preview",
        )
        conflict = plan.operations.get(
            operation=BomPlanOperation.OPERATION_CONFLICT,
        )
        self.assertEqual(
            {candidate["id"] for candidate in conflict.before_values["duplicate_targets"]},
            {original.id, duplicate.id},
        )
        with self.assertRaises(BomPlanConflict):
            apply_bom_plan(
                plan=plan,
                actor=self.user,
                expected_target_hash=plan.target_hash,
            )

        resolve_bom_plan_conflicts(
            plan=plan,
            resolutions=[
                {
                    "operation": conflict.id,
                    "decision": "use_workstation",
                    "target_bom_part": original.id,
                },
            ],
        )
        apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
        )

        original.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertEqual(original.name, "Mounting plate")
        self.assertEqual(original.custom_fields["supplier"], "keep-this-procurement")
        self.assertFalse(original.engineering_detached)
        self.assertEqual(duplicate.custom_fields["supplier"], "detach-this-procurement")
        self.assertTrue(duplicate.engineering_detached)

    def test_keep_existing_resolution_leaves_duplicate_rows_untouched(self):
        workstation = self.workstations[0]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="keep-conflict-initial",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )
        original = BillOfMaterialPart.objects.get(project=self.project, name="Mounting plate")
        duplicate = BillOfMaterialPart.objects.create(
            project=self.project,
            parent=original.parent,
            name="Manual duplicate",
            level=original.level,
            source_workstation=original.source_workstation,
            source_workstation_snapshot=original.source_workstation_snapshot,
            source_occurrence_id=original.source_occurrence_id,
            created_by=self.user,
        )
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="keep-conflict-preview",
        )
        conflict = plan.operations.get(operation=BomPlanOperation.OPERATION_CONFLICT)
        resolve_bom_plan_conflicts(
            plan=plan,
            resolutions=[{"operation": conflict.id, "decision": "keep_existing"}],
        )
        apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
        )
        duplicate.refresh_from_db()
        self.assertFalse(duplicate.engineering_detached)
        self.assertEqual(duplicate.name, "Manual duplicate")

    def test_keep_existing_parent_conflict_keeps_new_descendants_in_the_hierarchy(self):
        workstation = self.workstations[0]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="parent-conflict-initial",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )
        root = BillOfMaterialPart.objects.get(project=self.project, parent=None)
        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Duplicate workstation root",
            level=root.level,
            source_workstation=root.source_workstation,
            source_workstation_snapshot=root.source_workstation_snapshot,
            source_occurrence_id=root.source_occurrence_id,
            created_by=self.user,
        )
        product = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="New child after conflict",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        workstation_root = workstation.nodes.get(revision__number=1, parent=None)
        commit_workstation_revision(
            workstation=workstation,
            operations=[
                {
                    "op": "add",
                    "source_project_item": product.id,
                    "parent_node": workstation_root.id,
                    "include_subtree": False,
                },
            ],
            expected_revision=workstation.active_revision,
            actor=self.user,
            reason="Add a child below the conflicted root",
            idempotency_key="parent-conflict-child",
        )
        self.approve_station(workstation)
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="parent-conflict-preview",
        )
        conflict = plan.operations.get(
            operation=BomPlanOperation.OPERATION_CONFLICT,
            after_values__level=1,
        )
        resolve_bom_plan_conflicts(
            plan=plan,
            resolutions=[{"operation": conflict.id, "decision": "keep_existing"}],
        )
        apply_bom_plan(
            plan=plan,
            actor=self.user,
            expected_target_hash=plan.target_hash,
        )
        created = BillOfMaterialPart.objects.get(
            project=self.project,
            name="New child after conflict",
        )
        self.assertEqual(created.parent_id, root.id)

    @override_settings(PROCESS_ASSEMBLY_BOM_ENABLED=True)
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_api_resolves_every_conflict_before_apply(self, _permissions):
        workstation = self.workstations[0]
        initial = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="api-conflict-initial",
        )
        apply_bom_plan(
            plan=initial,
            actor=self.user,
            expected_target_hash=initial.target_hash,
        )
        original = BillOfMaterialPart.objects.get(project=self.project, name="Mounting plate")
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=original.parent,
            name="API duplicate",
            level=original.level,
            source_workstation=original.source_workstation,
            source_workstation_snapshot=original.source_workstation_snapshot,
            source_occurrence_id=original.source_occurrence_id,
            created_by=self.user,
        )
        plan = create_bom_plan(
            project=self.project,
            workstation_ids=[workstation.id],
            actor=self.user,
            idempotency_key="api-conflict-preview",
        )
        conflict = plan.operations.get(operation=BomPlanOperation.OPERATION_CONFLICT)
        resolve_view = BomGenerationRunViewSet.as_view({"post": "resolve_conflicts"})
        request = self.factory.post(
            f"/api/project_management/bom-generation-runs/{plan.id}/resolve-conflicts/",
            {
                "resolutions": [
                    {"operation": conflict.id, "decision": "keep_existing"},
                ],
            },
            format="json",
        )
        force_authenticate(request, self.user)
        response = resolve_view(request, pk=plan.id)
        self.assertEqual(response.status_code, 200)
        resolved = next(
            operation
            for operation in response.data["operations"]
            if operation["operation"] == "conflict"
        )
        self.assertEqual(resolved["resolution_state"], "resolved_keep_existing")

    @override_settings(PROCESS_ASSEMBLY_BOM_ENABLED=True)
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_api_previews_lists_applies_idempotently_and_rejects_stale_targets(
        self,
        _permissions,
    ):
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [self.workstations[0].id],
                "idempotency_key": "api-preview",
            },
            format="json",
        )
        force_authenticate(request, self.user)
        preview = create_view(request)
        self.assertEqual(preview.status_code, 201)
        self.assertEqual(preview.data["summary"]["add"], 3)

        list_view = BomGenerationRunViewSet.as_view({"get": "list"})
        request = self.factory.get(
            "/api/project_management/bom-generation-runs/",
            {"project": self.project.id, "page_size": 25},
        )
        force_authenticate(request, self.user)
        response = list_view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

        manual = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Concurrent manual row",
            created_by=self.user,
        )
        apply_view = BomGenerationRunViewSet.as_view({"post": "apply"})
        request = self.factory.post(
            f"/api/project_management/bom-generation-runs/{preview.data['id']}/apply/",
            {"expected_target_hash": preview.data["target_hash"]},
            format="json",
        )
        force_authenticate(request, self.user)
        stale = apply_view(request, pk=preview.data["id"])
        self.assertEqual(stale.status_code, 409)
        stale_plan = self.project.bom_generation_runs.get(pk=preview.data["id"])
        self.assertEqual(stale_plan.status, "stale")
        self.assertEqual(stale_plan.metrics["stale_plan_rejections"], 1)
        self.assertEqual(stale_plan.metrics["last_stale_reason"], "target_hash_changed")
        manual.delete()

        retry_request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [self.workstations[0].id],
                "idempotency_key": "api-preview-fresh",
            },
            format="json",
        )
        force_authenticate(retry_request, self.user)
        fresh = create_view(retry_request)
        apply_request = self.factory.post(
            f"/api/project_management/bom-generation-runs/{fresh.data['id']}/apply/",
            {"expected_target_hash": fresh.data["target_hash"]},
            format="json",
        )
        force_authenticate(apply_request, self.user)
        applied = apply_view(apply_request, pk=fresh.data["id"])
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(applied.data["status"], "applied")
        retry_request = self.factory.post(
            f"/api/project_management/bom-generation-runs/{fresh.data['id']}/apply/",
            {"expected_target_hash": fresh.data["target_hash"]},
            format="json",
        )
        force_authenticate(retry_request, self.user)
        retried = apply_view(retry_request, pk=fresh.data["id"])
        self.assertEqual(retried.status_code, 200)
        self.assertFalse(retried.data["created"])

    @override_settings(
        PROCESS_ASSEMBLY_BOM_ENABLED=True,
        PROCESS_BOM_ASYNC_NODE_THRESHOLD=1,
    )
    @patch("project_management.tasks.process_bom_generation_run.apply_async")
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_large_plan_is_queued_then_materialized_with_structured_metrics(
        self,
        _permissions,
        apply_async,
    ):
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [self.workstations[0].id],
                "idempotency_key": "async-preview",
            },
            format="json",
        )
        force_authenticate(request, self.user)

        queued = create_view(request)

        self.assertEqual(queued.status_code, 202)
        self.assertEqual(queued.data["status"], "queued")
        self.assertEqual(queued.data["node_count"], 3)
        apply_async.assert_called_once_with(
            args=[queued.data["id"]],
            queue="default",
        )

        legacy_preview = create_bom_plan(
            project=self.project,
            workstation_ids=[self.workstations[1].id],
            actor=self.user,
            idempotency_key=f"bom-worker-{queued.data['id']}",
        )

        queued_plan = BomGenerationRun.objects.get(pk=queued.data["id"])
        queued_plan.metrics = {**queued_plan.metrics, "retry_count": 1, "failure_count": 1}
        queued_plan.error_details = {"code": "previous_attempt_failed"}
        queued_plan.save(update_fields=["metrics", "error_details", "updated_at"])

        plan = process_queued_bom_plan(queued.data["id"])

        self.assertEqual(plan.status, "preview")
        self.assertEqual(plan.operations.count(), 3)
        self.assertEqual(plan.metrics["planning"]["mode"], "asynchronous")
        self.assertEqual(plan.metrics["planning"]["node_count"], 3)
        self.assertEqual(plan.metrics["planning"]["operation_counts"]["add"], 3)
        self.assertEqual(plan.metrics["retry_count"], 1)
        self.assertEqual(plan.metrics["failure_count"], 1)
        self.assertEqual(plan.error_details, {})
        self.assertTrue(BomGenerationRun.objects.filter(pk=legacy_preview.id).exists())
        self.assertEqual(legacy_preview.operations.count(), 3)
        self.assertEqual(
            plan.operations.get(after_values__level=1).after_values["name"],
            "Station 1",
        )

    @override_settings(PROCESS_ASSEMBLY_BOM_ENABLED=True)
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_api_rejects_the_internal_worker_idempotency_namespace(self, _permissions):
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [self.workstations[0].id],
                "idempotency_key": "__bom_worker__:client-controlled",
            },
            format="json",
        )
        force_authenticate(request, self.user)

        response = create_view(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(BomGenerationRun.objects.count(), 0)

    @override_settings(PROCESS_ASSEMBLY_BOM_ENABLED=True)
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_api_never_exposes_or_applies_internal_worker_runs(self, _permissions):
        internal = create_bom_plan(
            project=self.project,
            workstation_ids=[self.workstations[0].id],
            actor=self.user,
            idempotency_key="__bom_worker__:internal-only",
            internal_run=True,
        )
        list_view = BomGenerationRunViewSet.as_view({"get": "list"})
        list_request = self.factory.get(
            "/api/project_management/bom-generation-runs/",
            {"project": self.project.id},
        )
        force_authenticate(list_request, self.user)
        listed = list_view(list_request)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.data["count"], 0)

        retrieve_view = BomGenerationRunViewSet.as_view({"get": "retrieve"})
        retrieve_request = self.factory.get(
            f"/api/project_management/bom-generation-runs/{internal.id}/",
        )
        force_authenticate(retrieve_request, self.user)
        retrieved = retrieve_view(retrieve_request, pk=internal.id)
        self.assertEqual(retrieved.status_code, 404)

        apply_view = BomGenerationRunViewSet.as_view({"post": "apply"})
        apply_request = self.factory.post(
            f"/api/project_management/bom-generation-runs/{internal.id}/apply/",
            {"expected_target_hash": internal.target_hash},
            format="json",
        )
        force_authenticate(apply_request, self.user)
        applied = apply_view(apply_request, pk=internal.id)
        self.assertEqual(applied.status_code, 404)
        internal.refresh_from_db()
        self.assertEqual(internal.status, BomGenerationRun.STATUS_PREVIEW)

    @override_settings(
        PROCESS_ASSEMBLY_BOM_ENABLED=True,
        PROCESS_BOM_ASYNC_NODE_THRESHOLD=1,
    )
    @patch("project_management.tasks.process_bom_generation_run.apply_async")
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_queued_plan_is_dispatched_once_and_rejects_target_changes(
        self,
        _permissions,
        apply_async,
    ):
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        payload = {
            "project": self.project.id,
            "workstations": [self.workstations[0].id],
            "idempotency_key": "async-target-stale",
        }
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            payload,
            format="json",
        )
        force_authenticate(request, self.user)
        queued = create_view(request)
        original_target_hash = queued.data["target_hash"]

        duplicate_request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            payload,
            format="json",
        )
        force_authenticate(duplicate_request, self.user)
        duplicate = create_view(duplicate_request)
        self.assertEqual(duplicate.data["id"], queued.data["id"])
        apply_async.assert_called_once()

        BillOfMaterialPart.objects.create(
            project=self.project,
            name="Added while queued",
            created_by=self.user,
        )
        plan = process_queued_bom_plan(queued.data["id"])

        self.assertEqual(plan.status, BomGenerationRun.STATUS_STALE)
        self.assertEqual(plan.operations.count(), 0)
        self.assertEqual(plan.target_hash, original_target_hash)
        self.assertEqual(plan.metrics["last_stale_reason"], "target_hash_changed")

    @override_settings(
        PROCESS_ASSEMBLY_BOM_ENABLED=True,
        PROCESS_BOM_ASYNC_NODE_THRESHOLD=1,
    )
    @patch("project_management.tasks.process_bom_generation_run.apply_async")
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_queued_plan_rejects_source_revision_changes(
        self,
        _permissions,
        _apply_async,
    ):
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        workstation = self.workstations[0]
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [workstation.id],
                "idempotency_key": "async-source-stale",
            },
            format="json",
        )
        force_authenticate(request, self.user)
        queued = create_view(request)
        original_source_hash = queued.data["source_hash"]

        root = workstation.nodes.get(revision__number=1, parent=None)
        commit_workstation_revision(
            workstation=workstation,
            operations=[{"op": "set_quantity", "node": root.id, "quantity": "2"}],
            expected_revision=workstation.active_revision,
            actor=self.user,
            reason="Changed while queued",
            idempotency_key="source-change-while-queued",
        )
        plan = process_queued_bom_plan(queued.data["id"])

        self.assertEqual(plan.status, BomGenerationRun.STATUS_STALE)
        self.assertEqual(plan.operations.count(), 0)
        self.assertEqual(plan.source_hash, original_source_hash)
        self.assertEqual(plan.metrics["last_stale_reason"], "source_hash_changed")

    def test_worker_atomically_claims_a_queued_plan(self):
        from project_management.services.workstation_bom import create_queued_bom_plan

        result = create_queued_bom_plan(
            project=self.project,
            workstation_ids=[self.workstations[0].id],
            actor=self.user,
            idempotency_key="already-running",
        )
        result.plan.status = BomGenerationRun.STATUS_RUNNING
        result.plan.claim_token = uuid.uuid4()
        result.plan.claim_expires_at = timezone.now() + timedelta(minutes=5)
        result.plan.save(
            update_fields=["status", "claim_token", "claim_expires_at", "updated_at"],
        )

        untouched = process_queued_bom_plan(result.plan.id)

        self.assertEqual(untouched.status, BomGenerationRun.STATUS_RUNNING)
        self.assertEqual(untouched.operations.count(), 0)

    @override_settings(PROCESS_BOM_RUNNING_TIMEOUT_SECONDS=60)
    def test_worker_reclaims_an_expired_running_plan(self):
        from project_management.services.workstation_bom import create_queued_bom_plan

        result = create_queued_bom_plan(
            project=self.project,
            workstation_ids=[self.workstations[0].id],
            actor=self.user,
            idempotency_key="expired-worker-claim",
        )
        result.plan.status = BomGenerationRun.STATUS_RUNNING
        result.plan.claim_token = uuid.uuid4()
        result.plan.claim_expires_at = timezone.now() - timedelta(seconds=1)
        result.plan.save(
            update_fields=["status", "claim_token", "claim_expires_at", "updated_at"],
        )

        recovered = process_queued_bom_plan(result.plan.id)

        self.assertEqual(recovered.status, BomGenerationRun.STATUS_PREVIEW)
        self.assertEqual(recovered.operations.count(), 3)
        self.assertIsNone(recovered.claim_token)
        self.assertIsNone(recovered.claim_expires_at)

    def test_bom_task_uses_late_ack_and_worker_loss_redelivery(self):
        from project_management.tasks import process_bom_generation_run

        self.assertTrue(process_bom_generation_run.acks_late)
        self.assertTrue(process_bom_generation_run.reject_on_worker_lost)

    @override_settings(PROCESS_ASSEMBLY_BOM_ENABLED=True)
    @patch.object(BomGenerationRunViewSet, "get_permissions", return_value=[])
    def test_api_does_not_expose_or_accept_cross_tenant_plans(self, _permissions):
        foreign_user = User.objects.create_user(
            email="foreign-bom@example.com",
            password="password",
            user_type="company_user",
        )
        foreign_company = CompanyProfile.objects.create(
            admin_user=foreign_user,
            company_name="Foreign BoM Company",
            company_address="Elsewhere",
            company_town="Town",
            company_tell_no="789",
            company_mob_no="987",
            company_web_address="https://foreign.example.com",
            company_email_address=foreign_user.email,
            company_tax_reference="foreign-tax",
            company_iban="foreign-iban",
        )
        foreign_user.company = foreign_company
        foreign_user.save(update_fields=["company"])
        create_view = BomGenerationRunViewSet.as_view({"post": "create"})
        request = self.factory.post(
            "/api/project_management/bom-generation-runs/",
            {
                "project": self.project.id,
                "workstations": [self.workstations[0].id],
                "idempotency_key": "foreign-preview",
            },
            format="json",
        )
        force_authenticate(request, foreign_user)
        response = create_view(request)
        self.assertEqual(response.status_code, 404)
