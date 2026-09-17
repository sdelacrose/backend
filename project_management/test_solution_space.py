from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models.signals import post_save, pre_delete
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from aas_integration.domain import ProductAASMapper
from aas_integration.models import AASResourceIdentity, ProductAASProfile
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanyAssemblyProductionLink,
    CompanySolutionSpaceItem,
    ContactCompany,
    Document,
    ECPMPConfig,
    Folder,
    MatrixComment,
    ObjectAssessment,
    Project,
    ProjectSolutionSpaceItem,
    ProjectTeamMembers,
    Requirement,
    SolutionSpaceConfig,
)
from project_management.permissions import BillOfMaterialPermission
from project_management.serializers.matrix import (
    BillOfMaterialPartSerializer,
    CompanySolutionSpaceItemSerializer,
    MatrixCommentSerializer,
    ProjectSolutionSpaceItemSerializer,
    SolutionSpaceConfigSerializer,
)
from project_management.serializers.object_assessment import ObjectAssessmentSerializer
from project_management.views.matrix import (
    AvailableProductPartViewSet,
    BillOfMaterialPartViewSet,
    CompanySolutionSpaceItemViewSet,
    ProjectSolutionSpaceItemViewSet,
    SolutionSpaceConfigViewSet,
)
from project_management.views.project import ProjectViewSet
from project_management.signals import (
    create_related_objects,
    create_default_folders,
    notify_on_object_assessment_delete,
    notify_on_object_assessment_save,
    notify_on_contact_company_delete,
    notify_on_contact_company_save,
    notify_on_project_save,
)


class SolutionSpaceMatrixTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_object_assessment_save, sender=ObjectAssessment)
        post_save.disconnect(create_related_objects, sender=ProjectTeamMembers)
        pre_delete.disconnect(notify_on_object_assessment_delete, sender=ObjectAssessment)
        post_save.disconnect(notify_on_contact_company_save, sender=ContactCompany)
        pre_delete.disconnect(notify_on_contact_company_delete, sender=ContactCompany)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            notify_on_object_assessment_save,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            post_save.connect,
            create_related_objects,
            sender=ProjectTeamMembers,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_object_assessment_delete,
            sender=ObjectAssessment,
        )
        self.addCleanup(
            post_save.connect,
            notify_on_contact_company_save,
            sender=ContactCompany,
        )
        self.addCleanup(
            pre_delete.connect,
            notify_on_contact_company_delete,
            sender=ContactCompany,
        )

        self.factory = APIRequestFactory()
        self.owner = User.objects.create_user(
            email="solution-space-owner@example.com",
            password="password",
            first_name="Solution",
            last_name="Owner",
            user_type="company_user",
        )
        self.engineer = User.objects.create_user(
            email="solution-space-engineer@example.com",
            password="password",
            first_name="Responsible",
            last_name="Engineer",
            user_type="company_user",
        )
        self.company = self._create_company(self.owner, "Solution Space Company")
        User.objects.filter(id__in=[self.owner.id, self.engineer.id]).update(
            company=self.company,
        )
        self.owner.refresh_from_db()
        self.engineer.refresh_from_db()

        self.other_owner = User.objects.create_user(
            email="other-solution-space-owner@example.com",
            password="password",
            first_name="Other",
            last_name="Owner",
            user_type="company_user",
        )
        self.other_company = self._create_company(
            self.other_owner,
            "Other Solution Space Company",
        )
        self.other_owner.company = self.other_company
        self.other_owner.save(update_fields=["company"])

        self.project = self._create_project(self.company, self.owner, "Main Project")
        self.other_project = self._create_project(
            self.other_company,
            self.other_owner,
            "Other Project",
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=self.engineer,
        )
        self.customer = ContactCompany.objects.create(
            user=self.owner,
            company_profile=self.company,
            company_name="Network Customer",
            contact_email="network-customer@example.com",
        )
        self.other_customer = ContactCompany.objects.create(
            user=self.other_owner,
            company_profile=self.other_company,
            company_name="Other Customer",
            contact_email="other-customer@example.com",
        )
        self.company_scope_config = CompanyConfig.objects.create(
            company_profile=self.company,
            solution=self.project.solution,
            skills=["drill", "assemble", "test"],
        )
        self.project_scope_config = ECPMPConfig.objects.create(
            project=self.project,
            skills=["drill", "test", "project-specific-skill"],
        )
        self.company_config = SolutionSpaceConfig.objects.create(
            company_profile=self.company,
        )

    @staticmethod
    def _create_company(admin_user, name):
        return CompanyProfile.objects.create(
            admin_user=admin_user,
            company_name=name,
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=admin_user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )

    @staticmethod
    def _create_project(company, owner, name):
        return Project.objects.create(
            project_name=name,
            contract_value=1000,
            company_profile=company,
            user=owner,
            created_by=owner,
        )

    def _request(self, method="post", path="/"):
        request = getattr(self.factory, method)(path)
        request.user = self.owner
        return request

    def test_config_keeps_assembly_visible_and_accepts_translated_custom_column(self):
        serializer = SolutionSpaceConfigSerializer(
            self.company_config,
            data={
                "columns": [
                    {
                        "key": "assembly_name",
                        "label": "Assembly",
                        "label_en": "Assembly",
                        "label_de": "Baugruppe",
                        "label_fr": "Assemblage",
                        "type": "text",
                        "order": 3,
                        "visible": False,
                        "system": True,
                    },
                    {
                        "key": "surface_finish",
                        "label": "Surface finish",
                        "label_en": "Surface finish",
                        "label_de": "Oberfläche",
                        "label_fr": "Finition de surface",
                        "type": "text",
                        "order": 0,
                        "visible": True,
                        "idta_semantic_id": "0173-1#02-BAA120#008",
                    },
                ]
            },
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        updated = serializer.save()
        columns = {column["key"]: column for column in updated.columns}
        self.assertTrue(columns["assembly_name"]["visible"])
        self.assertEqual(
            columns["surface_finish"]["label_fr"],
            "Finition de surface",
        )
        self.assertEqual(
            columns["surface_finish"]["idta_semantic_id"],
            "0173-1#02-BAA120#008",
        )

    def test_default_config_uses_ensemble_in_french_system_column_labels(self):
        columns = {
            column["key"]: column for column in self.company_config.columns
        }

        self.assertEqual(
            columns["assembly_name"]["label_fr"],
            "Nom de l'ensemble",
        )
        self.assertEqual(
            columns["assembly_type"]["label_fr"],
            "Type d'ensemble",
        )
        self.assertEqual(
            columns["projects"]["label_fr"],
            "Ensemble utilisé dans le projet",
        )

    def test_solution_space_uses_centralized_scope_skill_options(self):
        project_config = SolutionSpaceConfig.objects.create(
            company_profile=self.company,
            project=self.project,
        )

        company_data = SolutionSpaceConfigSerializer(self.company_config).data
        project_data = SolutionSpaceConfigSerializer(project_config).data
        self.assertNotIn("skill_options", company_data)
        self.assertNotIn("skill_options", project_data)

        company_item = CompanySolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "assembly_name": "Company assembly",
                "required_skills": ["project-specific-skill"],
            },
            context={"request": self._request()},
        )
        self.assertFalse(company_item.is_valid())
        self.assertIn("required_skills", company_item.errors)

        valid_company_item = CompanySolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "assembly_name": "Configured company assembly",
                "required_skills": ["assemble"],
            },
            context={"request": self._request()},
        )
        self.assertTrue(valid_company_item.is_valid(), valid_company_item.errors)

        direct_project_item = ProjectSolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "assembly_name": "Project assembly",
                "required_skills": ["project-specific-skill"],
            },
            context={"request": self._request()},
        )
        self.assertFalse(direct_project_item.is_valid())
        self.assertIn("source_company_item", direct_project_item.errors)

        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved project assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            required_skills=["assemble"],
        )
        project_item = ProjectSolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "source_company_item": source.id,
            },
            context={"request": self._request()},
        )
        self.assertTrue(project_item.is_valid(), project_item.errors)
        selected = project_item.save()
        self.assertEqual(selected.assembly_name, source.assembly_name)
        self.assertEqual(selected.required_skills, ["assemble"])

    def test_company_catalog_links_products_and_processes_bidirectionally(self):
        product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Drive assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        process = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Drive assembly line",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        serializer = CompanySolutionSpaceItemSerializer(
            product,
            data={"related_assembly_ids": [process.id]},
            partial=True,
            context={"request": self._request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertTrue(
            CompanyAssemblyProductionLink.objects.filter(
                product_assembly=product,
                process_assembly=process,
            ).exists()
        )
        self.assertEqual(
            CompanySolutionSpaceItemSerializer(product).data[
                "related_process_assembly_ids"
            ],
            [process.id],
        )
        self.assertEqual(
            CompanySolutionSpaceItemSerializer(process).data[
                "related_product_assembly_ids"
            ],
            [product.id],
        )

    def test_company_catalog_rejects_cross_company_and_same_catalog_links(self):
        product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Drive assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        same_catalog = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Second product",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        other_process = CompanySolutionSpaceItem.objects.create(
            company_profile=self.other_company,
            assembly_name="Other process",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        for related_id in (same_catalog.id, other_process.id):
            serializer = CompanySolutionSpaceItemSerializer(
                product,
                data={"related_assembly_ids": [related_id]},
                partial=True,
                context={"request": self._request()},
            )
            self.assertFalse(serializer.is_valid())
            self.assertIn("related_assembly_ids", serializer.errors)

    @patch.object(SolutionSpaceConfigViewSet, "get_permissions", return_value=[])
    def test_project_config_can_be_updated_by_id_without_project_query_param(
        self,
        _permissions,
    ):
        project_config = SolutionSpaceConfig.objects.create(
            company_profile=self.company,
            project=self.project,
        )
        columns = project_config.columns
        columns[0] = {**columns[0], "label": "Updated assembly"}
        request = self.factory.patch(
            f"/api/project_management/solution-space-config/{project_config.id}/",
            {"columns": columns},
            format="json",
        )
        force_authenticate(request, user=self.owner)

        response = SolutionSpaceConfigViewSet.as_view(
            {"patch": "partial_update"},
        )(request, pk=project_config.id)

        self.assertEqual(response.status_code, 200, response.data)
        project_config.refresh_from_db()
        self.assertEqual(project_config.columns[0]["label"], "Updated assembly")

    def test_company_item_validates_typed_fields_and_scope(self):
        serializer = CompanySolutionSpaceItemSerializer(
            data={
                "assembly_name": "Drive assembly",
                "required_skills": ["drill"],
                "provided_skills": ["test"],
                "responsible_engineer": self.engineer.id,
                "projects": [self.project.id],
                "customers": [self.customer.id],
                "year_of_construction": 2026,
            },
            context={"request": self._request()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        item = serializer.save(company_profile=self.company)
        self.assertEqual(item.responsible_engineer, self.engineer)
        self.assertEqual(list(item.projects.all()), [self.project])
        self.assertEqual(list(item.customers.all()), [self.customer])

        invalid = CompanySolutionSpaceItemSerializer(
            data={
                "assembly_name": "Invalid scope",
                "projects": [self.other_project.id],
                "customers": [self.other_customer.id],
            },
            context={"request": self._request()},
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("projects", invalid.errors)
        self.assertIn("customers", invalid.errors)

    @patch(
        "project_management.views.project.require_any_permission_multiple_modules",
        return_value=True,
    )
    def test_company_only_project_options_exclude_cross_company_projects(
        self,
        _permission_mock,
    ):
        ProjectTeamMembers.objects.create(
            project=self.other_project,
            role="external member",
            team_member=self.owner,
        )
        request = self.factory.get(
            "/api/project_management/projects/",
            {"company_only": "true", "project_status": "all"},
        )
        force_authenticate(request, user=self.owner)
        view = ProjectViewSet()
        view.action_map = {"get": "list"}
        view.action = "list"
        view.args = ()
        view.kwargs = {}
        view.format_kwarg = None
        view.request = view.initialize_request(request)

        project_ids = set(view.get_queryset().values_list("id", flat=True))

        self.assertIn(self.project.id, project_ids)
        self.assertNotIn(self.other_project.id, project_ids)

    def test_company_hierarchy_rejects_level_eleven(self):
        parent = None
        for level in range(1, 11):
            serializer = CompanySolutionSpaceItemSerializer(
                data={
                    "assembly_name": f"Assembly {level}",
                    "parent": parent.id if parent else None,
                },
                context={"request": self._request()},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            parent = serializer.save(company_profile=self.company)

        serializer = CompanySolutionSpaceItemSerializer(
            data={
                "assembly_name": "Level 11",
                "parent": parent.id,
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("parent", serializer.errors)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_indent_rejects_subtree_that_would_exceed_level_ten(
        self,
        _permissions,
        _permission_lookup,
    ):
        parent = None
        for level in range(1, 9):
            parent = CompanySolutionSpaceItem.objects.create(
                company_profile=self.company,
                parent=parent,
                level=level,
                sort_order=1,
                assembly_name=f"Parent level {level}",
            )
        previous_sibling = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=parent,
            level=9,
            sort_order=1,
            assembly_name="Previous level nine",
        )
        subtree_root = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=parent,
            level=9,
            sort_order=2,
            assembly_name="Subtree root",
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=subtree_root,
            level=10,
            sort_order=1,
            assembly_name="Level ten child",
        )

        request = self.factory.post(
            f"/api/project_management/company-solution-space-items/{subtree_root.id}/indent/",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"post": "indent"})(
            request,
            pk=subtree_root.id,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Cannot create hierarchy deeper than 10 levels.",
        )
        subtree_root.refresh_from_db()
        self.assertEqual(subtree_root.parent_id, parent.id)
        self.assertNotEqual(subtree_root.parent_id, previous_sibling.id)

    def test_project_links_reject_records_from_other_project(self):
        other_requirement = Requirement.objects.create(
            project=self.other_project,
            name="Other requirement",
            object_number="R-OTHER",
        )
        other_assessment = ObjectAssessment.objects.create(
            project=self.other_project,
            record_type="Photo",
            record_title="Other assessment",
            file=SimpleUploadedFile("other.txt", b"content"),
        )
        serializer = ProjectSolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "assembly_name": "Project assembly",
                "requirements": [other_requirement.id],
                "object_assessments": [other_assessment.id],
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("requirements", serializer.errors)
        self.assertIn("object_assessments", serializer.errors)

    def test_object_assessment_links_project_solution_space_in_both_directions(self):
        assessment = ObjectAssessment.objects.create(
            project=self.project,
            record_type="Notes",
            record_title="Assembly observation",
            file=SimpleUploadedFile("observation.txt", b"content"),
        )
        item = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Observed assembly",
        )
        serializer = ObjectAssessmentSerializer(
            assessment,
            data={
                "project_solution_space_item_ids": f"[{item.id}]",
            },
            partial=True,
            context={"request": self._request()},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        self.assertEqual(list(assessment.project_solution_space_items.all()), [item])
        self.assertEqual(
            ObjectAssessmentSerializer(assessment).data["project_solution_space_items"],
            [item.id],
        )

        other_item = ProjectSolutionSpaceItem.objects.create(
            project=self.other_project,
            assembly_name="Other project assembly",
        )
        invalid = ObjectAssessmentSerializer(
            assessment,
            data={
                "project_solution_space_item_ids": f"[{other_item.id}]",
            },
            partial=True,
            context={"request": self._request()},
        )
        self.assertFalse(invalid.is_valid())
        self.assertIn("project_solution_space_item_ids", invalid.errors)

    def test_project_responsible_engineer_must_be_a_team_member(self):
        serializer = ProjectSolutionSpaceItemSerializer(
            data={
                "project": self.project.id,
                "assembly_name": "Project assembly",
                "responsible_engineer": self.other_owner.id,
            },
            context={"request": self._request()},
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("responsible_engineer", serializer.errors)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_list_scope_isolated_for_companies_projects_and_freelancers(
        self,
        _project_permissions,
        _company_permissions,
        _permission_lookup,
    ):
        company_item = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Company assembly",
        )
        other_company_item = CompanySolutionSpaceItem.objects.create(
            company_profile=self.other_company,
            assembly_name="Other company assembly",
        )
        company_request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {"page_size": 1000},
        )
        force_authenticate(company_request, user=self.owner)
        company_response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
            company_request,
        )
        company_ids = {item["id"] for item in company_response.data["results"]}
        self.assertIn(company_item.id, company_ids)
        self.assertNotIn(other_company_item.id, company_ids)

        project_source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved project assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        project_item = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Project assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            source_company_item=project_source,
        )
        other_project_source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.other_company,
            assembly_name="Other approved assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        ProjectSolutionSpaceItem.objects.create(
            project=self.other_project,
            assembly_name="Other project assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            source_company_item=other_project_source,
        )
        inaccessible_request = self.factory.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.id, "page_size": 1000},
        )
        force_authenticate(inaccessible_request, user=self.other_owner)
        inaccessible_response = ProjectSolutionSpaceItemViewSet.as_view(
            {"get": "list"},
        )(inaccessible_request)
        self.assertEqual(inaccessible_response.data["count"], 0)

        freelancer = User.objects.create_user(
            email="solution-space-freelancer@example.com",
            password="password",
            first_name="Project",
            last_name="Freelancer",
            user_type="freelancer",
        )
        ProjectTeamMembers.objects.create(
            project=self.project,
            role="member",
            team_member=freelancer,
        )
        freelancer_request = self.factory.get(
            "/api/project_management/project-solution-space-items/",
            {"project": self.project.id, "page_size": 1000},
        )
        force_authenticate(freelancer_request, user=freelancer)
        freelancer_response = ProjectSolutionSpaceItemViewSet.as_view(
            {"get": "list"},
        )(freelancer_request)
        freelancer_ids = {
            item["id"] for item in freelancer_response.data["results"]
        }
        self.assertEqual(freelancer_ids, {project_item.id})

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_copy_to_project_selects_approved_subtree_and_is_idempotent(
        self,
        _permissions,
        _permission_lookup,
    ):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Reusable assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            required_skills=["drill"],
            provided_skills=["test"],
            responsible_engineer=self.engineer,
            year_of_construction=2025,
            custom_fields={"surface_finish": "polished"},
            created_by=self.owner,
        )
        source.customers.add(self.customer)
        child = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=source,
            level=2,
            assembly_name="Approved child",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            sort_order=1,
        )
        request = self.factory.post(
            f"/api/project_management/company-solution-space-items/{source.id}/copy-to-project/",
            {"project": self.project.id},
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view(
            {"post": "copy_to_project"}
        )(request, pk=source.id)

        self.assertEqual(response.status_code, 201, response.data)
        copied = ProjectSolutionSpaceItem.objects.get(id=response.data["id"])
        self.assertEqual(copied.source_company_item, source)
        self.assertEqual(copied.assembly_name, source.assembly_name)
        self.assertEqual(copied.custom_fields, source.custom_fields)
        self.assertTrue(source.projects.filter(id=self.project.id).exists())
        copied_child = ProjectSolutionSpaceItem.objects.get(
            project=self.project,
            source_company_item=child,
        )
        self.assertEqual(copied_child.parent, copied)
        self.assertEqual(copied.level, 1)
        self.assertEqual(copied_child.level, 2)

        source.assembly_name = "Updated company assembly"
        source.save(update_fields=["assembly_name"])
        copied.refresh_from_db()
        self.assertEqual(copied.assembly_name, "Reusable assembly")
        self.assertEqual(
            ProjectSolutionSpaceItemSerializer(copied).data["assembly_name"],
            "Updated company assembly",
        )

        repeated_request = self.factory.post(
            f"/api/project_management/company-solution-space-items/{source.id}/copy-to-project/",
            {"project": self.project.id},
            format="json",
        )
        force_authenticate(repeated_request, user=self.owner)
        repeated = CompanySolutionSpaceItemViewSet.as_view(
            {"post": "copy_to_project"}
        )(repeated_request, pk=source.id)
        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertEqual(
            ProjectSolutionSpaceItem.objects.filter(project=self.project).count(),
            2,
        )
        copied.refresh_from_db()
        self.assertEqual(copied.assembly_name, "Updated company assembly")

    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_copy_project_product_to_bom_preserves_identity_and_is_idempotent(
        self,
        _permissions,
    ):
        product = AvailableProductPart.objects.create(
            company_profile=self.company,
            name="Festo servo drive",
            part_number="CMMT-AS-C2-3A-MP-S1",
            custom_fields={"supplier": "Old supplier value"},
        )
        ProductAASProfile.objects.create(
            product=product,
            origin=ProductAASProfile.Origin.PURCHASED,
            manufacturer_name="Festo SE & Co. KG",
            supplier_name="Festo",
            supplier_aas_id="https://supplier.example/aas/cmmt",
            global_asset_id=(
                "https://smart.festo.com/asset/type/oc/CMMT-AS-C2-3A-MP-S1"
            ),
            order_code="CMMT-AS-C2-3A-MP-S1",
        )
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            aas_product=product,
            assembly_name="Approved servo drive",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
            custom_fields={"voltage": "400 V"},
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=source,
            assembly_name=source.assembly_name,
            assembly_type=source.assembly_type,
            custom_fields={"project_note": "Line 1"},
        )
        requirement = Requirement.objects.create(
            project=self.project,
            name="Positioning accuracy",
            object_number="REQ-100",
        )
        selected.requirements.add(requirement)

        request = self.factory.post(
            f"/api/project_management/project-solution-space-items/"
            f"{selected.id}/copy-to-bom/",
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = ProjectSolutionSpaceItemViewSet.as_view(
            {"post": "copy_to_bom"},
        )(request, pk=selected.id)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["created"])
        bom_part = BillOfMaterialPart.objects.get(
            id=response.data["bom_part"]["id"],
        )
        self.assertEqual(bom_part.project, self.project)
        self.assertIsNone(bom_part.parent)
        self.assertEqual(bom_part.level, 1)
        self.assertEqual(bom_part.sort_order, 1)
        self.assertEqual(bom_part.name, source.assembly_name)
        self.assertEqual(bom_part.part_number, product.part_number)
        self.assertEqual(bom_part.available_product, product)
        self.assertEqual(bom_part.source_project_assembly, selected)
        self.assertEqual(bom_part.custom_fields["quantity"], 1)
        self.assertEqual(bom_part.custom_fields["supplier"], "Festo")
        self.assertEqual(
            bom_part.custom_fields["global_asset_id"],
            "https://smart.festo.com/asset/type/oc/CMMT-AS-C2-3A-MP-S1",
        )
        self.assertEqual(bom_part.custom_fields["project_note"], "Line 1")
        self.assertEqual(bom_part.custom_fields["voltage"], "400 V")
        self.assertEqual(list(bom_part.requirements.all()), [requirement])

        repeated_request = self.factory.post(
            f"/api/project_management/project-solution-space-items/"
            f"{selected.id}/copy-to-bom/",
            format="json",
        )
        force_authenticate(repeated_request, user=self.owner)
        repeated = ProjectSolutionSpaceItemViewSet.as_view(
            {"post": "copy_to_bom"},
        )(repeated_request, pk=selected.id)

        self.assertEqual(repeated.status_code, 200, repeated.data)
        self.assertFalse(repeated.data["created"])
        self.assertEqual(repeated.data["bom_part"]["id"], bom_part.id)
        self.assertEqual(
            BillOfMaterialPart.objects.filter(
                project=self.project,
                source_project_assembly=selected,
            ).count(),
            1,
        )

    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_copy_project_process_to_bom_is_rejected(self, _permissions):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved production process",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=source,
            assembly_name=source.assembly_name,
            assembly_type=source.assembly_type,
        )
        request = self.factory.post(
            f"/api/project_management/project-solution-space-items/"
            f"{selected.id}/copy-to-bom/",
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = ProjectSolutionSpaceItemViewSet.as_view(
            {"post": "copy_to_bom"},
        )(request, pk=selected.id)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(BillOfMaterialPart.objects.count(), 0)

    def test_bom_source_project_assembly_is_server_controlled(self):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved product",
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=source,
            assembly_name=source.assembly_name,
        )
        serializer = BillOfMaterialPartSerializer(
            data={
                "project": self.project.id,
                "name": "Manual BoM row",
                "source_project_assembly": selected.id,
            },
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        bom_part = serializer.save()
        self.assertIsNone(bom_part.source_project_assembly)

    def test_copy_to_bom_uses_bom_create_permission(self):
        view = ProjectSolutionSpaceItemViewSet()
        view.action = "copy_to_bom"
        permission = view.get_permissions()[0]

        self.assertIsInstance(permission, BillOfMaterialPermission)
        self.assertEqual(permission.action, "create")

    def test_copy_to_bom_checks_create_instead_of_update_permission(self):
        view = ProjectSolutionSpaceItemViewSet()
        view.action = "copy_to_bom"
        permission = view.get_permissions()[0]
        request = SimpleNamespace(method="POST", user=self.owner)

        with (
            patch.object(permission, "_get_context", return_value={}),
            patch.object(
                permission,
                "_check_create_permission",
                return_value=True,
            ) as create_check,
            patch.object(permission, "_check_update_permission_project") as update_check,
        ):
            allowed = permission.has_object_permission(request, view, self.project)

        self.assertTrue(allowed)
        create_check.assert_called_once_with(self.owner, {})
        update_check.assert_not_called()

    def test_project_catalog_rejects_canonical_changes(self):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved process",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=source,
            assembly_name=source.assembly_name,
            assembly_type=source.assembly_type,
        )
        serializer = ProjectSolutionSpaceItemSerializer(
            selected,
            data={"assembly_name": "Project-only process"},
            partial=True,
            context={"request": self._request()},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("assembly_name", serializer.errors)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_filtered_child_response_includes_ancestor_path(
        self,
        _permissions,
        _permission_lookup,
    ):
        parent = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Parent assembly",
            sort_order=1,
        )
        child = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=parent,
            level=2,
            assembly_name="Matching child",
            sort_order=1,
        )
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Unrelated assembly",
            sort_order=2,
        )
        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {"assembly_name": "Matching child", "page_size": 1000},
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(request)
        result_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(result_ids, {parent.id, child.id})
        self.assertEqual(response.data["match_count"], 1)
        self.assertEqual(response.data["context_count"], 1)
        self.assertEqual(response.data["matched_ids"], [child.id])

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_hierarchy_pagination_counts_matches_without_ancestor_context(
        self,
        _permissions,
        _permission_lookup,
    ):
        parent = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Context parent",
            sort_order=1,
        )
        children = [
            CompanySolutionSpaceItem.objects.create(
                company_profile=self.company,
                parent=parent,
                level=2,
                assembly_name=f"Matching child {index}",
                sort_order=index,
            )
            for index in range(1, 4)
        ]

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {"search": "Matching child", "page": 2, "page_size": 2},
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
            request
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["match_count"], 3)
        self.assertEqual(response.data["context_count"], 1)
        self.assertEqual(response.data["matched_ids"], [children[2].id])
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [parent.id, children[2].id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_explicit_ordering_is_applied_before_pagination(
        self,
        _permissions,
        _permission_lookup,
    ):
        CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering Zulu",
            sort_order=1,
        )
        alpha = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering Alpha",
            sort_order=2,
        )

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {
                "search": "Ordering",
                "ordering": "assembly_name",
                "page": 1,
                "page_size": 1,
            },
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
            request
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["matched_ids"], [alpha.id])
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [alpha.id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_related_assembly_ordering_uses_displayed_names_and_keeps_empty_last(
        self,
        _permissions,
        _permission_lookup,
    ):
        process_10 = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Process 10",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        process_2 = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Process 2",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        linked_to_10 = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering related alpha",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        linked_to_2 = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering related beta",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        unlinked = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering related missing",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        CompanyAssemblyProductionLink.objects.create(
            product_assembly=linked_to_10,
            process_assembly=process_10,
        )
        CompanyAssemblyProductionLink.objects.create(
            product_assembly=linked_to_2,
            process_assembly=process_2,
        )

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {
                "search": "Ordering related",
                "assembly_type": CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
                "ordering": "related_assemblies",
                "page_size": 25,
            },
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
            request
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [linked_to_2.id, linked_to_10.id, unlinked.id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_linked_document_ordering_uses_document_count(
        self,
        _permissions,
        _permission_lookup,
    ):
        folder = Folder.objects.create(
            company_profile=self.company,
            name="Assembly ordering documents",
        )
        documents = [
            Document.objects.create(
                folder=folder,
                file=f"https://example.com/assembly-ordering-{index}.pdf",
                file_name=f"assembly-ordering-{index}.pdf",
                created_by=self.owner,
            )
            for index in range(2)
        ]
        two_documents = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering documents two",
        )
        one_document = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering documents one",
        )
        no_documents = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Ordering documents none",
        )
        two_documents.documents.add(*documents)
        one_document.documents.add(documents[0])

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {
                "search": "Ordering documents",
                "ordering": "linked_documents",
                "page_size": 25,
            },
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
            request
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [no_documents.id, one_document.id, two_documents.id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_related_assembly_filter_and_details_are_bidirectional(
        self,
        _permissions,
        _permission_lookup,
    ):
        product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Linked product",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        process = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Linked process",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        CompanyAssemblyProductionLink.objects.create(
            product_assembly=product,
            process_assembly=process,
        )

        for source, related in ((product, process), (process, product)):
            with self.subTest(source=source.id):
                request = self.factory.get(
                    "/api/project_management/company-solution-space-items/",
                    {
                        "linked_assembly_ids": related.id,
                        "assembly_type": source.assembly_type,
                        "page_size": 25,
                    },
                )
                force_authenticate(request, user=self.owner)
                response = CompanySolutionSpaceItemViewSet.as_view(
                    {"get": "list"}
                )(request)
                self.assertEqual(response.data["count"], 1)
                row = response.data["results"][0]
                self.assertEqual(row["id"], source.id)
                self.assertEqual(
                    row["related_assembly_details"],
                    [
                        {
                            "id": related.id,
                            "assembly_name": related.assembly_name,
                            "assembly_type": related.assembly_type,
                        }
                    ],
                )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_link_update_marks_both_published_assemblies_out_of_date(
        self,
        _permissions,
        _permission_lookup,
    ):
        product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Published product",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        process = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Published process",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        identities = [
            AASResourceIdentity.objects.create(
                company=self.company,
                source_type=AASResourceIdentity.SourceType.COMPANY_ASSEMBLY,
                company_assembly=assembly,
                out_of_date=False,
            )
            for assembly in (product, process)
        ]
        request = self.factory.patch(
            f"/api/project_management/company-solution-space-items/{product.id}/",
            {"related_assembly_ids": [process.id]},
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view(
            {"patch": "partial_update"}
        )(request, pk=product.id)

        self.assertEqual(response.status_code, 200, response.data)
        for identity in identities:
            identity.refresh_from_db()
            self.assertTrue(identity.out_of_date)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(AvailableProductPartViewSet, "get_permissions", return_value=[])
    def test_available_product_filters_run_before_pagination(
        self,
        _permissions,
        _permission_lookup,
    ):
        products = []
        for index, supplier in enumerate(("Festo", "Festo", "SICK"), start=1):
            product = AvailableProductPart.objects.create(
                company_profile=self.company,
                name=f"Servo product {index}",
                part_number=f"PART-{index}",
                sort_order=index,
            )
            ProductAASProfile.objects.create(
                product=product,
                origin=ProductAASProfile.Origin.PURCHASED,
                supplier_name=supplier,
                manufacturer_name=supplier,
            )
            products.append(product)

        request = self.factory.get(
            "/api/project_management/available-product-parts/",
            {
                "search": "Servo",
                "origin": "purchased",
                "supplier": "Festo",
                "page": 2,
                "page_size": 1,
            },
        )
        force_authenticate(request, user=self.owner)
        response = AvailableProductPartViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(response.data["matched_ids"], [products[1].id])
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [products[1].id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(BillOfMaterialPartViewSet, "get_permissions", return_value=[])
    def test_bom_filters_run_before_pagination_and_keep_parent_context(
        self,
        _permissions,
        _permission_lookup,
    ):
        parent = BillOfMaterialPart.objects.create(
            project=self.project,
            name="Line",
            sort_order=1,
        )
        matching = BillOfMaterialPart.objects.create(
            project=self.project,
            parent=parent,
            level=2,
            name="Festo drive",
            part_number="CMMT",
            status="Planned",
            custom_fields={"supplier": "Festo"},
            sort_order=1,
        )
        BillOfMaterialPart.objects.create(
            project=self.project,
            parent=parent,
            level=2,
            name="SICK scanner",
            status="Installed",
            custom_fields={"supplier": "SICK"},
            sort_order=2,
        )

        request = self.factory.get(
            "/api/project_management/bom-parts/",
            {
                "project": self.project.id,
                "status": "Planned",
                "supplier": "Festo",
                "page_size": 25,
            },
        )
        force_authenticate(request, user=self.owner)
        response = BillOfMaterialPartViewSet.as_view({"get": "list"})(request)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["matched_ids"], [matching.id])
        self.assertEqual(
            [row["id"] for row in response.data["results"]],
            [parent.id, matching.id],
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_company_filters_remain_and_across_categories(
        self,
        _permissions,
        _permission_lookup,
    ):
        matching = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Matching project and year",
            year_of_construction=2026,
            required_skills=["drill"],
            sort_order=1,
        )
        matching.projects.add(self.project)
        wrong_year = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Matching project only",
            year_of_construction=2025,
            required_skills=["drill"],
            sort_order=2,
        )
        wrong_year.projects.add(self.project)

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {
                "search": "Main Project",
                "year_of_construction": "2026",
                "required_skills": "drill",
                "page_size": 1000,
            },
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(request)
        result_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(result_ids, {matching.id})
        self.assertNotIn(wrong_year.id, result_ids)

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_company_filters_cover_all_requested_fields(
        self,
        _permissions,
        _permission_lookup,
    ):
        matching = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Filter target assembly",
            required_skills=["drill"],
            provided_skills=["test"],
            responsible_engineer=self.engineer,
            assembly_type="components",
            year_of_construction=2026,
            sort_order=1,
        )
        matching.projects.add(self.project)
        matching.customers.add(self.customer)
        other = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Other assembly",
            required_skills=["assemble"],
            provided_skills=["assemble"],
            assembly_type="production_lines",
            year_of_construction=2025,
            sort_order=2,
        )
        filter_cases = (
            {"assembly_name": "Filter target"},
            {"assembly_type": "components"},
            {"required_skills": "drill"},
            {"provided_skills": "test"},
            {"responsible_engineers": str(self.engineer.id)},
            {"project_ids": str(self.project.id)},
            {"project_name": "Main Project"},
            {"project_reference": str(self.project.id)},
            {"customer_ids": str(self.customer.id)},
            {"customer_name": "Network Customer"},
            {"year_of_construction": "2026"},
        )

        for query in filter_cases:
            with self.subTest(query=query):
                request = self.factory.get(
                    "/api/project_management/company-solution-space-items/",
                    {**query, "page_size": 1000},
                )
                force_authenticate(request, user=self.owner)
                response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(
                    request,
                )
                result_ids = {item["id"] for item in response.data["results"]}
                self.assertIn(matching.id, result_ids)
                self.assertNotIn(other.id, result_ids)

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {"assembly_type": "invalid", "page_size": 1000},
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(request)
        result_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(result_ids, {matching.id, other.id})

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_company_product_filter_keeps_legacy_unclassified_rows_visible(
        self,
        _permissions,
        _permission_lookup,
    ):
        default_product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Default product assembly",
        )
        legacy_product = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Legacy product assembly",
            assembly_type=None,
        )
        process = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Process assembly",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )

        request = self.factory.get(
            "/api/project_management/company-solution-space-items/",
            {"assembly_type": "components", "page_size": 1000},
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view({"get": "list"})(request)
        result_ids = {item["id"] for item in response.data["results"]}

        default_product.refresh_from_db()
        self.assertEqual(
            default_product.assembly_type,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        self.assertEqual(result_ids, {default_product.id, legacy_product.id})
        self.assertNotIn(process.id, result_ids)

    def test_legacy_assembly_type_backfill_preserves_hierarchy_and_source(self):
        process_parent = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Process parent",
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        process_child = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            parent=process_parent,
            level=2,
            assembly_name="Legacy process child",
            assembly_type=None,
        )
        product_root = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Legacy product root",
            assembly_type=None,
        )
        project_snapshot = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=process_child,
            assembly_name=process_child.assembly_name,
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )

        migration = import_module(
            "project_management.migrations.0172_backfill_legacy_assembly_types"
        )
        migration.backfill_legacy_assembly_types(
            apps,
            SimpleNamespace(connection=connection),
        )

        process_child.refresh_from_db()
        product_root.refresh_from_db()
        project_snapshot.refresh_from_db()
        self.assertEqual(
            process_child.assembly_type,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )
        self.assertEqual(
            product_root.assembly_type,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        self.assertEqual(
            project_snapshot.assembly_type,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        )

    @patch(
        "project_management.views.matrix.require_any_permission_multiple_modules",
        return_value=True,
    )
    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_project_assembly_type_filter(self, _permissions, _permission_lookup):
        matching_source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved components assembly",
            assembly_type="components",
            sort_order=1,
        )
        other_source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved production line assembly",
            assembly_type="production_lines",
            sort_order=2,
        )
        matching = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=matching_source,
            assembly_name="Components assembly",
            assembly_type="components",
            sort_order=1,
        )
        other = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=other_source,
            assembly_name="Production line assembly",
            assembly_type="production_lines",
            sort_order=2,
        )
        legacy = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=matching_source,
            assembly_name="Legacy components assembly",
            assembly_type=None,
            sort_order=3,
        )

        request = self.factory.get(
            "/api/project_management/project-solution-space-items/",
            {
                "project": self.project.id,
                "assembly_type": "components",
                "page_size": 1000,
            },
        )
        force_authenticate(request, user=self.owner)
        response = ProjectSolutionSpaceItemViewSet.as_view({"get": "list"})(
            request,
        )
        result_ids = {item["id"] for item in response.data["results"]}

        self.assertEqual(result_ids, {matching.id, legacy.id})
        self.assertNotIn(other.id, result_ids)

    @patch.object(ProjectSolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_project_hierarchy_mutations_are_rejected(self, _permissions):
        source = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Approved assembly",
            assembly_type="components",
        )
        selected = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            source_company_item=source,
            assembly_name=source.assembly_name,
            assembly_type=source.assembly_type,
        )

        for action, url_path in (
            ("move_up", "move-up"),
            ("move_down", "move-down"),
            ("indent", "indent"),
            ("outdent", "outdent"),
        ):
            with self.subTest(action=action):
                request = self.factory.post(
                    f"/api/project_management/project-solution-space-items/"
                    f"{selected.id}/{url_path}/",
                )
                force_authenticate(request, user=self.owner)
                response = ProjectSolutionSpaceItemViewSet.as_view(
                    {"post": action},
                )(request, pk=selected.id)
                self.assertEqual(response.status_code, 400)
                self.assertIn("company catalog", response.data["detail"])

    def test_comments_support_both_solution_space_targets(self):
        company_item = CompanySolutionSpaceItem.objects.create(
            company_profile=self.company,
            assembly_name="Commented company item",
        )
        project_item = ProjectSolutionSpaceItem.objects.create(
            project=self.project,
            assembly_name="Commented project item",
        )
        for target_type, target_field, target in (
            (
                MatrixComment.TARGET_COMPANY_SOLUTION_SPACE,
                "company_solution_space_item",
                company_item,
            ),
            (
                MatrixComment.TARGET_PROJECT_SOLUTION_SPACE,
                "project_solution_space_item",
                project_item,
            ),
        ):
            serializer = MatrixCommentSerializer(
                data={
                    "target_type": target_type,
                    target_field: target.id,
                    "content": "Solution Space comment",
                },
                context={"request": self._request()},
            )
            self.assertTrue(serializer.is_valid(), serializer.errors)
            comment = serializer.save()
            self.assertEqual(comment.target_type, target_type)
            self.assertEqual(getattr(comment, f"{target_field}_id"), target.id)

    @patch(
        "aas_integration.permissions.has_aas_models_permission",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_ai_search_copy_creates_purchased_product_assembly_and_aas(
        self,
        _permissions,
        _aas_permission,
    ):
        payload = {
            "source": "ai_search",
            "source_product_id": "festo-result-1",
            "name": "CMMT-AS-C2-3A-MP-S1",
            "article_number": "8167904",
            "supplier": "Festo",
            "supplier_aas_id": "https://smart.festo.com/aas/8167904",
            "global_asset_id": (
                "https://smart.festo.com/asset/type/oc/CMMT-AS-C2-3A-MP-S1"
            ),
            "asset_kind": "Type",
            "product_url": "https://www.festo.com/product/8167904",
            "technical_fields": [
                {
                    "key": "manufacturer",
                    "label": "Manufacturer",
                    "value": "Festo SE & Co. KG",
                },
                {
                    "key": "rated_voltage",
                    "label": "Rated voltage",
                    "value": "24 V",
                },
            ],
        }
        request = self.factory.post(
            "/api/project_management/company-solution-space-items/"
            "copy-supplier-product/",
            payload,
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view(
            {"post": "copy_supplier_product"}
        )(request)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(response.data["created_product"])
        self.assertTrue(response.data["created_assembly"])
        assembly = CompanySolutionSpaceItem.objects.get(
            id=response.data["assembly"]["id"]
        )
        product = assembly.aas_product
        self.assertEqual(
            assembly.assembly_type,
            CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        )
        self.assertEqual(product.name, payload["name"])
        self.assertEqual(product.part_number, payload["article_number"])
        self.assertEqual(product.aas_profile.origin, ProductAASProfile.Origin.PURCHASED)
        self.assertEqual(
            product.aas_profile.supplier_aas_id,
            payload["supplier_aas_id"],
        )
        self.assertEqual(
            product.aas_profile.global_asset_id,
            payload["global_asset_id"],
        )
        self.assertEqual(len(product.aas_profile.source_payload_hash), 64)
        self.assertTrue(hasattr(product, "aas_identity"))

        document = ProductAASMapper().map(
            product,
            product.aas_identity.logical_aas_id,
        )
        technical_data = next(
            submodel
            for submodel in document.submodels
            if submodel["idShort"] == "TechnicalData"
        )
        technical_values = {
            element["idShort"]: element["value"]
            for element in technical_data["submodelElements"]
        }
        self.assertEqual(technical_values["manufacturer"], "Festo SE & Co. KG")
        self.assertEqual(technical_values["rated_voltage"], "24 V")

    @patch(
        "aas_integration.permissions.has_aas_models_permission",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_ai_search_copy_is_idempotent_per_company_supplier_identity(
        self,
        _permissions,
        _aas_permission,
    ):
        payload = {
            "source": "ai_search",
            "name": "Supplier motor",
            "article_number": "MOTOR-1",
            "supplier": "Supplier AG",
            "supplier_aas_id": "urn:supplier:aas:motor-1",
            "global_asset_id": "urn:supplier:asset:motor-1",
            "technical_fields": [],
        }
        responses = []
        for _ in range(2):
            request = self.factory.post(
                "/api/project_management/company-solution-space-items/"
                "copy-supplier-product/",
                payload,
                format="json",
            )
            force_authenticate(request, user=self.owner)
            responses.append(
                CompanySolutionSpaceItemViewSet.as_view(
                    {"post": "copy_supplier_product"}
                )(request)
            )

        self.assertEqual(responses[0].status_code, 201, responses[0].data)
        self.assertEqual(responses[1].status_code, 200, responses[1].data)
        self.assertFalse(responses[1].data["created"])
        self.assertEqual(
            responses[0].data["assembly"]["id"],
            responses[1].data["assembly"]["id"],
        )
        self.assertEqual(
            ProductAASProfile.objects.filter(
                product__company_profile=self.company,
                supplier_aas_id=payload["supplier_aas_id"],
            ).count(),
            1,
        )

    @patch(
        "aas_integration.permissions.has_aas_models_permission",
        return_value=True,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_ai_search_copy_requires_verified_supplier_identity(
        self,
        _permissions,
        _aas_permission,
    ):
        request = self.factory.post(
            "/api/project_management/company-solution-space-items/"
            "copy-supplier-product/",
            {
                "source": "ai_search",
                "name": "Unverified result",
                "supplier": "Supplier AG",
            },
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view(
            {"post": "copy_supplier_product"}
        )(request)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            AvailableProductPart.objects.filter(name="Unverified result").exists()
        )

    @patch(
        "aas_integration.permissions.has_aas_models_permission",
        return_value=False,
    )
    @patch.object(CompanySolutionSpaceItemViewSet, "get_permissions", return_value=[])
    def test_ai_search_copy_requires_aas_write_access(
        self,
        _permissions,
        _aas_permission,
    ):
        request = self.factory.post(
            "/api/project_management/company-solution-space-items/"
            "copy-supplier-product/",
            {
                "source": "ai_search",
                "name": "Forbidden result",
                "supplier": "Supplier AG",
                "supplier_aas_id": "urn:supplier:aas:forbidden",
            },
            format="json",
        )
        force_authenticate(request, user=self.owner)
        response = CompanySolutionSpaceItemViewSet.as_view(
            {"post": "copy_supplier_product"}
        )(request)

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AvailableProductPart.objects.filter(name="Forbidden result").exists()
        )
