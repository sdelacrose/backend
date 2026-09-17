from django.test import TestCase, override_settings
from rest_framework.test import force_authenticate

from project_management.test_workstation_revisions import (
    WorkstationRevisionTests as Fixtures,
)
from project_management.views.workstation import ProjectWorkstationViewSet
from models.models import Permission, Role, RolePermission, UserRole
from project_management.views.matrix import ProjectSolutionSpaceItemViewSet
from project_management.views.matrix import BillOfMaterialPartViewSet
from project_management.models import BillOfMaterialPart


@override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
class EngineeringDeskFeedbackTests(TestCase):
    def setUp(self):
        Fixtures.setUp(self)
        self.role = Role.objects.create(
            name="Engineering editor",
            company=self.company,
            type="solution",
            scope="company",
        )
        permission, _ = Permission.objects.get_or_create(
            key="projects.full_access",
            defaults={"name": "Projects", "module": "projects"},
        )
        RolePermission.objects.create(role=self.role, permission=permission)
        UserRole.objects.create(user=self.user, role=self.role, company=self.company)

    def test_engineer_creates_new_assembly_without_catalog_source(self):
        request = self.factory.post(
            "/workstations/",
            {
                "project": self.project.pk,
                "name": "Custom inspection station",
                "idempotency_key": "new-inspection-station",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        response = ProjectWorkstationViewSet.as_view({"post": "create"})(request)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["source_process_assembly"])
        self.assertEqual(response.data["name"], "Custom inspection station")
        self.assertEqual(len(response.data["nodes"]), 1)
        self.assertEqual(response.data["nodes"][0]["approval_status"], "open")

    def create_blank(self, **changes):
        request = self.factory.post(
            "/workstations/",
            {
                "project": self.project.pk,
                "name": "Independent assembly",
                "idempotency_key": "blank-retry",
                **changes,
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        return ProjectWorkstationViewSet.as_view({"post": "create"})(request)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=False)
    def test_disabled_desk_blocks_direct_reads_and_mutations(self):
        for method, action, payload in (
            ("get", "list", {}),
            ("post", "create", {"name": "Cannot create"}),
            ("get", "retrieve", {}),
            ("get", "compliance", {}),
            ("post", "compliance", {}),
            ("post", "workbench", {}),
            ("post", "preview_revision", {}),
            ("post", "commit_revision", {}),
        ):
            with self.subTest(method=method, action=action):
                request = getattr(self.factory, method)("/workstations/", payload, format="json")
                force_authenticate(request, user=self.user)
                response = ProjectWorkstationViewSet.as_view({method: action})(request, pk=1)
                self.assertEqual(response.status_code, 404, response.data)

    def test_manual_bom_creation_only_remains_in_legacy_mode(self):
        for enabled, expected_status in ((True, 400), (False, 201)):
            with self.subTest(engineering_enabled=enabled), override_settings(
                PROJECT_WORKSTATION_COMPOSITION_ENABLED=enabled,
            ):
                request = self.factory.post("/bom-parts/", {
                    "project": self.project.pk, "name": "Legacy manual part",
                }, format="json")
                force_authenticate(request, user=self.user)
                response = BillOfMaterialPartViewSet.as_view({"post": "create"})(request)
                self.assertEqual(response.status_code, expected_status, response.data)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=False)
    def test_legacy_bom_is_not_blocked_by_hidden_engineering_approval(self):
        part = BillOfMaterialPart.objects.create(project=self.project, name="Existing production part")
        request = self.factory.get("/bom-parts/", {"project": self.project.pk, "ids": str(part.pk)})
        force_authenticate(request, user=self.user)
        response = BillOfMaterialPartViewSet.as_view({"get": "purchase_eligibility"})(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["results"], [{"id": part.pk, "purchase_block_reason": ""}])

    def test_retry_is_idempotent_and_different_payload_is_rejected(self):
        first = self.create_blank()
        retry = self.create_blank()
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(retry.status_code, 200, retry.data)
        self.assertEqual(first.data["id"], retry.data["id"])
        self.assertEqual(self.create_blank(name="Changed name").status_code, 400)

    def test_blank_name_and_catalog_zero_are_not_silent_copies(self):
        for changes in [
            {"name": "   "},
            {"source_process_assembly": 0},
            {"name": "x" * 256},
        ]:
            with self.subTest(changes=changes):
                response = self.create_blank(**changes)
                self.assertEqual(response.status_code, 400, response.data)

    def test_viewer_cannot_create_an_independent_assembly(self):
        RolePermission.objects.filter(role=self.role).delete()
        permission, _ = Permission.objects.get_or_create(
            key="projects.view_all_access",
            defaults={"name": "View projects", "module": "projects"},
        )
        RolePermission.objects.create(role=self.role, permission=permission)
        self.assertEqual(self.create_blank().status_code, 403)

    def test_catalog_items_cannot_bypass_engineering_desk_into_bom(self):
        before = BillOfMaterialPart.objects.count()
        self.product.source_company_item = self.template
        self.product.save(update_fields=["source_company_item"])
        for assembly_type in ["components", "production_lines"]:
            self.product.assembly_type = assembly_type
            self.product.save(update_fields=["assembly_type"])
            request = self.factory.post("/copy-to-bom/", {}, format="json")
            force_authenticate(request, user=self.user)
            response = ProjectSolutionSpaceItemViewSet.as_view({"post": "copy_to_bom"})(
                request, pk=self.product.pk
            )
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BillOfMaterialPart.objects.count(), before)

    def test_move_out_relink_and_remove_preserve_occurrence_history(self):
        root = self.workstation.nodes.get(parent=None)
        child = self.workstation.nodes.get(parent=root)
        request = self.factory.post(
            "/commit-revision/",
            {
                "expected_revision": 1,
                "operations": [
                    {
                        "op": "add",
                        "source_project_item": self.product.pk,
                        "parent_node": child.pk,
                        "include_subtree": False,
                    }
                ],
                "reason": "QA nested part",
                "idempotency_key": "nested-part",
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        response = ProjectWorkstationViewSet.as_view({"post": "commit_revision"})(
            request, pk=self.workstation.pk
        )
        self.assertEqual(response.status_code, 201, response.data)
        occurrence = self.workstation.nodes.get(
            revision__number=2, source_project_item=self.product
        ).occurrence_id
        for revision, operation, parent in [
            (2, "move", root),
            (3, "move", child),
            (4, "exclude", None),
        ]:
            current = self.workstation.nodes.get(
                revision__number=revision, occurrence_id=occurrence
            )
            operation_data = {"op": operation, "node": current.pk}
            if operation == "move":
                operation_data["parent_node"] = self.workstation.nodes.get(
                    revision__number=revision, occurrence_id=parent.occurrence_id
                ).pk
                operation_data["sort_order"] = 1
            request = self.factory.post(
                "/commit-revision/",
                {
                    "expected_revision": revision,
                    "operations": [operation_data],
                    "reason": "QA hierarchy operation",
                    "idempotency_key": f"hierarchy-{revision}",
                },
                format="json",
            )
            force_authenticate(request, user=self.user)
            response = ProjectWorkstationViewSet.as_view({"post": "commit_revision"})(
                request, pk=self.workstation.pk
            )
            self.assertEqual(response.status_code, 201, response.data)
            updated = next(
                node
                for node in response.data["workstation"]["nodes"]
                if node["occurrence_id"] == str(occurrence)
            )
            self.assertEqual(updated["is_active"], operation != "exclude")
            if operation == "move":
                self.assertEqual(updated["parent"] is not None, bool(parent))
        self.assertEqual(
            self.workstation.nodes.filter(occurrence_id=occurrence).count(), 4
        )
