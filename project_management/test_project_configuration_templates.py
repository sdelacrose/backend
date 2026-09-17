from django.db.models.signals import post_save
from django.test import TestCase

from models import constants
from models.configuration_templates import get_project_template_defaults
from models.models import CompanyConfig, CompanyProfile, GlobalConfig, ProjectConfigTemplate, User
from project_management.models import Project
from project_management.services.project_configuration_templates import (
    create_project_configuration,
    ensure_project_config_template,
)
from project_management.serializers.time_expense import (
    get_project_owner_valid_choices,
)
from project_management.signals import create_default_folders, notify_on_project_save


class ProjectConfigurationTemplateInitializationTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)

        self.user = User.objects.create_user(
            email="project-config-template@example.com",
            password="password",
            first_name="Project",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Project Config Template Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address=self.user.email,
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])

    def _project(self, name, solution="Energy Consultant Solution"):
        return Project.objects.create(
            project_name=name,
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
            solution=solution,
        )

    def test_new_project_configuration_uses_project_template_not_company_config(self):
        template_values = get_project_template_defaults()
        template_values["labels"] = ["Project template label"]
        template_values["document_types"] = [
            *constants.GC_PROJECT_DOCUMENT_TYPES,
            "Custom project document:CPD",
        ]
        ProjectConfigTemplate.objects.create(
            company_profile=self.company,
            **template_values,
        )
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            labels=["Company-only label"],
        )
        project = self._project("Independent project template")

        configuration = create_project_configuration(project)

        self.assertEqual(configuration.labels, ["Project template label"])
        self.assertIn("Custom project document:CPD", configuration.document_types)
        self.assertNotIn("Company-only label", configuration.labels)
        self.assertEqual(configuration.rooms, constants.GC_ROOMS)

    def test_missing_template_is_repaired_from_global_config(self):
        GlobalConfig.objects.create(
            company_profile=self.company,
            labels=["Custom project label"],
        )
        project = self._project("Repair project template")

        template = ensure_project_config_template(project)

        self.assertEqual(template.labels, ["Custom project label"])
        self.assertEqual(template.document_types, constants.GC_PROJECT_DOCUMENT_TYPES)

    def test_one_template_initializes_projects_for_each_solution(self):
        values = get_project_template_defaults()
        values["labels"] = ["Shared project template"]
        ProjectConfigTemplate.objects.create(
            company_profile=self.company,
            **values,
        )

        for solution, _label in constants.SHAREMUNDO_SOLUTION_TYPES:
            with self.subTest(solution=solution):
                project = self._project(f"Project {solution}", solution)
                configuration = create_project_configuration(project)
                self.assertEqual(
                    configuration.labels,
                    ["Shared project template"],
                )

    def test_project_billing_choices_use_company_configuration(self):
        CompanyConfig.objects.create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            invoice_status=["open", "paid"],
            expense_types=["Travel", "Meals"],
        )
        project = self._project("Company-owned billing configuration")

        self.assertCountEqual(
            get_project_owner_valid_choices("invoice_status", project),
            ["open", "paid"],
        )
        self.assertCountEqual(
            get_project_owner_valid_choices("expense_types", project),
            ["Travel", "Meals"],
        )
