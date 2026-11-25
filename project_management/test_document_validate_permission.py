"""GED-WF.1.1: the documents.validate catalog entry and its controlled backfill."""
import json
import os
import tempfile
from io import StringIO

from django.core.management import call_command
from rest_framework.test import APIRequestFactory, force_authenticate

from models.models import Permission, Role, RolePermission, UserRole
from project_management.test_document_workflow import THREE, WorkflowTestBase

KEY = "documents.validate"


class ValidatePermissionCatalogTests(WorkflowTestBase):
    def test_the_permission_exists_once_and_is_granted_to_nobody_by_the_migration(self):
        self.assertEqual(Permission.objects.filter(key=KEY).count(), 1)
        self.assertEqual(Permission.objects.get(key=KEY).module, "documents")
        self.assertFalse(RolePermission.objects.filter(permission__key=KEY).exists())

    def test_it_is_offered_in_the_company_and_project_solution_role_catalogs(self):
        from companyprofileapis.views.role import RoleViewSet
        for scope in ("company", "project"):
            request = APIRequestFactory().get("/x/", {"type": "solution", "scope": scope})
            force_authenticate(request, user=self.creator)
            response = RoleViewSet.as_view({"get": "get_permissions_template"}, permission_classes=[])(request)
            self.assertEqual(response.status_code, 200)
            self.assertIn("validate", response.data["documents"], scope)
            self.assertIn("manage_cycle", response.data["documents"], scope)  # untouched
        # not offered for another role type
        request = APIRequestFactory().get("/x/", {"type": "client", "scope": "company"})
        force_authenticate(request, user=self.creator)
        response = RoleViewSet.as_view({"get": "get_permissions_template"}, permission_classes=[])(request)
        self.assertNotIn("validate", response.data.get("documents", {}))


class BackfillDocumentValidateTests(WorkflowTestBase):
    def setUp(self):
        super().setUp()
        self.company = self.document.folder.company_profile
        for user in (self.creator, self.alice, self.bob, self.carol, self.dave):
            user.company = self.company
            user.save(update_fields=["company"])
        self.document.author = self.alice
        self.document.save(update_fields=["author"])
        self.start(THREE, revision=self.revision2)  # alice author, bob verifier, carol approver

    def role(self, user, *keys, default=False, active=True):
        role = Role.objects.create(name=f"bf-{user.pk}-{Role.objects.count()}", company=self.company, scope="company",
                                   type="solution", is_default=default, is_active=active)
        for key in keys:
            permission, _ = Permission.objects.get_or_create(key=key, defaults={"module": "documents", "name": key})
            RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=user, role=role, company=self.company)
        return role

    def run_command(self, *args):
        out = StringIO()
        call_command("backfill_document_validate", *args, stdout=out)
        return out.getvalue()

    def holders(self):
        return set(RolePermission.objects.filter(permission__key=KEY).values_list("role_id", flat=True))

    def test_dry_run_changes_nothing_and_apply_grants_only_the_legitimate_roles(self):
        alice = self.role(self.alice, "documents.view", "documents.update")        # access + actor -> grant
        bob = self.role(self.bob, "documents.update")                               # update without access -> no
        carol = self.role(self.carol, "documents.view", "documents.full_access")    # already covered -> no
        dave = self.role(self.dave, "documents.view", "documents.update")           # not an actor -> no
        self.run_command()
        self.assertEqual(self.holders(), set())
        with tempfile.TemporaryDirectory() as folder:
            backup = os.path.join(folder, "backup.json")
            self.run_command("--apply", "--backup", backup)
            self.assertEqual(self.holders(), {alice.pk})
            self.assertEqual(json.load(open(backup))["granted_role_ids"], [alice.pk])
            self.assertNotIn(bob.pk, self.holders())
            self.assertNotIn(carol.pk, self.holders())
            self.assertNotIn(dave.pk, self.holders())
            self.run_command("--apply", "--backup", backup)  # idempotent, no duplicate
            self.assertEqual(RolePermission.objects.filter(permission__key=KEY, role=alice).count(), 1)
            self.run_command("--revert", backup)
            self.assertEqual(self.holders(), set())

    def test_default_inactive_and_other_company_roles_are_never_granted(self):
        shared = self.role(self.alice, "documents.view", default=True)
        inactive = self.role(self.bob, "documents.view", active=False)
        outsider_company = self.carol.company
        self.carol.company = None
        self.carol.save(update_fields=["company"])
        foreign = self.role(self.carol, "documents.view")
        with tempfile.TemporaryDirectory() as folder:
            self.run_command("--apply", "--backup", os.path.join(folder, "b.json"))
        self.assertEqual(self.holders(), set())
        self.assertTrue(shared.pk and inactive.pk and foreign.pk and outsider_company)

    def test_apply_requires_a_backup_file(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_command("--apply")


from project_management import test_company_owner_document_access as _owner_suite  # noqa: E402

_OwnerBase = _owner_suite.CompanyOwnerDocumentAccessTests


class OwnerValidateRightTests(_OwnerBase):
    """The documentary owner bypass (company owner outside the team) keeps working for validation: an
    owner holding the company documents.full_access is recognised by can_validate_document (same mechanism as
    the update right). Reuses the owner fixtures of the owner suite."""

    def test_owner_with_company_full_access_is_recognised(self):
        from project_management.models import ProjectTeamMembers
        from project_management.services.document_cycle import can_validate_document
        self.grant_company_documents(self.owner)
        self.project.user = self.approver
        self.project.save(update_fields=["user"])
        ProjectTeamMembers.objects.filter(team_member=self.owner).delete()
        document = self.make_document()
        self.assertTrue(can_validate_document(self.owner, document))


for _name in [n for n in dir(_OwnerBase) if n.startswith("test_")]:
    setattr(OwnerValidateRightTests, _name, None)  # do not re-run the owner suite from here

del _OwnerBase  # not collected from this module