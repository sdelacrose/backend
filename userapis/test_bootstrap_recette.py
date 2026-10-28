"""LINKS-RECETTE-05AM: the bootstrap_recette command. Run with DJANGO_SETTINGS_MODULE=ecpmpapp.settings.recette
on a throw-away database (the licence tables come from ecpmpapp.recette_apps)."""
import json
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication

from ecpmpapp.custom_exceptions import InactiveLicenseException
from ecpmpapp.permissions import BaseModulePermission, LicensePermission
from models.models import (
    CompanyConfig, CompanyProfile, Permission, Role, RolePermission, Staff, User, UserRole,
)
from package_license.models import License, Package, Product
from userapis.management.commands.bootstrap_recette import CATALOGUE_PATH

ARGS = dict(email="first@example.invalid", auth0_sub="auth0|recette-first", first_name="First", last_name="Tester")
MODELS = (User, CompanyProfile, CompanyConfig, Staff, Permission, Role, RolePermission, UserRole, Product, Package, License)


def run(**overrides):
    call_command("bootstrap_recette", stdout=StringIO(), **{**ARGS, **overrides})


def counts():
    return {model.__name__: model.objects.count() for model in MODELS}


@override_settings(RECETTE_MODE=True)
class BootstrapRecetteTests(TestCase):
    def licence_ok(self, user):
        request = SimpleNamespace(user=User.objects.get(pk=user.pk), query_params={})
        return LicensePermission().has_permission(request, None)

    def test_first_run_creates_everything(self):
        run()
        user = User.objects.get(auth0_sub=ARGS["auth0_sub"])
        company = CompanyProfile.objects.get(admin_user=user)
        self.assertEqual((user.company_id, company.company_name), (company.id, "Sharemundo Recette"))
        self.assertTrue(user.is_active and not user.is_deleted)
        self.assertFalse(user.is_superuser or user.is_staff)
        self.assertFalse(user.has_usable_password())
        self.assertTrue(CompanyConfig.objects.filter(company_profile=company, solution="Energy Consultant Solution").exists())
        config = CompanyConfig.objects.get(company_profile=company, solution="Energy Consultant Solution")
        self.assertTrue(config.document_categories and config.document_status)
        from project_management.models import Folder
        self.assertEqual(Folder.objects.filter(company_profile=company, name=f"{company.id}_company_root_folder", parent=None).count(), 1)
        self.assertTrue(Staff.objects.filter(user=user).exists())
        self.assertEqual(Role.objects.filter(company=company).count(), 3)
        self.assertEqual(UserRole.objects.filter(user=user).count(), 2)  # the project role is assigned per project
        package = Package.objects.get(company_profile=company, user=user)
        self.assertGreater(package.expires_at, timezone.now() + timedelta(days=360))
        self.assertTrue(License.objects.get(package=package, user=user).is_active)

    def test_second_run_creates_no_duplicate(self):
        run()
        before = counts()
        run()
        self.assertEqual(counts(), before)

    def test_active_licence_is_accepted(self):
        run()
        self.assertTrue(self.licence_ok(User.objects.get(auth0_sub=ARGS["auth0_sub"])))

    def test_expired_licence_is_refused(self):
        run()
        Package.objects.update(expires_at=timezone.now() - timedelta(hours=1))
        with self.assertRaises(InactiveLicenseException):
            self.licence_ok(User.objects.get(auth0_sub=ARGS["auth0_sub"]))

    def test_revoked_licence_or_package_is_refused(self):
        run()
        user = User.objects.get(auth0_sub=ARGS["auth0_sub"])
        License.objects.update(is_active=False)
        with self.assertRaises(InactiveLicenseException):
            self.licence_ok(user)
        License.objects.update(is_active=True)
        Package.objects.update(is_active=False)
        with self.assertRaises(InactiveLicenseException):
            self.licence_ok(user)

    def test_revoked_or_expired_licence_is_neither_revived_nor_extended(self):
        run()
        License.objects.update(is_active=False)
        with self.assertRaises(CommandError):
            run()
        self.assertFalse(License.objects.get().is_active)
        License.objects.update(is_active=True)
        expired = timezone.now() - timedelta(days=1)
        Package.objects.update(expires_at=expired)
        with self.assertRaises(CommandError):
            run()
        self.assertEqual(Package.objects.get().expires_at, expired)

    def test_inactive_or_deleted_user_is_refused(self):
        run()
        user = User.objects.get(auth0_sub=ARGS["auth0_sub"])
        User.objects.filter(pk=user.pk).update(is_active=False)
        with self.assertRaises(CommandError):
            run()
        self.assertFalse(User.objects.get(pk=user.pk).is_active)
        with self.assertRaises(AuthenticationFailed):  # the HTTP authentication refuses an inactive user
            JWTAuthentication().get_user({"sub": ARGS["auth0_sub"]})
        User.objects.filter(pk=user.pk).update(is_active=True, is_deleted=True)
        with self.assertRaises(CommandError):
            run()
        self.assertTrue(User.objects.get(pk=user.pk).is_deleted)

    def test_admin_role_gives_business_permissions(self):
        run()
        user = User.objects.get(auth0_sub=ARGS["auth0_sub"])
        for module, action in (("documents", "full_access"), ("contact_management", "create"),
                               ("company_configurations", "view"), ("documents", "correspondence_send"),
                               ("aas_models", "view")):
            self.assertTrue(
                BaseModulePermission(module, action, "company")._check_permission(user, module, action, "company", {}),
                f"{module}.{action}",
            )
        self.assertFalse(BaseModulePermission("documents", "nope", "company")._check_permission(
            user, "documents", "nope", "company", {}))
        project_role = Role.objects.get(company=user.company, scope="project")
        self.assertTrue(project_role.role_permissions.filter(permission__key="project.view").exists())

    def test_user_of_another_company_is_refused(self):
        run(company_name="Company A")
        before = counts()
        with self.assertRaises(CommandError):
            run(company_name="Company B")
        self.assertEqual(counts(), before)
        other = User.objects.create(email="other@example.invalid", last_name="Other", auth0_sub="auth0|other")
        CompanyProfile.objects.create(
            admin_user=other, company_name="Company C", company_address="-", company_town="-", company_tell_no="-",
            company_mob_no="-", company_web_address="-", company_email_address="-", company_tax_reference="-",
            company_iban="-",
        )
        before = counts()
        with self.assertRaises(CommandError):  # same name, another owner
            run(email="second@example.invalid", auth0_sub="auth0|second", company_name="Company C")
        self.assertEqual(counts(), before)

    def test_inconsistent_identity_is_refused(self):
        run()
        before = counts()
        for overrides in ({"auth0_sub": "auth0|someone-else"}, {"email": "another@example.invalid"}):
            with self.assertRaises(CommandError):
                run(**overrides)
        self.assertEqual(counts(), before)

    def test_invalid_arguments_write_nothing(self):
        before = counts()
        for overrides in ({"auth0_sub": ""}, {"auth0_sub": "no-pipe"}, {"auth0_sub": "a|b c"},
                          {"email": "not-an-email"}, {"first_name": ""}, {"last_name": "x" * 31}, {"company_name": " "}):
            with self.assertRaises(CommandError):
                run(**overrides)
        self.assertEqual(counts(), before)

    @override_settings(RECETTE_MODE=False)
    def test_refused_outside_recette_without_writing(self):
        before = counts()
        with self.assertRaises(CommandError):
            run()
        self.assertEqual(counts(), before)

    def test_failure_rolls_everything_back(self):
        before = counts()
        with mock.patch("userapis.management.commands.bootstrap_recette.Command.ensure_licence",
                        side_effect=RuntimeError("boom")), self.assertRaises(RuntimeError):
            run()
        self.assertEqual(counts(), before)

    def test_catalogue_is_consistent(self):
        catalogue = json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))
        self.assertTrue(catalogue["version"])
        known = set(catalogue["permissions"])
        for spec in catalogue["roles"].values():
            self.assertFalse(set(spec["permissions"]) - known, spec["name"])
        for key, spec in catalogue["permissions"].items():
            self.assertEqual(key.split(".", 1)[0], spec["module"], key)
