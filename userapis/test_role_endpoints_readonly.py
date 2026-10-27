# SEC-PERM-02: the LINKS role endpoints (user-role and role) are read-only. Role and role
# assignment writes belong to admin-server-backend (RolesPermission-protected); here every write
# verb must answer 405 and leave the database untouched.
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from models.models import (
    CompanyProfile,
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
)
from project_management.test_document_revision import NoNotificationsMixin

USER_ROLE_URL = "/api/user/user-role/"
ROLE_URL = "/api/company/role/"


class RoleEndpointsReadOnlyTests(NoNotificationsMixin, TestCase):
    def setUp(self):
        self.silence_notifications()
        # The global product-license check queries tables absent from the test DB.
        patcher = patch("ecpmpapp.permissions.LicensePermission.has_permission", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.admin = self._user("roles.admin@links-local.invalid")
        self.company = CompanyProfile.objects.create(
            admin_user=self.admin, company_name="Roles Co", company_address="1",
            post_code="0", company_town="T", company_country="C", company_tell_no="0",
            company_mob_no="1", company_web_address="http://x", company_email_address="r@x.invalid",
            company_tax_reference="T", company_iban="I",
        )
        self.admin.company = self.company
        self.admin.save(update_fields=["company"])

        self.permission, _ = Permission.objects.get_or_create(
            key="contact_management.view",
            defaults={"name": "contact_management.view", "module": "contact_management", "description": "test"},
        )
        self.user_role_def = self._role("Ordinary", "solution")
        self.privileged_role_def = self._role("Privileged", "solution")
        self.admin_server_role_def = self._role("Admin server", "admin_server")

        self.user = self._member("ordinary@links-local.invalid", self.user_role_def)
        self.colleague = self._member("colleague@links-local.invalid", None)
        self.other_member = self._member("other@links-local.invalid", self.privileged_role_def)

        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.own_assignment = UserRole.objects.get(user=self.user)

    def _user(self, email):
        return User.objects.create_user(
            email=email, password="x", first_name="T", last_name="U", user_type="company_user"
        )

    def _role(self, name, role_type):
        role = Role.objects.create(name=name, company=self.company, type=role_type, scope="company")
        RolePermission.objects.create(role=role, permission=self.permission)
        return role

    def _member(self, email, role):
        user = self._user(email)
        user.company = self.company
        user.save(update_fields=["company"])
        if role is not None:
            UserRole.objects.create(user=user, role=role, company=self.company, assigned_by=self.admin)
        return user

    def _snapshot(self):
        return (
            list(UserRole.objects.order_by("id").values_list("id", "user_id", "role_id")),
            list(Role.objects.order_by("id").values_list("id", "name", "type", "scope", "is_active")),
            list(RolePermission.objects.order_by("id").values_list("id", "role_id", "permission_id")),
        )

    def assert_refused_and_unchanged(self, method, url, payload=None):
        before = self._snapshot()
        response = getattr(self.client, method)(url, payload or {}, format="json")
        self.assertEqual(response.status_code, 405, f"{method.upper()} {url} -> {response.status_code}")
        self.assertEqual(self._snapshot(), before)

    # --- user-role: writes are refused ----------------------------------------------------

    def test_an_ordinary_user_cannot_create_a_role_assignment(self):
        self.assert_refused_and_unchanged(
            "post", USER_ROLE_URL, {"user": self.colleague.id, "role": self.privileged_role_def.id}
        )

    def test_an_ordinary_user_cannot_modify_an_assignment(self):
        url = f"{USER_ROLE_URL}{self.own_assignment.id}/"
        payload = {"user": self.user.id, "role": self.privileged_role_def.id}
        self.assert_refused_and_unchanged("put", url, payload)
        self.assert_refused_and_unchanged("patch", url, {"role": self.privileged_role_def.id})
        self.own_assignment.refresh_from_db()
        self.assertEqual(self.own_assignment.role_id, self.user_role_def.id)

    def test_an_ordinary_user_cannot_delete_an_assignment(self):
        self.assert_refused_and_unchanged("delete", f"{USER_ROLE_URL}{self.own_assignment.id}/")
        self.assertTrue(UserRole.objects.filter(id=self.own_assignment.id).exists())

    def test_an_ordinary_user_cannot_assign_an_admin_server_role_to_self_or_a_colleague(self):
        self.assert_refused_and_unchanged(
            "post", USER_ROLE_URL, {"user": self.user.id, "role": self.admin_server_role_def.id}
        )
        self.assert_refused_and_unchanged(
            "post", USER_ROLE_URL, {"user": self.colleague.id, "role": self.admin_server_role_def.id}
        )
        self.assertFalse(UserRole.objects.filter(role__type="admin_server").exists())

    # --- user-role: reads still work -------------------------------------------------------

    def test_a_user_still_reads_only_their_own_assignments(self):
        response = self.client.get(USER_ROLE_URL)
        self.assertEqual(response.status_code, 200)
        results = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual([item["id"] for item in results], [self.own_assignment.id])

        detail = self.client.get(f"{USER_ROLE_URL}{self.own_assignment.id}/")
        self.assertEqual(detail.status_code, 200)
        other = UserRole.objects.get(user=self.other_member)
        self.assertEqual(self.client.get(f"{USER_ROLE_URL}{other.id}/").status_code, 404)

    # --- role: writes are refused, reads still work -----------------------------------------

    def test_role_writes_are_refused_and_nothing_is_saved(self):
        self.assert_refused_and_unchanged(
            "post", ROLE_URL,
            {"name": "Injected", "type": "admin_server", "scope": "company",
             "permissions_json": {"contact_management": {"view": True}}},
        )
        url = f"{ROLE_URL}{self.user_role_def.id}/"
        self.assert_refused_and_unchanged("put", url, {"name": "Renamed", "type": "solution", "scope": "company"})
        self.assert_refused_and_unchanged("patch", url, {"permissions_json": {"contact_management": {"view": False}}})
        self.assert_refused_and_unchanged("delete", url)
        self.user_role_def.refresh_from_db()
        self.assertEqual(self.user_role_def.name, "Ordinary")

    def test_role_reads_still_work(self):
        listing = self.client.get(ROLE_URL)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(self.client.get(f"{ROLE_URL}{self.user_role_def.id}/").status_code, 200)
        template = self.client.get(f"{ROLE_URL}get-permissions-template/", {"type": "solution", "scope": "company"})
        self.assertEqual(template.status_code, 200)
