from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from companyprofileapis.services.configuration_visibility import (
    ProjectConfigurationVisibilityError,
    get_project_configuration_value_repairs,
    repair_project_configuration_values,
    validate_project_configuration_visibility,
)
from companyprofileapis.serializers.company import CompanyConfigSerializer
from models import constants
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import ECPMPConfig, Project


class ProjectConfigurationVisibilityTests(TestCase):
    def setUp(self):
        self.project = Project(
            project_name="Visibility project",
            contract_value=1000,
            contract_type="Fixed Price",
            commercial_model="Target Cost",
        )
        Project.objects.bulk_create([self.project])
        self.configuration, _ = ECPMPConfig.objects.get_or_create(
            project=self.project
        )

    def test_used_project_value_cannot_be_newly_disabled(self):
        with self.assertRaises(ProjectConfigurationVisibilityError) as context:
            validate_project_configuration_visibility(
                self.configuration,
                {},
                {"contract_type": {"disabled_values": ["Fixed Price"]}},
            )

        self.assertEqual(
            context.exception.blocked_values,
            {"contract_type": ["Fixed Price"]},
        )

    def test_unused_project_value_can_be_disabled(self):
        validate_project_configuration_visibility(
            self.configuration,
            {},
            {"contract_type": {"disabled_values": ["Time and Materials"]}},
        )

    def test_already_disabled_value_is_not_revalidated(self):
        visibility = {
            "commercial_model": {"disabled_values": ["Target Cost"]}
        }
        validate_project_configuration_visibility(
            self.configuration,
            visibility,
            visibility,
        )


class CompanyProjectConfigurationValueTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="configuration-values@example.com",
            password="password",
            first_name="Configuration",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Configuration Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="configuration@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.configuration = CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            contract_type=list(constants.GC_CONTRACT_TYPES),
            international_standard=list(constants.GC_INTERNATIONAL_STANDARDS),
            major_project_model=[*constants.GC_MAJOR_PROJECT_MODELS, "Custom"],
            commercial_model=list(constants.GC_COMMERCIAL_MODELS),
            supporting_agreement=list(constants.GC_SUPPORTING_AGREEMENTS),
        )
        self.project = Project(
            project_name="Configured project",
            contract_value=1000,
            company_profile=self.company,
            major_project_model="Custom",
        )
        Project.objects.bulk_create([self.project])

    def test_used_value_cannot_be_removed_from_company_configuration(self):
        serializer = CompanyConfigSerializer(
            self.configuration,
            data={"major_project_model": list(constants.GC_MAJOR_PROJECT_MODELS)},
            partial=True,
        )

        self.assertFalse(serializer.is_valid())
        error = serializer.errors["configuration_visibility"][
            "major_project_model"
        ]
        self.assertEqual(error["code"], "configuration_value_in_use")
        self.assertEqual(error["values"], ["Custom"])

    def test_unused_value_can_be_removed_from_company_configuration(self):
        self.project.major_project_model = None
        self.project.save(update_fields=["major_project_model"])
        serializer = CompanyConfigSerializer(
            self.configuration,
            data={"major_project_model": list(constants.GC_MAJOR_PROJECT_MODELS)},
            partial=True,
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_repair_restores_missing_and_reenables_used_values(self):
        self.configuration.major_project_model = list(
            constants.GC_MAJOR_PROJECT_MODELS
        )
        self.configuration.configuration_visibility = {
            "major_project_model": {"disabled_values": ["Custom"]}
        }
        self.configuration.save(
            update_fields=["major_project_model", "configuration_visibility"]
        )

        missing, hidden, replacements = (
            get_project_configuration_value_repairs(self.configuration)
        )
        self.assertEqual(missing, {"major_project_model": ["Custom"]})
        self.assertEqual(hidden, {"major_project_model": ["Custom"]})
        self.assertEqual(replacements, {})

        repair_project_configuration_values(self.configuration)
        self.configuration.refresh_from_db()

        self.assertIn("Custom", self.configuration.major_project_model)
        self.assertNotIn(
            "major_project_model",
            self.configuration.configuration_visibility,
        )

    def test_repair_replaces_legacy_contract_type_with_other(self):
        self.project.contract_type = "client"
        self.project.save(update_fields=["contract_type"])
        Project.objects.bulk_create(
            [
                Project(
                    project_name=f"Legacy {contract_type}",
                    contract_value=1000,
                    company_profile=self.company,
                    contract_type=contract_type,
                )
                for contract_type in ["authority", "consultant", "contractor"]
            ]
        )

        missing, hidden, replacements = (
            get_project_configuration_value_repairs(self.configuration)
        )

        self.assertEqual(missing, {})
        self.assertEqual(hidden, {})
        self.assertEqual(
            replacements,
            {
                "contract_type": {
                    "authority": "Other",
                    "client": "Other",
                    "consultant": "Other",
                    "contractor": "Other",
                }
            },
        )

        repair_project_configuration_values(self.configuration)
        self.project.refresh_from_db()
        self.configuration.refresh_from_db()

        self.assertEqual(
            Project.objects.filter(
                company_profile=self.company,
                contract_type="Other",
            ).count(),
            4,
        )
        self.assertNotIn("client", self.configuration.contract_type)

    def test_repair_only_considers_projects_from_the_same_solution(self):
        core_configuration = CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Core Solution",
            contract_type=list(constants.GC_CONTRACT_TYPES),
            international_standard=list(constants.GC_INTERNATIONAL_STANDARDS),
            major_project_model=list(constants.GC_MAJOR_PROJECT_MODELS),
            commercial_model=list(constants.GC_COMMERCIAL_MODELS),
            supporting_agreement=list(constants.GC_SUPPORTING_AGREEMENTS),
        )

        missing, hidden, replacements = (
            get_project_configuration_value_repairs(core_configuration)
        )

        self.assertEqual(missing, {})
        self.assertEqual(hidden, {})
        self.assertEqual(replacements, {})

    def test_repair_command_is_dry_run_by_default_and_idempotent(self):
        self.configuration.major_project_model = list(
            constants.GC_MAJOR_PROJECT_MODELS
        )
        self.configuration.save(update_fields=["major_project_model"])

        dry_run_output = StringIO()
        call_command("repair_project_configuration_values", stdout=dry_run_output)
        self.configuration.refresh_from_db()
        self.assertNotIn("Custom", self.configuration.major_project_model)
        self.assertIn("Would repair 1 configuration(s)", dry_run_output.getvalue())

        apply_output = StringIO()
        call_command(
            "repair_project_configuration_values",
            apply=True,
            stdout=apply_output,
        )
        self.configuration.refresh_from_db()
        self.assertIn("Custom", self.configuration.major_project_model)
        self.assertIn("Repaired 1 configuration(s)", apply_output.getvalue())

        second_output = StringIO()
        call_command(
            "repair_project_configuration_values",
            apply=True,
            stdout=second_output,
        )
        self.assertIn("Repaired 0 configuration(s)", second_output.getvalue())
