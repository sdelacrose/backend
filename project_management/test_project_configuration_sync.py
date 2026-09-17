from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework.permissions import AllowAny
from rest_framework.test import APIRequestFactory, force_authenticate

from models.configuration_sync import PRESERVE_LOCAL, REPLACE
from models.configuration_templates import get_project_template_defaults
from models.models import CompanyProfile, ProjectConfigTemplate, User
from project_management.models import ECPMPConfig, Project
from project_management.services.project_configuration_templates import (
    build_project_configuration_values,
)
from project_management.signals import create_default_folders, notify_on_project_save
from project_management.views.ecpmp_config import ECPMPConfigViewSet


class ProjectConfigurationSyncTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)

        self.user = User.objects.create_user(
            email="project-sync@example.com",
            password="password",
            first_name="Project",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Project Sync Company",
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

        template_values = get_project_template_defaults()
        template_values["labels"] = ["Admin label"]
        self.template = ProjectConfigTemplate.objects.create(
            company_profile=self.company,
            **template_values,
        )
        self.project = Project.objects.create(
            project_name="Project configuration sync",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
            solution="Energy Consultant Solution",
        )
        config_values = build_project_configuration_values(self.project)
        config_values["labels"] = ["Local label"]
        self.config = ECPMPConfig.objects.create(
            project=self.project,
            m365_tenant_id="local-tenant",
            **config_values,
        )
        self.factory = APIRequestFactory()

    def _post_action(self, action, data=None):
        request = self.factory.post(
            f"/api/project_management/ecpmpconfig/{self.config.pk}/{action}/",
            data or {},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = ECPMPConfigViewSet.as_view({"post": action})
        with patch.object(
            ECPMPConfigViewSet,
            "get_permissions",
            return_value=[AllowAny()],
        ):
            return view(request, pk=self.config.pk)

    def test_preserve_sync_keeps_local_values_and_appends_admin_values(self):
        response = self._post_action(
            "sync_with_admin",
            {"strategy": PRESERVE_LOCAL},
        )

        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.labels, ["Local label", "Admin label"])
        self.assertEqual(self.config.m365_tenant_id, "local-tenant")

    def test_replace_sync_replaces_syncable_values_only(self):
        response = self._post_action(
            "sync_with_admin",
            {"strategy": REPLACE},
        )

        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.labels, ["Admin label"])
        self.assertEqual(self.config.m365_tenant_id, "local-tenant")

    def test_reset_restores_canonical_defaults(self):
        response = self._post_action("reset_config")

        self.assertEqual(response.status_code, 200)
        self.config.refresh_from_db()
        self.assertEqual(self.config.labels, [])
        self.assertEqual(self.config.m365_tenant_id, "local-tenant")

    def test_sync_rejects_unknown_strategy(self):
        response = self._post_action(
            "sync_with_admin",
            {"strategy": "unknown"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("strategy", response.data)
