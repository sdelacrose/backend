from unittest.mock import patch

from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from rest_framework import serializers
from rest_framework.test import APIRequestFactory

from models.models import CompanyProfile, User
from project_management.models import (
    ContactCompany,
    ContactUser,
    Project,
    ProjectExternalUsers,
)
from project_management.signals import (
    create_default_folders,
    notify_on_contact_company_save,
    notify_on_contact_save,
    notify_on_project_save,
)

from project_management.serializers.contact import (
    ContactCompanySerializer,
    ContactUserSerializer,
    resolve_external_contact_role,
)


class ResolveExternalContactRoleTests(SimpleTestCase):
    def test_uses_requested_configured_role(self):
        role = resolve_external_contact_role(
            {"role": "supplier"},
            ["consultant", "supplier"],
        )

        self.assertEqual(role, "supplier")

    def test_preserves_existing_role_when_payload_omits_role(self):
        role = resolve_external_contact_role(
            {},
            ["consultant", "supplier"],
            existing_role="supplier",
        )

        self.assertEqual(role, "supplier")

    def test_uses_configured_consultant_role_for_new_project_link(self):
        role = resolve_external_contact_role(
            {},
            ["project manager", "consultant", "supplier"],
        )

        self.assertEqual(role, "consultant")

    def test_rejects_stale_architect_role(self):
        with self.assertRaises(serializers.ValidationError):
            resolve_external_contact_role(
                {"role": "architect"},
                ["consultant", "supplier"],
            )


class ContactProjectAssignmentAtomicityTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        post_save.disconnect(notify_on_contact_save, sender=ContactUser)
        post_save.disconnect(notify_on_contact_company_save, sender=ContactCompany)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, notify_on_contact_save, sender=ContactUser)
        self.addCleanup(
            post_save.connect,
            notify_on_contact_company_save,
            sender=ContactCompany,
        )

        self.factory = APIRequestFactory()
        self.user = User.objects.create_user(
            email="project-contact-owner@example.com",
            password="password",
            first_name="Project",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Project Contact Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="project-contact-owner@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.projects = [
            Project.objects.create(
                project_name=f"Contact Project {index}",
                contract_value=1000,
                company_profile=self.company,
                created_by=self.user,
            )
            for index in (1, 2)
        ]

    def request(self):
        request = self.factory.post(
            "/api/project_management/contact-user/",
            {},
            format="json",
        )
        request.user = self.user
        return request

    def mixed_projects(self):
        return [
            {"project_id": self.projects[0].id, "role": "supplier"},
            {"project_id": self.projects[1].id, "role": "architect"},
        ]

    @patch(
        "project_management.serializers.contact.get_valid_choices",
        return_value=["consultant", "supplier"],
    )
    def test_user_create_with_invalid_assignment_writes_nothing(self, _choices):
        serializer = ContactUserSerializer(
            data={
                "name": "No",
                "surname": "Partial User",
                "email": "no-partial-user@example.com",
                "projects": self.mixed_projects(),
            },
            context={"request": self.request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(serializers.ValidationError):
            serializer.save()

        self.assertFalse(
            ContactUser.objects.filter(email="no-partial-user@example.com").exists()
        )
        self.assertFalse(ProjectExternalUsers.objects.exists())

    @patch(
        "project_management.serializers.contact.get_valid_choices",
        return_value=["consultant", "supplier"],
    )
    def test_company_create_with_invalid_assignment_writes_nothing(self, _choices):
        serializer = ContactCompanySerializer(
            data={
                "company_name": "No Partial Company",
                "contact_email": "no-partial-company@example.com",
                "projects": self.mixed_projects(),
            },
            context={"request": self.request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(serializers.ValidationError):
            serializer.save()

        self.assertFalse(
            ContactCompany.objects.filter(
                contact_email="no-partial-company@example.com"
            ).exists()
        )
        self.assertFalse(ProjectExternalUsers.objects.exists())

    @patch(
        "project_management.serializers.contact.get_valid_choices",
        return_value=["consultant", "supplier"],
    )
    def test_user_update_with_invalid_assignment_changes_nothing(self, _choices):
        contact = ContactUser.objects.create(
            company_profile=self.company,
            name="Original",
            surname="User",
            email="original-user@example.com",
        )
        existing_link = ProjectExternalUsers.objects.create(
            project=self.projects[0],
            contact_user=contact,
            role="supplier",
        )
        serializer = ContactUserSerializer(
            contact,
            data={"name": "Changed", "projects": self.mixed_projects()},
            partial=True,
            context={"request": self.request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(serializers.ValidationError):
            serializer.save()

        contact.refresh_from_db()
        existing_link.refresh_from_db()
        self.assertEqual(contact.name, "Original")
        self.assertEqual(existing_link.role, "supplier")
        self.assertEqual(ProjectExternalUsers.objects.count(), 1)

    @patch(
        "project_management.serializers.contact.get_valid_choices",
        return_value=["consultant", "supplier"],
    )
    def test_company_update_with_invalid_assignment_changes_nothing(self, _choices):
        contact = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Original Company",
            contact_email="original-company@example.com",
        )
        existing_link = ProjectExternalUsers.objects.create(
            project=self.projects[0],
            contact_company=contact,
            role="supplier",
        )
        serializer = ContactCompanySerializer(
            contact,
            data={
                "company_name": "Changed Company",
                "projects": self.mixed_projects(),
            },
            partial=True,
            context={"request": self.request()},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        with self.assertRaises(serializers.ValidationError):
            serializer.save()

        contact.refresh_from_db()
        existing_link.refresh_from_db()
        self.assertEqual(contact.company_name, "Original Company")
        self.assertEqual(existing_link.role, "supplier")
        self.assertEqual(ProjectExternalUsers.objects.count(), 1)
