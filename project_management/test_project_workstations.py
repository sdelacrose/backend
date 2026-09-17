from django.core.exceptions import ValidationError
from django.db.models.signals import post_save
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from unittest.mock import patch

from models.models import CompanyProfile, Permission, Role, RolePermission, User
from project_management.models import (
    CompanySolutionSpaceItem,
    Project,
    ProjectSolutionSpaceItem,
    ProjectTeamMembers,
    ProjectWorkstation,
    Requirement,
)
from project_management.services.workstation_composition import (
    create_project_workstation,
)
from project_management.signals import (
    create_default_folders,
    create_related_objects,
    notify_on_project_save,
)
from project_management.views.workstation import ProjectWorkstationViewSet
from project_management.views.matrix import ProjectSolutionSpaceItemViewSet


class ProjectWorkstationCompositionTests(TestCase):
    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch("project_management.views.workstation.require_any_permission_multiple_modules", return_value=True)
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_default_workstation_order_follows_reviewed_station_order(self, *_mocks):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company, assembly_name="Reusable template",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project, source_company_item=template,
            assembly_name=template.assembly_name, assembly_type=template.assembly_type,
        )
        for name, order in [("Finished storage", 7), ("Incoming storage", 1), ("Handling infeed", 2)]:
            requirement = Requirement.objects.create(project=self.project, name=name, sort_order=order)
            selected.requirements.add(requirement)
            create_project_workstation(project=self.project, source=template, actor=self.owner,
                idempotency_key=name, source_requirement=requirement)
        request = self.factory.get("/project-workstations/", {"project": self.project.pk})
        force_authenticate(request, user=self.owner)
        response = ProjectWorkstationViewSet.as_view({"get": "list"})(request)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([row["name"] for row in response.data["results"]],
            ["Incoming storage", "Handling infeed", "Finished storage"])

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch("project_management.views.workstation.require_any_permission_multiple_modules", return_value=True)
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_reviewed_requirement_creates_a_named_station_and_retry_reuses_it(self, *_mocks):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company, assembly_name="Storage template",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project, source_company_item=template,
            assembly_name="Storage template", assembly_type=template.assembly_type,
        )
        requirement = Requirement.objects.create(
            project=self.project, name="WS10 Incoming storage", object_number="WS10",
        )
        selected.requirements.add(requirement)
        create_view = ProjectWorkstationViewSet.as_view({"post": "create"})
        payload = {
            "project": self.project.id, "source_process_assembly": template.id,
            "source_requirement": requirement.id, "idempotency_key": "reviewed-station",
        }
        responses = []
        for attempt in range(2):
            payload["idempotency_key"] = f"dialog-attempt-{attempt}"
            request = self.factory.post("/project-workstations/", payload)
            force_authenticate(request, user=self.owner)
            responses.append(create_view(request))
        self.assertEqual(responses[0].status_code, 201, responses[0].data)
        self.assertEqual(responses[0].data["name"], "WS10 Incoming storage")
        self.assertEqual(responses[0].data["source_requirement"], requirement.id)
        self.assertEqual(responses[1].data["id"], responses[0].data["id"])
        other_station = Requirement.objects.create(
            project=self.project, name="WS70 Finished storage", object_number="WS70",
        )
        selected.requirements.add(other_station)
        payload["source_requirement"] = other_station.id
        request = self.factory.post("/project-workstations/", payload)
        force_authenticate(request, user=self.owner)
        second = create_view(request)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(second.data["name"], "WS70 Finished storage")
        self.assertNotEqual(second.data["id"], responses[0].data["id"])

        # An unrelated requirement cannot be attached just by supplying its ID.
        unlinked = Requirement.objects.create(project=self.project, name="Unlinked")
        payload["source_requirement"] = unlinked.id
        request = self.factory.post("/project-workstations/", payload)
        force_authenticate(request, user=self.owner)
        self.assertEqual(create_view(request).status_code, 400)

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

        self.owner = User.objects.create_user(
            email="workstation-owner@example.com",
            password="password",
            first_name="Workstation",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.owner,
            company_name="Workstation Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.owner.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.owner.company = self.company
        self.owner.save(update_fields=["company"])
        self.project = Project.objects.create(
            project_name="Factory Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.owner,
            created_by=self.owner,
        )
        self.factory = APIRequestFactory()

    def test_create_from_process_template_copies_the_complete_hierarchy(self):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Workstation 1",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            sort_order=4,
            custom_fields={"quantity": 1, "unit": "station"},
        )
        child = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=template,
            assembly_name="Assembly 1.1",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            level=2,
            sort_order=2,
            custom_fields={"quantity": 3, "unit": "piece"},
        )

        result = create_project_workstation(
            project=self.project,
            source=template,
            actor=self.owner,
            idempotency_key="factory-project-workstation-1",
        )

        self.assertTrue(result.created)
        workstation = ProjectWorkstation.objects.get(id=result.workstation.id)
        self.assertEqual(workstation.name, "Workstation 1")
        self.assertEqual(workstation.source_process_assembly, template)
        self.assertEqual(workstation.active_revision, 1)

        nodes = list(
            workstation.nodes.select_related("parent", "source_company_item").order_by(
                "level",
                "sort_order",
            )
        )
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].source_company_item, template)
        self.assertIsNone(nodes[0].parent)
        self.assertEqual(nodes[0].quantity, 1)
        self.assertEqual(nodes[0].unit, "station")
        self.assertEqual(nodes[1].source_company_item, child)
        self.assertEqual(nodes[1].parent, nodes[0])
        self.assertEqual(nodes[1].level, 2)
        self.assertEqual(nodes[1].sort_order, 2)
        self.assertEqual(nodes[1].quantity, 3)
        self.assertEqual(nodes[1].unit, "piece")

        revision = workstation.revisions.get(number=1)
        self.assertEqual(revision.snapshot.content_hash, workstation.template_hash)
        self.assertEqual(len(revision.snapshot.content["nodes"]), 2)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_process_catalog_pagination_does_not_duplicate_rows_from_membership_joins(
        self,
        _permissions,
        _permission_lookup,
    ):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved process assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="One process assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            source_company_item=source,
        )
        for index in range(2):
            team_member = User.objects.create_user(
                email=f"membership-{index}@example.com",
                password="password",
                user_type="company_user",
                company=self.company,
            )
            ProjectTeamMembers.objects.create(
                project=self.project,
                role="Member",
                project_access="RO",
                team_member=team_member,
            )

        request = self.factory.get(
            "/project-solution-space-items/",
            {
                "project": self.project.id,
                "assembly_type": "production_lines",
                "page": 1,
                "page_size": 25,
            },
        )
        force_authenticate(request, user=self.owner)
        response = ProjectSolutionSpaceItemViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_create_api_is_idempotent_and_returns_the_active_tree(
        self,
        _permissions,
        _permission_lookup,
    ):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Reusable workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=template,
            assembly_name="Nested assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            level=2,
        )
        request_data = {
            "project": self.project.id,
            "source_process_assembly": template.id,
            "idempotency_key": "api-reusable-workstation",
            "name": "WS10 Incoming storage",
        }
        create_view = ProjectWorkstationViewSet.as_view({"post": "create"})

        first_request = self.factory.post("/project-workstations/", request_data)
        force_authenticate(first_request, user=self.owner)
        first_response = create_view(first_request)
        retry_request = self.factory.post("/project-workstations/", request_data)
        force_authenticate(retry_request, user=self.owner)
        retry_response = create_view(retry_request)

        self.assertEqual(first_response.status_code, 201, first_response.data)
        self.assertEqual(first_response.data["name"], "WS10 Incoming storage")
        self.assertEqual(first_response.data["nodes"][0]["name"], "WS10 Incoming storage")
        self.assertEqual(retry_response.status_code, 200, retry_response.data)
        self.assertEqual(first_response.data["id"], retry_response.data["id"])
        self.assertEqual(len(first_response.data["nodes"]), 2)
        self.assertEqual(ProjectWorkstation.objects.count(), 1)

        list_request = self.factory.get(
            "/project-workstations/",
            {"project": self.project.id},
        )
        force_authenticate(list_request, user=self.owner)
        list_response = ProjectWorkstationViewSet.as_view({"get": "list"})(
            list_request,
        )
        self.assertEqual(list_response.status_code, 200, list_response.data)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(len(list_response.data["results"][0]["nodes"]), 2)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    def test_project_permissions_allow_an_editor_and_keep_a_viewer_read_only(self):
        editor = User.objects.create_user(
            email="workstation-editor@example.com",
            password="password",
            user_type="company_user",
            company=self.company,
        )
        viewer = User.objects.create_user(
            email="workstation-viewer@example.com",
            password="password",
            user_type="company_user",
            company=self.company,
        )
        editor_role = Role.objects.create(
            name="Workstation Editor",
            company=self.company,
            type="solution",
            scope="project",
        )
        viewer_role = Role.objects.create(
            name="Workstation Viewer",
            company=self.company,
            type="solution",
            scope="project",
        )
        for role, permission_keys in (
            (
                editor_role,
                ("project_assembly_catalog.create", "project_assembly_catalog.view"),
            ),
            (viewer_role, ("project_assembly_catalog.view",)),
        ):
            for permission_key in permission_keys:
                permission, _created = Permission.objects.get_or_create(
                    key=permission_key,
                    defaults={
                        "name": permission_key,
                        "module": "project_assembly_catalog",
                    },
                )
                RolePermission.objects.create(role=role, permission=permission)
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="Editor",
            project_role=editor_role,
            project_access="RW",
            team_member=editor,
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="Viewer",
            project_role=viewer_role,
            project_access="RO",
            team_member=viewer,
        )
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Permission workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        payload = {
            "project": self.project.id,
            "source_process_assembly": template.id,
            "idempotency_key": "permission-workstation",
        }
        create_view = ProjectWorkstationViewSet.as_view({"post": "create"})

        editor_request = self.factory.post("/project-workstations/", payload)
        force_authenticate(editor_request, user=editor)
        editor_response = create_view(editor_request)
        viewer_request = self.factory.post("/project-workstations/", payload)
        force_authenticate(viewer_request, user=viewer)
        viewer_response = create_view(viewer_request)

        self.assertEqual(editor_response.status_code, 201, editor_response.data)
        self.assertEqual(viewer_response.status_code, 403, viewer_response.data)

        list_request = self.factory.get(
            "/project-workstations/",
            {"project": self.project.id},
        )
        force_authenticate(list_request, user=viewer)
        list_response = ProjectWorkstationViewSet.as_view({"get": "list"})(
            list_request,
        )
        self.assertEqual(list_response.status_code, 200, list_response.data)
        self.assertEqual(list_response.data["count"], 1)

    def test_create_rejects_a_template_deeper_than_the_supported_tree(self):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Deep workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        parent = template
        for level in range(2, CompanySolutionSpaceItem.MAX_LEVEL + 2):
            parent = CompanySolutionSpaceItem.objects.create(
                company_profile=self.company,
                parent=parent,
                assembly_name=f"Assembly level {level}",
                assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
                level=level,
            )

        with self.assertRaises(ValidationError):
            create_project_workstation(
                project=self.project,
                source=template,
                actor=self.owner,
                idempotency_key="too-deep-workstation",
            )

        self.assertFalse(ProjectWorkstation.objects.exists())

    def test_template_snapshots_are_independent_and_retry_keeps_the_original(self):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Original workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        first = create_project_workstation(
            project=self.project,
            source=template,
            actor=self.owner,
            idempotency_key="first-workstation",
        )
        original_hash = first.workstation.template_hash

        template.assembly_name = "Updated catalog workstation"
        template.save(update_fields=["assembly_name"])
        retry = create_project_workstation(
            project=self.project,
            source=template,
            actor=self.owner,
            idempotency_key="first-workstation",
        )
        second = create_project_workstation(
            project=self.project,
            source=template,
            actor=self.owner,
            idempotency_key="second-workstation",
        )

        self.assertFalse(retry.created)
        self.assertEqual(retry.workstation.id, first.workstation.id)
        self.assertEqual(retry.workstation.name, "Original workstation")
        self.assertEqual(retry.workstation.template_hash, original_hash)
        self.assertTrue(second.created)
        self.assertEqual(second.workstation.name, "Updated catalog workstation")
        self.assertNotEqual(second.workstation.template_hash, original_hash)
        self.assertEqual(ProjectWorkstation.objects.count(), 2)

    def test_invalid_source_tree_rolls_back_without_partial_workstation_data(self):
        template = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Invalid workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=template,
            assembly_name="Invalid quantity",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            level=2,
            custom_fields={"quantity": 0},
        )

        with self.assertRaises(ValidationError):
            create_project_workstation(
                project=self.project,
                source=template,
                actor=self.owner,
                idempotency_key="invalid-workstation",
            )

        self.assertFalse(ProjectWorkstation.objects.exists())

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_api_does_not_expose_or_accept_another_tenants_workstations(
        self,
        _permissions,
        _permission_lookup,
    ):
        other_owner = User.objects.create_user(
            email="other-workstation-owner@example.com",
            password="password",
            user_type="company_user",
        )
        other_company = CompanyProfile.objects.create(
            admin_user=other_owner,
            company_name="Other Workstation Company",
            company_address="Street 2",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://other.example.com",
            company_email_address=other_owner.email,
            company_tax_reference="other-tax",
            company_iban="other-iban",
        )
        other_owner.company = other_company
        other_owner.save(update_fields=["company"])
        other_project = Project.objects.create(
            project_name="Other Factory",
            contract_value=1000,
            company_profile=other_company,
            user=other_owner,
            created_by=other_owner,
        )
        other_template = CompanySolutionSpaceItem.objects.create(
            company_profile=other_company,
            assembly_name="Other tenant workstation",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        create_project_workstation(
            project=other_project,
            source=other_template,
            actor=other_owner,
            idempotency_key="other-tenant-workstation",
        )

        list_request = self.factory.get(
            "/project-workstations/",
            {"project": other_project.id},
        )
        force_authenticate(list_request, user=self.owner)
        list_response = ProjectWorkstationViewSet.as_view({"get": "list"})(
            list_request,
        )
        self.assertEqual(list_response.status_code, 200, list_response.data)
        self.assertEqual(list_response.data["count"], 0)

        create_request = self.factory.post(
            "/project-workstations/",
            {
                "project": self.project.id,
                "source_process_assembly": other_template.id,
                "idempotency_key": "foreign-template",
            },
        )
        force_authenticate(create_request, user=self.owner)
        create_response = ProjectWorkstationViewSet.as_view({"post": "create"})(
            create_request,
        )
        self.assertEqual(create_response.status_code, 404, create_response.data)
        self.assertEqual(ProjectWorkstation.objects.count(), 1)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=True)
    @patch(
        "project_management.views.workstation.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_list_supports_search_ordering_pagination_and_revision_history(
        self,
        _permissions,
        _permission_lookup,
    ):
        for name in ("Beta workstation", "Alpha workstation", "Gamma workstation"):
            template = CompanySolutionSpaceItem.objects.create(
                company_profile=self.company,
                assembly_name=name,
                assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            )
            create_project_workstation(
                project=self.project,
                source=template,
                actor=self.owner,
                idempotency_key=name.lower().replace(" ", "-"),
            )

        list_request = self.factory.get(
            "/project-workstations/",
            {
                "project": self.project.id,
                "search": "workstation",
                "ordering": "name",
                "page_size": 2,
            },
        )
        force_authenticate(list_request, user=self.owner)
        list_response = ProjectWorkstationViewSet.as_view({"get": "list"})(
            list_request,
        )

        self.assertEqual(list_response.status_code, 200, list_response.data)
        self.assertEqual(list_response.data["count"], 3)
        self.assertEqual(
            [item["name"] for item in list_response.data["results"]],
            ["Alpha workstation", "Beta workstation"],
        )
        self.assertIsNotNone(list_response.data["next"])

        workstation_id = list_response.data["results"][0]["id"]
        revisions_request = self.factory.get(
            f"/project-workstations/{workstation_id}/revisions/",
            {"project": self.project.id},
        )
        force_authenticate(revisions_request, user=self.owner)
        revisions_response = ProjectWorkstationViewSet.as_view(
            {"get": "revisions"},
        )(revisions_request, pk=workstation_id)
        self.assertEqual(revisions_response.status_code, 200, revisions_response.data)
        self.assertEqual(revisions_response.data["count"], 1)
        self.assertEqual(revisions_response.data["results"][0]["number"], 1)
        self.assertEqual(revisions_response.data["results"][0]["node_count"], 1)

    @override_settings(PROJECT_WORKSTATION_COMPOSITION_ENABLED=False)
    @patch.object(ProjectWorkstationViewSet, "get_permissions", return_value=[])
    def test_disabled_feature_is_not_exposed(self, _permissions):
        request = self.factory.get(
            "/project-workstations/",
            {"project": self.project.id},
        )
        force_authenticate(request, user=self.owner)
        response = ProjectWorkstationViewSet.as_view({"get": "list"})(request)
        self.assertEqual(response.status_code, 404)
