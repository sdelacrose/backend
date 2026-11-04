from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate
from models.models import Permission, Role, RolePermission, UserRole
from project_management.models import DocumentWorkflow, DocumentWorkflowEvent
from project_management.services.document_cycle import (
    assert_can_manage_document_cycle, can_manage_cycle_identity,
    document_cycle_permissions, has_document_control_right,
)
from project_management.services.document_workflow import perform_action, stop_workflow
from project_management.test_document_workflow import WorkflowTestBase, THREE
from project_management.views.document_workflow import DocumentWorkflowViewSet
from project_management.views.document_revision import DocumentRevisionViewSet


class DocumentCycleControlsTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.document.author = self.alice
        self.document.save(update_fields=["author"])
        self.company = self.document.folder.company_profile
        for user in (self.creator, self.alice, self.bob, self.carol, self.dave):
            user.company = self.company
            user.save(update_fields=["company"])

    def grant(self, user, *keys):
        role = Role.objects.create(name=f"Cycle role {user.pk}-{Role.objects.count()}", company=self.company,
                                   scope="company", type="solution", is_default=False)
        for key in keys:
            permission, _ = Permission.objects.get_or_create(key=key, defaults={"module": "documents", "name": key})
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=self.company)
        return role

    def post_workflow(self, user, data=None, action=False):
        request = APIRequestFactory().post("/x/", data or {
            "workflow_type": THREE, "author": self.alice.pk,
            "verifier": self.bob.pk, "approver": self.carol.pk,
        }, format="json")
        force_authenticate(request, user=user)
        view = DocumentWorkflowViewSet.as_view({"post": "act" if action else "start"}, permission_classes=[])
        with patch("project_management.views.document_workflow.get_register_document_or_404", return_value=self.document):
            return view(request, document_id=self.document.pk, revision_id=self.revision2.pk)

    def test_author_and_creator_have_cycle_identity_but_need_update_right(self):
        for user in (self.creator, self.alice):
            self.assertTrue(can_manage_cycle_identity(user, self.document))
            self.assertFalse(document_cycle_permissions(user, self.document)["can_manage"])
            self.grant(user, "documents.update")
            self.assertTrue(document_cycle_permissions(user, self.document)["can_manage"])

    def test_generic_full_access_does_not_grant_document_control(self):
        self.grant(self.dave, "documents.full_access")
        self.assertFalse(has_document_control_right(self.dave, self.document))
        with self.assertRaises(PermissionDenied):
            assert_can_manage_document_cycle(self.dave, self.document)
        self.assertEqual(self.post_workflow(self.dave).status_code, 403)
        self.assertFalse(DocumentWorkflow.objects.exists())

    def test_explicit_controller_starts_without_signing_for_author(self):
        self.grant(self.dave, "documents.update", "documents.manage_cycle")
        self.assertTrue(has_document_control_right(self.dave, self.document))
        response = self.post_workflow(self.dave)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "DRAFT")
        self.assertEqual([(e["action"], e["actor"]["id"]) for e in response.data["events"]], [("START", self.dave.pk)])
        self.assertEqual(self.post_workflow(self.dave, {"action": "SIGN"}, True).status_code, 403)

    def test_author_launch_signs_with_real_update_permission(self):
        self.grant(self.alice, "documents.update")
        response = self.post_workflow(self.alice)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "VERIFY")
        self.assertEqual(response.data["events"][1]["actor"]["id"], self.alice.pk)

    def test_inactive_controller_role_is_denied(self):
        role = self.grant(self.dave, "documents.update", "documents.manage_cycle")
        role.is_active = False
        role.save(update_fields=["is_active"])
        self.assertFalse(has_document_control_right(self.dave, self.document))

    def test_company_right_never_applies_to_another_company(self):
        self.grant(self.dave, "documents.update", "documents.manage_cycle")
        self.dave.company = None
        self.dave.save(update_fields=["company"])
        self.assertFalse(has_document_control_right(self.dave, self.document))

    def test_generic_updater_cannot_create_revision(self):
        self.grant(self.dave, "documents.update")
        request = APIRequestFactory().post("/x/", {"file": SimpleUploadedFile("next.txt", b"new"),
            "revision_type": "minor", "comment": "Correction"}, format="multipart")
        force_authenticate(request, user=self.dave)
        view = DocumentRevisionViewSet.as_view({"post": "create"}, permission_classes=[])
        count = self.document.revisions.count()
        with patch("project_management.views.document_revision.get_register_document_or_404", return_value=self.document):
            response = view(request, document_id=self.document.pk)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.document.revisions.count(), count)

    def test_controller_can_create_revision_without_becoming_the_document_author(self):
        self.grant(self.dave, "documents.update", "documents.manage_cycle")
        creator_id = self.document.created_by_id
        request = APIRequestFactory().post("/x/", {"file": SimpleUploadedFile("controller.txt", b"corrected"),
            "revision_type": "minor", "comment": "Controller correction"}, format="multipart")
        force_authenticate(request, user=self.dave)
        view = DocumentRevisionViewSet.as_view({"post": "create"}, permission_classes=[])
        with patch("project_management.views.document_revision.get_register_document_or_404", return_value=self.document):
            response = view(request, document_id=self.document.pk)
        self.assertEqual(response.status_code, 201)
        self.document.refresh_from_db()
        self.assertEqual(self.document.author_id, self.alice.pk)
        self.assertEqual(self.document.created_by_id, creator_id)
        self.assertEqual(self.document.revisions.order_by("-id").first().created_by_id, self.dave.pk)

    def test_cycle_permission_can_be_held_in_a_second_role(self):
        self.grant(self.dave, "documents.update")
        self.grant(self.dave, "documents.manage_cycle")
        self.assertTrue(has_document_control_right(self.dave, self.document))
        self.assertTrue(document_cycle_permissions(self.dave, self.document)["can_manage"])

    def test_project_cycle_right_is_scoped_to_the_assigned_project(self):
        from project_management.models import Project, ProjectTeamMembers
        first = Project.objects.create(project_name="Cycle scope A", company_profile=self.company,
            user=self.creator, created_by=self.creator, contract_value=1000, solution="Energy Consultant Solution", project_end_date="2027-01-31")
        second = Project.objects.create(project_name="Cycle scope B", company_profile=self.company,
            user=self.creator, created_by=self.creator, contract_value=1000, solution="Energy Consultant Solution", project_end_date="2027-01-31")
        permission, _ = Permission.objects.get_or_create(key="documents.manage_cycle", defaults={"name": "Manage cycle", "module": "documents"})
        role = Role.objects.create(name="Project controller", scope="project", type="solution", company=self.company)
        RolePermission.objects.create(role=role, permission=permission)
        ProjectTeamMembers.objects.create(project=first, team_member=self.dave, project_role=role, role="Controller")
        folder = self.document.folder
        folder.company_profile = None
        folder.project = first
        folder.save(update_fields=["company_profile", "project"])
        self.document.refresh_from_db()
        self.assertTrue(has_document_control_right(self.dave, self.document))
        folder.project = second
        folder.save(update_fields=["project"])
        self.document.refresh_from_db()
        self.assertFalse(has_document_control_right(self.dave, self.document))

    def test_stop_preserves_signature_and_history_and_unlocks_file(self):
        self.grant(self.alice, "documents.update")
        wf = self.start(THREE, revision=self.revision2)
        perform_action(wf, self.alice, "SIGN", "")
        before = list(wf.events.values())
        stopped = stop_workflow(wf, self.alice, "  Author unavailable  ")
        self.assertEqual(stopped.status, "CANCELLED")
        self.assertFalse(stopped.is_active)
        self.assertIsNotNone(stopped.completed_at)
        self.assertEqual(list(wf.events.order_by("id").values())[:len(before)], before)
        event = wf.events.order_by("id").last()
        self.assertEqual((event.action, event.actor_id, event.comment), ("STOP", self.alice.pk, "Author unavailable"))
        self.assertIsNone(self.revision2.revision_label)
        with self.assertRaises(ValidationError):
            perform_action(stopped, self.bob, "VALIDATE", "")
        from project_management.services.document_workflow import assert_document_file_unlocked
        assert_document_file_unlocked(self.document)

    def test_creator_can_stop_and_controller_alone_cannot(self):
        self.grant(self.creator, "documents.update")
        self.grant(self.dave, "documents.update", "documents.manage_cycle")
        wf = self.start(THREE, revision=self.revision2)
        with self.assertRaises(PermissionDenied):
            stop_workflow(wf, self.dave, "No authority")
        self.assertEqual(stop_workflow(wf, self.creator, "Stop requested").status, "CANCELLED")

    def test_blank_reason_and_repeated_stop_leave_no_extra_events(self):
        self.grant(self.alice, "documents.update")
        wf = self.start(THREE, revision=self.revision2)
        count = wf.events.count()
        with self.assertRaises(ValidationError):
            stop_workflow(wf, self.alice, "   ")
        self.assertEqual(wf.events.count(), count)
        stop_workflow(wf, self.alice, "A reason")
        with self.assertRaises(ValidationError):
            stop_workflow(wf, self.alice, "Again")
        self.assertEqual(wf.events.count(), count + 1)

    def test_stop_action_endpoint_enforces_actor_and_comment(self):
        self.grant(self.alice, "documents.update")
        self.start(THREE, revision=self.revision2)
        self.assertEqual(self.post_workflow(self.alice, {"action": "STOP", "comment": " "}, True).status_code, 400)
        response = self.post_workflow(self.alice, {"action": "STOP", "comment": "Reason"}, True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "CANCELLED")
        self.assertFalse(response.data["can_stop"])

    def test_permission_catalog_does_not_grant_it_to_any_role(self):
        permission, _ = Permission.objects.get_or_create(key="documents.manage_cycle", defaults={"name": "Manage cycle", "module": "documents"})
        self.assertFalse(RolePermission.objects.filter(permission=permission).exists())
        from companyprofileapis.serializers.role import RoleSerializer
        serializer = RoleSerializer(data={"name": "New controller", "scope": "company", "type": "solution",
            "permissions_json": {"documents": {"manage_cycle": True}}})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_permission_template_exposes_unassigned_cycle_right_as_false(self):
        Permission.objects.get_or_create(key="documents.manage_cycle", defaults={"name": "Manage cycle", "module": "documents"})
        from companyprofileapis.views.role import RoleViewSet
        request = APIRequestFactory().get("/x/", {"type": "solution", "scope": "company"})
        force_authenticate(request, user=self.creator)
        response = RoleViewSet.as_view({"get": "get_permissions_template"}, permission_classes=[])(request)
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.data["documents"]["manage_cycle"], False)
        self.assertFalse(RolePermission.objects.filter(permission__key="documents.manage_cycle").exists())

    def test_withdrawn_workflow_cannot_be_stopped(self):
        self.grant(self.alice, "documents.update")
        workflow = self.start(THREE, revision=self.revision2)
        from django.utils import timezone
        self.document.withdrawn_at = timezone.now()
        self.document.save(update_fields=["withdrawn_at"])
        before = workflow.events.count()
        with self.assertRaises(ValidationError):
            stop_workflow(workflow, self.alice, "Stop")
        self.assertEqual(workflow.events.count(), before)
