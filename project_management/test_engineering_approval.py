import uuid

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from project_management.models import BillOfMaterialPart
from project_management.services.engineering_approval import purchase_block_reason, validate_purchase_items
from project_management.services.workstation_bom import apply_bom_plan, BomPlanConflict

from project_management import test_workstation_bom as bom_fixtures
from project_management.services.workstation_bom import create_bom_plan
from project_management.services.workstation_revision import commit_workstation_revision


@override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
class EngineeringApprovalTests(TestCase):
    agree_initial_fixture = False
    setUp = bom_fixtures.WorkstationBomPlanTests.setUp

    def approve(self, station):
        station.refresh_from_db()
        root = station.nodes.get(parent=None, revision__number=station.active_revision)
        return commit_workstation_revision(
            workstation=station, actor=self.user, expected_revision=station.active_revision,
            idempotency_key=str(uuid.uuid4()), reason="Reviewed",
            operations=[{"op": "set_approval", "node": root.id,
                         "approval_status": "agreed", "include_subtree": True}],
        )

    def plan(self, station):
        return create_bom_plan(project=self.project, workstation_ids=[station.id],
                               actor=self.user, idempotency_key=str(uuid.uuid4()))

    def test_existing_rows_are_preserved_but_not_purchase_eligible(self):
        part = BillOfMaterialPart.objects.create(project=self.project, name="Legacy part")
        self.assertIn("Review required", purchase_block_reason(part))
        with self.assertRaises(ValidationError):
            validate_purchase_items(project=self.project, items=[{"bom_part_id": part.id}])
        self.assertTrue(BillOfMaterialPart.objects.filter(pk=part.id).exists())

    def test_agreeing_one_part_does_not_approve_its_ancestors(self):
        station = self.workstations[0]
        leaf = station.nodes.get(name="Mounting plate")
        commit_workstation_revision(workstation=station, actor=self.user,
            expected_revision=station.active_revision, reason="Leaf reviewed", idempotency_key="leaf-only",
            operations=[{"op": "set_approval", "node": leaf.id, "approval_status": "agreed"}])
        with self.assertRaisesMessage(ValidationError, "Frame assembly"):
            self.plan(station)

    def test_invalid_approval_does_not_write_a_revision(self):
        station = self.workstations[0]
        root = station.nodes.get(parent=None)
        for operation in [
            {"op": "set_approval", "node": root.id, "approval_status": "accepted"},
            {"op": "set_approval", "node": root.id, "approval_status": "agreed", "include_subtree": "false"},
            {"op": "set_approval", "node": self.workstations[1].nodes.get(parent=None).id, "approval_status": "agreed"},
        ]:
            with self.subTest(operation=operation), self.assertRaises(ValidationError):
                commit_workstation_revision(workstation=station, actor=self.user,
                    expected_revision=1, reason="Invalid review", idempotency_key=str(uuid.uuid4()), operations=[operation])
        station.refresh_from_db()
        self.assertEqual(station.active_revision, 1)

    def test_quantity_change_resets_approval_and_blocks_existing_cart(self):
        station = self.workstations[0]
        self.approve(station)
        plan = self.plan(station)
        apply_bom_plan(plan=plan, actor=self.user, expected_target_hash=plan.target_hash)
        part = BillOfMaterialPart.objects.get(project=self.project, is_purchasable=True)
        self.assertEqual(purchase_block_reason(part), "")
        station.refresh_from_db()
        leaf = station.nodes.get(revision__number=station.active_revision, name="Mounting plate")
        commit_workstation_revision(
            workstation=station, actor=self.user, expected_revision=station.active_revision,
            idempotency_key=str(uuid.uuid4()), reason="Change quantity",
            operations=[{"op": "set_quantity", "node": leaf.id, "quantity": "4", "unit": "piece"}],
        )
        part.refresh_from_db()
        self.assertIn("Agreed", purchase_block_reason(part))
        with self.assertRaises(ValidationError):
            validate_purchase_items(project=self.project, items=[{"bom_part_id": part.id}])
        self.assertEqual(BillOfMaterialPart.objects.filter(project=self.project).count(), 3)

    def test_revoking_approval_invalidates_preview(self):
        station = self.workstations[0]
        self.approve(station)
        plan = self.plan(station)
        station.refresh_from_db()
        root = station.nodes.get(parent=None, revision__number=station.active_revision)
        commit_workstation_revision(
            workstation=station, actor=self.user, expected_revision=station.active_revision,
            idempotency_key=str(uuid.uuid4()), reason="Needs discussion",
            operations=[{"op": "set_approval", "node": root.id, "approval_status": "under_discussion"}],
        )
        with self.assertRaises(BomPlanConflict):
            apply_bom_plan(plan=plan, actor=self.user, expected_target_hash=plan.target_hash)

    def test_copy_updates_reviewed_snapshot_even_when_composition_is_unchanged(self):
        station = self.workstations[0]
        self.approve(station)
        initial = self.plan(station)
        apply_bom_plan(plan=initial, actor=self.user, expected_target_hash=initial.target_hash)
        part = BillOfMaterialPart.objects.get(project=self.project, is_purchasable=True)
        part.custom_fields = {"price": "120.00", "supplier": "QA supplier"}
        part.save(update_fields=["custom_fields"])
        self.approve(station)
        part.refresh_from_db()
        self.assertIn("copy", purchase_block_reason(part))
        update = self.plan(station)
        self.assertEqual(update.summary["update"], 3)
        apply_bom_plan(plan=update, actor=self.user, expected_target_hash=update.target_hash)
        part.refresh_from_db()
        self.assertEqual(purchase_block_reason(part), "")
        self.assertEqual(part.custom_fields, {"price": "120.00", "supplier": "QA supplier"})

    def test_approval_cannot_be_combined_with_engineering_changes(self):
        station = self.workstations[0]
        root = station.nodes.get(parent=None)
        with self.assertRaisesMessage(ValidationError, "Save engineering changes"):
            commit_workstation_revision(
                workstation=station, actor=self.user, expected_revision=station.active_revision,
                idempotency_key=str(uuid.uuid4()), reason="Unreviewed change",
                operations=[{"op": "set_quantity", "node": root.id, "quantity": "2"},
                            {"op": "set_approval", "node": root.id, "approval_status": "agreed"}],
            )

    def test_open_parts_require_explicit_agreement_before_bom(self):
        station = self.workstations[0]
        with self.assertRaisesMessage(ValidationError, "Agreed"):
            create_bom_plan(project=self.project, workstation_ids=[station.id],
                            actor=self.user, idempotency_key=str(uuid.uuid4()))
        root = station.nodes.get(parent=None, revision__number=station.active_revision)
        commit_workstation_revision(
            workstation=station, actor=self.user, expected_revision=station.active_revision,
            idempotency_key=str(uuid.uuid4()),
            reason="Reviewed for purchase",
            operations=[{"op": "set_approval", "node": root.id,
                         "approval_status": "agreed", "include_subtree": True}],
        )
        plan = create_bom_plan(project=self.project, workstation_ids=[station.id],
                               actor=self.user, idempotency_key=str(uuid.uuid4()))
        self.assertEqual(plan.summary["add"], 3)
