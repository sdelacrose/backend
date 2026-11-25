"""RELEASE-02B: the bootstrap_recette_hr command. Run with DJANGO_SETTINGS_MODULE=ecpmpapp.settings.recette
on a throw-away database (the licence tables come from ecpmpapp.recette_apps)."""
import json
from io import StringIO
from types import SimpleNamespace

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from ecpmpapp.permissions import LicensePermission
from models.models import CompanyProfile, Role, RolePermission, Staff, User, UserRole
from package_license.models import License
from userapis.management.commands.bootstrap_recette_hr import CATALOGUE_PATH

try:  # present once the HR lot (backend PR #8) is integrated
    from human_resource_management import permissions as hr_permissions
except ImportError:  # pragma: no cover
    hr_permissions = None

OWNER = dict(email="first@example.invalid", auth0_sub="auth0|recette-first", first_name="First", last_name="Tester")
HR = dict(email="rh@example.invalid", auth0_sub="auth0|recette-rh", first_name="Rh", last_name="Tester")
SST_KEYS = {
    "staff_management.manage_sst_cases", "staff_management.view_sst_dossier",
    "staff_management.view_sst_payroll_projection", "staff_management.manage_sst_payroll_attestation",
}


def run_hr(**overrides):
    call_command("bootstrap_recette_hr", stdout=StringIO(), **{**HR, **overrides})


def keys(user):
    roles = UserRole.objects.filter(user=user).values("role")
    return set(RolePermission.objects.filter(role__in=roles).values_list("permission__key", flat=True))


@override_settings(RECETTE_MODE=True)
class BootstrapRecetteHrTests(TestCase):
    def setUp(self):
        call_command("bootstrap_recette", stdout=StringIO(), **OWNER)
        self.owner = User.objects.get(auth0_sub=OWNER["auth0_sub"])
        self.company = CompanyProfile.objects.get(admin_user=self.owner)
        self.owner_keys_before = keys(self.owner)

    def test_catalogue_is_versioned_and_sst_is_separate(self):
        catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        hr, sst = catalogue["roles"]["hr"], catalogue["roles"]["sst"]
        self.assertTrue(catalogue["version"])
        self.assertEqual(set(sst["permissions"]), SST_KEYS)
        self.assertFalse(SST_KEYS & set(hr["permissions"]))
        self.assertFalse({k for k in hr["permissions"] if k.endswith(".full_access")})
        self.assertFalse({k for k in hr["permissions"] if k.startswith("company_configurations.")})
        self.assertTrue(set(hr["permissions"]) | SST_KEYS <= set(catalogue["permissions"]))

    def test_hr_role_only_by_default(self):
        run_hr()
        user = User.objects.get(auth0_sub=HR["auth0_sub"])
        self.assertEqual(user.company_id, self.company.id)
        self.assertFalse(user.is_superuser or user.is_staff or user.has_usable_password())
        self.assertTrue(Staff.objects.filter(user=user).exists())
        roles = list(UserRole.objects.filter(user=user).order_by("id").values_list("role__name", flat=True))
        self.assertEqual(roles, ["Responsable RH — Recette"])
        self.assertIn("staff_management.view_compensation", keys(user))
        self.assertFalse(SST_KEYS & keys(user))
        self.assertTrue(License.objects.get(user=user).is_active)
        request = SimpleNamespace(user=user, query_params={})
        self.assertTrue(LicensePermission().has_permission(request, None))

    def test_with_sst_adds_separate_role_after_hr_role(self):
        run_hr(with_sst=True)
        user = User.objects.get(auth0_sub=HR["auth0_sub"])
        roles = list(UserRole.objects.filter(user=user).order_by("id").values_list("role__name", flat=True))
        self.assertEqual(roles, ["Responsable RH — Recette", "Référent SST — Recette"])
        self.assertTrue(SST_KEYS <= keys(user))
        sst_role = Role.objects.get(company=self.company, name="Référent SST — Recette")
        self.assertEqual(set(sst_role.role_permissions.values_list("permission__key", flat=True)), SST_KEYS)

    def test_owner_is_never_given_hr_or_sst_rights(self):
        run_hr(with_sst=True)
        self.assertEqual(keys(self.owner), self.owner_keys_before)
        self.assertFalse(SST_KEYS & keys(self.owner))
        with self.assertRaisesMessage(CommandError, "never assigned to the company owner"):
            call_command("bootstrap_recette_hr", stdout=StringIO(), with_sst=True, **OWNER)
        self.assertEqual(keys(self.owner), self.owner_keys_before)

    def test_idempotent(self):
        run_hr(with_sst=True)
        counts = (Role.objects.count(), RolePermission.objects.count(), UserRole.objects.count(), License.objects.count())
        run_hr(with_sst=True)
        self.assertEqual(
            counts, (Role.objects.count(), RolePermission.objects.count(), UserRole.objects.count(), License.objects.count())
        )

    def test_refusals_change_nothing(self):
        before = (User.objects.count(), UserRole.objects.count())
        with self.assertRaisesMessage(CommandError, "Recette company not found"):
            run_hr(company_name="Unknown company")
        other_admin = User.objects.create(email="other-admin@example.invalid", first_name="O", last_name="A",
                                          user_type="company_user", auth0_sub="auth0|other-admin")
        other = CompanyProfile.objects.create(
            admin_user=other_admin, company_name="Other", company_address="1", company_town="T", company_tell_no="1",
            company_mob_no="1", company_web_address="https://o.invalid", company_email_address="o@o.invalid",
            company_tax_reference="t", company_iban="i",
        )
        User.objects.create(email="x@example.invalid", first_name="X", last_name="Y", user_type="company_user",
                            auth0_sub="auth0|x", company=other)
        with self.assertRaisesMessage(CommandError, "belongs to another company"):
            run_hr(email="x@example.invalid", auth0_sub="auth0|x")
        self.assertEqual(before[1], UserRole.objects.count())

    @override_settings(RECETTE_MODE=False)
    def test_refuses_outside_recette(self):
        with self.assertRaisesMessage(CommandError, "RECETTE_MODE"):
            run_hr()

    def test_backend_hr_checks_follow_the_roles(self):
        if hr_permissions is None or not hasattr(hr_permissions, "has_sst_manage_permission"):
            self.skipTest("HR lot not integrated in this branch")
        run_hr()
        user = User.objects.get(auth0_sub=HR["auth0_sub"])
        self.assertTrue(hr_permissions.has_compensation_view_permission(user))
        self.assertTrue(hr_permissions.has_cumulative_leave_review_permission(user))
        self.assertTrue(hr_permissions.has_staff_management_view_permission(user))
        self.assertFalse(hr_permissions.has_sst_manage_permission(user))
        self.assertFalse(hr_permissions.has_sst_view_permission(user))
        self.assertFalse(hr_permissions.has_sst_manage_permission(self.owner))
        run_hr(with_sst=True)
        self.assertTrue(hr_permissions.has_sst_manage_permission(user))
        self.assertTrue(hr_permissions.has_sst_payroll_attestation_permission(user))
        self.assertFalse(hr_permissions.has_sst_manage_permission(self.owner))
