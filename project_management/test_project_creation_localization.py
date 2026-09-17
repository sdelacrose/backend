from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import TestCase
from rest_framework.test import APIRequestFactory

from models.constants import GC_PROJECT_DOCUMENT_TYPES as SHARED_PROJECT_DOCUMENT_TYPES
from models.models import CompanyConfig, User
from project_management.models import Project
from project_management.models.constants import (
    CUSTOMER_DEMAND_DOCUMENT_TYPE,
    GC_PROJECT_DOCUMENT_TYPES,
)
from project_management.serializers.project import ProjectSerializer
from project_management.signals import notify_on_project_save


class ProjectCreationLocalizationTests(TestCase):
    def setUp(self):
        notification_preference_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_preference_patcher.start()
        self.addCleanup(notification_preference_patcher.stop)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(
            post_save.connect,
            notify_on_project_save,
            sender=Project,
        )
        self.user = User.objects.create_user(
            email="localized-projects@example.com",
            password="password",
            first_name="Localized",
            last_name="Creator",
            user_type="freelancer",
            preferred_language="French",
        )
        CompanyConfig.objects.create(user=self.user)
        self.factory = APIRequestFactory()

    def create_project(self, project_type, locale=None):
        request = self.factory.post("/projects/")
        request.user = self.user
        data = {
            "project_name": f"{project_type} project",
            "project_type": project_type,
            "contract_value": "1000.00",
            "project_start_date": "2026-08-01",
            "project_end_date": "2027-08-01",
            "user": self.user.id,
        }
        if locale is not None:
            data["locale"] = locale

        serializer = ProjectSerializer(
            data=data,
            context={"request": request, "user": self.user},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        return serializer.save()

    def test_explicit_locale_overrides_creator_preference(self):
        project = self.create_project("Angebot", locale="en")
        first_phase = project.milestones.order_by("sort_order", "id").first()
        first_action = first_phase.action_items.order_by("id").first()

        self.assertEqual(first_phase.name, "Inquiry")
        self.assertEqual(first_action.name, "Inquiry owner assigned")

    def test_missing_locale_uses_creator_preference(self):
        project = self.create_project("Angebot")
        first_phase = project.milestones.order_by("sort_order", "id").first()
        first_action = first_phase.action_items.order_by("id").first()

        self.assertEqual(first_phase.name, "Demande initiale")
        self.assertEqual(
            first_action.name,
            "Responsable de la demande assigné",
        )

    def test_default_phase_uses_creation_locale(self):
        project = self.create_project("Other Project", locale="de")

        self.assertEqual(
            project.milestones.get().name,
            "Standardprojektphase",
        )

    def test_locale_is_write_only(self):
        serializer = ProjectSerializer()

        self.assertTrue(serializer.fields["locale"].write_only)

    def test_new_project_configuration_includes_customer_demand_document_type(self):
        self.assertIn(
            CUSTOMER_DEMAND_DOCUMENT_TYPE,
            GC_PROJECT_DOCUMENT_TYPES,
        )
        self.assertIn(
            CUSTOMER_DEMAND_DOCUMENT_TYPE,
            SHARED_PROJECT_DOCUMENT_TYPES,
        )
        self.assertLess(
            SHARED_PROJECT_DOCUMENT_TYPES.index(CUSTOMER_DEMAND_DOCUMENT_TYPE),
            SHARED_PROJECT_DOCUMENT_TYPES.index("Other:OTH"),
        )
