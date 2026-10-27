"""LINKS-RECETTE-05AL: package_license (admin-server-backend @ c27b6ab, models + migrations only) as
installed by ecpmpapp.settings.recette, and its fit with ecpmpapp.permissions.LicensePermission.
Run with DJANGO_SETTINGS_MODULE=ecpmpapp.settings.recette on a throw-away database."""
import socket
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.utils import timezone

from ecpmpapp.custom_exceptions import InactiveLicenseException
from ecpmpapp.permissions import LicensePermission
from models.models import User
from package_license.models import License, Package, Product

SETTINGS_DIR = Path(settings.BASE_DIR) / "ecpmpapp" / "settings"


def _no_network(*args, **kwargs):
    raise AssertionError("no network access is expected from the licence tables")


class RecettePackageLicenseTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="lic@example.invalid", last_name="Lic", auth0_sub="auth0|lic")
        self.product = Product.objects.create(name="Energy Consultant Solution", price=0)
        for target in ("socket.create_connection", "socket.socket.connect"):
            patcher = mock.patch(target, _no_network)
            patcher.start()
            self.addCleanup(patcher.stop)

    def grant(self, *, package_active=True, license_active=True, expires_in=timedelta(days=30), **package_extra):
        package = Package.objects.create(
            is_active=package_active,
            expires_at=timezone.now() + expires_in,
            status="Cancelled",
            is_freetrial=True,
            **package_extra,
        )
        return License.objects.create(
            package=package, product=self.product, user=self.user, is_active=license_active,
            license_assigned_at=timezone.now(),
        )

    def check(self):
        request = SimpleNamespace(user=self.user, query_params={})
        return LicensePermission().has_permission(request, None)

    def test_app_and_tables(self):
        self.assertEqual(apps.get_app_config("package_license").label, "package_license")
        tables = set(connection.introspection.table_names())
        self.assertTrue({"package_license_license", "package_license_product", "package_license_package"} <= tables)

    def test_the_22_migrations_are_applied_and_match_the_models(self):
        applied = MigrationRecorder.Migration.objects.filter(app="package_license")
        self.assertEqual(applied.count(), 22)
        call_command("makemigrations", "package_license", "--check", "--dry-run", verbosity=0)

    def test_active_license_is_accepted(self):
        self.grant()
        self.assertTrue(self.check())

    def test_no_license_is_refused(self):
        with self.assertRaises(InactiveLicenseException):
            self.check()

    def test_expired_license_is_refused(self):
        self.grant(expires_in=timedelta(hours=-1))
        with self.assertRaises(InactiveLicenseException):
            self.check()

    def test_deactivated_license_is_refused(self):
        self.grant(license_active=False)
        with self.assertRaises(InactiveLicenseException):
            self.check()

    def test_deactivated_package_is_refused(self):
        self.grant(package_active=False)
        with self.assertRaises(InactiveLicenseException):
            self.check()

    def test_back_office_code_is_not_loaded(self):
        for module in ("admin_center", "accounts", "stripe"):
            self.assertNotIn(module, sys.modules)
        self.assertFalse((Path(settings.BASE_DIR) / "package_license" / "signals.py").exists())

    def test_only_the_recette_settings_enable_the_app(self):
        enabled_by = sorted(
            path.name for path in SETTINGS_DIR.glob("*.py")
            if "recette_apps" in path.read_text(encoding="utf-8") and path.name != "recette.py"
        )
        self.assertEqual(enabled_by, [])
        self.assertIn("ecpmpapp.recette_apps.RecettePackageLicenseConfig", settings.INSTALLED_APPS)
