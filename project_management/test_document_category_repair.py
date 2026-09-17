from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from companyprofileapis.serializers.company import CompanyConfigSerializer
from models import constants
from models.models import GlobalConfig
from project_management.management.commands.repair_document_categories import (
    normalized_document_categories,
)
from project_management.serializers.ecpmp_config import ECPMPCONFIGSerializer


class DocumentCategoryValidationTests(TestCase):
    def test_company_configuration_rejects_missing_defaults(self):
        serializer = CompanyConfigSerializer(
            data={"document_categories": ["Custom:CUS"]},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("document_categories", serializer.errors)

    def test_project_configuration_rejects_missing_defaults(self):
        serializer = ECPMPCONFIGSerializer(
            data={"document_categories": ["Custom:CUS"]},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("document_categories", serializer.errors)


class DocumentCategoryRepairTests(TestCase):
    def test_normalization_restores_defaults_and_preserves_custom_values(self):
        result = normalized_document_categories(
            ["General:GEN", "Approved:A", "Approved:A"]
        )

        self.assertEqual(result[:6], constants.GC_DOCUMENT_CATEGORIES)
        self.assertEqual(result[6:], ["Approved:A"])

    def test_command_is_dry_run_by_default_and_idempotent_after_apply(self):
        configuration = GlobalConfig.objects.create(
            document_categories=["General:GEN", "Approved:A"]
        )

        call_command("repair_document_categories", stdout=StringIO())
        configuration.refresh_from_db()
        self.assertEqual(
            configuration.document_categories,
            ["General:GEN", "Approved:A"],
        )

        call_command("repair_document_categories", "--apply", stdout=StringIO())
        configuration.refresh_from_db()
        self.assertEqual(
            configuration.document_categories,
            [*constants.GC_DOCUMENT_CATEGORIES, "Approved:A"],
        )

        output = StringIO()
        call_command("repair_document_categories", stdout=output)
        self.assertIn("Would repair 0 total row(s).", output.getvalue())
