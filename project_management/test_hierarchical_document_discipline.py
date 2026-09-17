from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db.models import Q
from django.db.models.signals import post_save
from django.test import SimpleTestCase, TestCase
from rest_framework import serializers

from companyprofileapis.services.hierarchical_configurations import (
    validate_removed_document_disciplines,
)
from models.hierarchical_configurations import (
    DOCUMENT_DISCIPLINES_KEY,
    get_default_nested_configurations,
)
from models.models import CompanyConfig, CompanyProfile, User
from project_management.models import Document, ECPMPConfig, Folder, Project
from project_management.serializers.document import DocumentSerializer
from project_management.signals import create_default_folders


class DocumentDisciplineValidationTests(SimpleTestCase):
    def setUp(self):
        self.configuration = get_default_nested_configurations()
        self.serializer = DocumentSerializer(
            data={
                "discipline_group_id": "engineering",
                "discipline_option_id": "electrical-engineering",
            }
        )
        self.serializer.get_config = Mock(
            return_value=SimpleNamespace(
                nested_configurations=self.configuration
            )
        )

    def test_derives_document_discipline_snapshot_from_ids(self):
        validated = self.serializer.validate(
            {
                "discipline_group_id": "engineering",
                "discipline_option_id": "electrical-engineering",
            }
        )

        self.assertEqual(validated["discipline"], "Electrical Engineering")
        self.assertEqual(validated["discipline_group"], "Engineering")
        self.assertEqual(validated["discipline_code"], "ELE")

    def test_rejects_child_from_another_group(self):
        with self.assertRaises(serializers.ValidationError):
            self.serializer.validate(
                {
                    "discipline_group_id": "project-management",
                    "discipline_option_id": "electrical-engineering",
                }
            )

    def test_rejects_group_without_discipline(self):
        with self.assertRaises(serializers.ValidationError):
            self.serializer.validate(
                {
                    "discipline_group_id": "engineering",
                    "discipline_option_id": "",
                    "discipline": "",
                }
            )


class LinkedDocumentDisciplineDeletionTests(SimpleTestCase):
    def test_rejects_deleting_custom_discipline_used_by_a_document(self):
        current = get_default_nested_configurations()
        current[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"].append(
            {"id": "custom-discipline", "label": "Custom", "code": "CUS"}
        )
        merged = deepcopy(current)
        merged[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"] = [
            child
            for child in merged[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"]
            if child["id"] != "custom-discipline"
        ]
        config = CompanyConfig(
            company_profile_id=1,
            solution="Energy Consultant Solution",
            nested_configurations=current,
        )
        linked_queryset = Mock()
        linked_queryset.filter.return_value.exists.return_value = True

        with patch(
            "companyprofileapis.services.hierarchical_configurations.Document.objects.filter",
            return_value=linked_queryset,
        ):
            with self.assertRaises(serializers.ValidationError):
                validate_removed_document_disciplines(config, merged)

    def test_project_config_checks_only_project_documents(self):
        current = get_default_nested_configurations()
        current[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"].append(
            {"id": "project-custom", "label": "Project Custom", "code": "PCU"}
        )
        merged = deepcopy(current)
        merged[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"] = [
            child
            for child in merged[DOCUMENT_DISCIPLINES_KEY]["groups"][-1]["children"]
            if child["id"] != "project-custom"
        ]
        config = ECPMPConfig(
            project_id=42,
            nested_configurations=current,
        )
        linked_queryset = Mock()
        linked_queryset.filter.return_value.exists.return_value = True

        with patch(
            "companyprofileapis.services.hierarchical_configurations.Document.objects.filter",
            return_value=linked_queryset,
        ) as document_filter:
            with self.assertRaises(serializers.ValidationError):
                validate_removed_document_disciplines(config, merged)

        document_filter.assert_called_once_with(Q(folder__project_id=42))


class DocumentDisciplinePersistenceTests(TestCase):
    def setUp(self):
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.addCleanup(self.notification_patcher.stop)
        post_save.disconnect(create_default_folders, sender=Project)
        self.addCleanup(
            post_save.connect,
            create_default_folders,
            sender=Project,
        )
        self.user = User.objects.create_user(
            email="hierarchical-documents@example.com",
            password="password",
            first_name="Hierarchy",
            last_name="Tester",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Hierarchy Test Company",
            company_address="Test Street 1",
            company_town="Test Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="hierarchy@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.hierarchy = get_default_nested_configurations()
        construction_group = next(
            group
            for group in self.hierarchy[DOCUMENT_DISCIPLINES_KEY]["groups"]
            if group["id"] == "construction-site-management"
        )
        construction_group["children"].append(
            {
                "id": "custom-sky-scraper",
                "label": "Sky Scraper",
                "code": "SKY",
            }
        )
        CompanyConfig.objects.update_or_create(
            company_profile=self.company,
            solution="Energy Consultant Solution",
            defaults={"nested_configurations": self.hierarchy},
        )
        self.company_folder = Folder.objects.create(
            company_profile=self.company,
            name="Company hierarchy documents",
        )
        self.company_document = Document.objects.create(
            folder=self.company_folder,
            author=self.user,
            created_by=self.user,
            file="documents/company-hierarchy.pdf",
            file_name="company-hierarchy.pdf",
        )
        self.project = Project.objects.create(
            project_name="Hierarchy Test Project",
            contract_value=1000,
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )
        ECPMPConfig.objects.update_or_create(
            project=self.project,
            defaults={"nested_configurations": self.hierarchy},
        )
        self.project_folder = Folder.objects.create(
            project=self.project,
            name="Project hierarchy documents",
        )
        self.project_document = Document.objects.create(
            folder=self.project_folder,
            author=self.user,
            created_by=self.user,
            file="documents/project-hierarchy.pdf",
            file_name="project-hierarchy.pdf",
        )

    def _update_document(self, document, group_id, option_id):
        serializer = DocumentSerializer(
            instance=document,
            data={
                "discipline_group_id": group_id,
                "discipline_option_id": option_id,
            },
            partial=True,
            context={"user": self.user},
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        document.refresh_from_db()
        return document

    def test_company_document_update_persists_discipline_group_snapshot(self):
        document = self._update_document(
            self.company_document,
            "engineering",
            "electrical-engineering",
        )

        self.assertEqual(document.discipline, "Electrical Engineering")
        self.assertEqual(document.discipline_group, "Engineering")
        self.assertEqual(document.discipline_group_id, "engineering")
        self.assertEqual(document.discipline_option_id, "electrical-engineering")
        self.assertEqual(document.discipline_code, "ELE")

    def test_project_document_update_persists_discipline_group_snapshot(self):
        document = self._update_document(
            self.project_document,
            "digital-information-systems",
            "document-control",
        )

        self.assertEqual(document.discipline, "Document Control")
        self.assertEqual(
            document.discipline_group,
            "Digital & Information Management",
        )
        self.assertEqual(
            document.discipline_group_id,
            "digital-information-systems",
        )
        self.assertEqual(document.discipline_option_id, "document-control")
        self.assertEqual(document.discipline_code, "DOC")

    def test_document_update_clears_all_discipline_hierarchy_fields(self):
        document = self._update_document(
            self.company_document,
            "engineering",
            "electrical-engineering",
        )
        serializer = DocumentSerializer(
            instance=document,
            data={
                "discipline": "",
                "discipline_group_id": "",
                "discipline_option_id": "",
            },
            partial=True,
            context={"user": self.user},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        document.refresh_from_db()
        self.assertEqual(document.discipline, "")
        self.assertEqual(document.discipline_group, "")
        self.assertEqual(document.discipline_group_id, "")
        self.assertEqual(document.discipline_option_id, "")
        self.assertEqual(document.discipline_code, "")

    def test_clearing_legacy_discipline_also_clears_hierarchy_fields(self):
        document = self._update_document(
            self.company_document,
            "engineering",
            "electrical-engineering",
        )
        serializer = DocumentSerializer(
            instance=document,
            data={"discipline": ""},
            partial=True,
            context={"user": self.user},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        document.refresh_from_db()
        self.assertEqual(document.discipline, "")
        self.assertEqual(document.discipline_group, "")
        self.assertEqual(document.discipline_group_id, "")
        self.assertEqual(document.discipline_option_id, "")
        self.assertEqual(document.discipline_code, "")

    def test_metadata_update_allows_unchanged_retired_document_type(self):
        self.project_document.document_type = "Funding Request"
        self.project_document.save(update_fields=["document_type"])

        serializer = DocumentSerializer(
            instance=self.project_document,
            data={
                "document_type": "Funding Request",
                "discipline": "Sky Scraper",
                "discipline_group_id": "construction-site-management",
                "discipline_option_id": "custom-sky-scraper",
            },
            partial=True,
            context={"user": self.user},
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        document = serializer.save()
        self.assertEqual(document.document_type, "Funding Request")
        self.assertEqual(document.discipline, "Sky Scraper")
        self.assertEqual(document.discipline_code, "SKY")

    def test_metadata_update_rejects_changed_invalid_document_type(self):
        serializer = DocumentSerializer(
            instance=self.project_document,
            data={"document_type": "Invalid Changed Type"},
            partial=True,
            context={"user": self.user},
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("document_type", serializer.errors)
