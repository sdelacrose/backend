from io import StringIO
import json
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from models.configuration_templates import get_company_configuration_defaults
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import Folder


@override_settings(RECETTE_MODE=True)
class RecetteDocumentRepairTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(email="repair@example.invalid", last_name="Repair")
        self.company = CompanyProfile.objects.create(admin_user=self.user, company_name="Repair test")
        self.config = CompanyConfig.objects.create(company_profile=self.company, solution="Energy Consultant Solution")

    def run_repair(self, **kwargs):
        output = StringIO()
        call_command("repair_recette_documents", company_id=self.company.pk,
                     company_name=self.company.company_name, stdout=output, **kwargs)
        lines = output.getvalue().splitlines()
        return json.loads(lines[0]), lines[1].split("=", 1)[1]

    def test_preview_does_not_write_and_shows_canonical_values(self):
        plan, _ = self.run_repair()
        self.assertTrue(plan["create_root"])
        defaults = get_company_configuration_defaults()
        for field, change in plan["configuration_changes"].items():
            self.assertEqual(change["after"], defaults[field])
        self.assertFalse(Folder.objects.filter(company_profile=self.company).exists())
        self.config.refresh_from_db()
        self.assertEqual(self.config.document_categories, [])

    def test_apply_requires_exact_reviewed_plan(self):
        for digest in (None, "wrong"):
            with self.assertRaises(CommandError):
                self.run_repair(apply=True, expected_plan_sha256=digest)
        self.assertFalse(Folder.objects.filter(company_profile=self.company).exists())

    def test_apply_and_second_execution_are_idempotent(self):
        _, digest = self.run_repair()
        self.run_repair(apply=True, expected_plan_sha256=digest)
        self.assertEqual(Folder.objects.filter(company_profile=self.company, parent=None).count(), 1)
        self.config.refresh_from_db()
        self.assertTrue(self.config.document_status)
        plan, digest = self.run_repair()
        self.assertFalse(plan["create_root"])
        self.assertEqual(plan["configuration_changes"], {})
        self.run_repair(apply=True, expected_plan_sha256=digest)
        self.assertEqual(Folder.objects.filter(company_profile=self.company).count(), 1)

    def test_changed_data_invalidates_preview(self):
        _, digest = self.run_repair()
        self.config.document_categories = ["Custom:CUS"]
        self.config.save(update_fields=["document_categories"])
        with self.assertRaises(CommandError):
            self.run_repair(apply=True, expected_plan_sha256=digest)
        self.assertFalse(Folder.objects.filter(company_profile=self.company).exists())

    def test_custom_lists_and_other_companies_are_preserved(self):
        self.config.company_roles = ["Custom role"]
        self.config.document_categories = ["Custom:CUS"]
        self.config.save()
        other_user = User.objects.create(email="other@example.invalid", last_name="Other")
        other = CompanyProfile.objects.create(company_name="Other", admin_user=other_user)
        other_config = CompanyConfig.objects.create(company_profile=other, solution="Energy Consultant Solution")
        _, digest = self.run_repair()
        self.run_repair(apply=True, expected_plan_sha256=digest)
        self.config.refresh_from_db()
        other_config.refresh_from_db()
        self.assertEqual(self.config.company_roles, ["Custom role"])
        self.assertEqual(self.config.document_categories, ["Custom:CUS"])
        self.assertEqual(other_config.document_status, [])
        self.assertFalse(Folder.objects.filter(company_profile=other).exists())

    @override_settings(RECETTE_MODE=False)
    def test_refused_outside_recette(self):
        with self.assertRaises(CommandError):
            self.run_repair()

    def test_company_identity_must_match(self):
        with self.assertRaises(CommandError):
            call_command("repair_recette_documents", company_id=self.company.pk, company_name="Wrong")

    def test_failure_rolls_back_root_creation(self):
        _, digest = self.run_repair()
        with mock.patch.object(CompanyConfig, "save", side_effect=RuntimeError("test rollback")):
            with self.assertRaises(RuntimeError):
                self.run_repair(apply=True, expected_plan_sha256=digest)
        self.assertFalse(Folder.objects.filter(company_profile=self.company).exists())
