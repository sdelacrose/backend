from concurrent.futures import ThreadPoolExecutor
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from rest_framework import serializers

from models.models import CompanyProfile, User
from project_management.models import Document, DocumentReferenceCounter, Folder, Project
from project_management.serializers.document import DocumentSerializer
from project_management.services.document_numbering import (
    allocate_document_sequence,
    resolve_document_library_scope,
)


def create_user(email, *, user_type="company_user"):
    return User.objects.create_user(
        email=email,
        password="password",
        first_name="Document",
        last_name="Owner",
        user_type=user_type,
    )


def create_company(owner, name):
    company = CompanyProfile.objects.create(
        admin_user=owner,
        company_name=name,
        company_address="Street 1",
        company_town="Town",
        company_tell_no="123",
        company_mob_no="456",
        company_web_address="https://example.com",
        company_email_address=owner.email,
        company_tax_reference="tax",
        company_iban="iban",
    )
    owner.company = company
    owner.save(update_fields=["company"])
    return company


def create_project(company, owner, name):
    return Project.objects.bulk_create(
        [
            Project(
                project_name=name,
                contract_value=1000,
                company_profile=company,
                user=owner,
                created_by=owner,
            )
        ]
    )[0]


class DocumentLibraryNumberingTests(TestCase):
    def setUp(self):
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.addCleanup(self.notification_patcher.stop)

        self.company_owner = create_user("company-a@example.com")
        self.company = create_company(self.company_owner, "Company A")
        self.other_company_owner = create_user("company-b@example.com")
        self.other_company = create_company(
            self.other_company_owner,
            "Company B",
        )
        self.project = create_project(
            self.company,
            self.company_owner,
            "Project A",
        )
        self.other_project = create_project(
            self.company,
            self.company_owner,
            "Project B",
        )
        self.freelancer = create_user(
            "freelancer@example.com",
            user_type="freelancer",
        )

        self.company_folder = Folder.objects.create(
            company_profile=self.company,
            name="Client Correspondence",
        )
        self.company_nested_folder = Folder.objects.create(
            company_profile=self.company,
            parent=self.company_folder,
            name="Help",
        )
        self.other_company_folder = Folder.objects.create(
            company_profile=self.other_company,
            name="Client Correspondence",
        )
        self.project_folder = Folder.objects.create(
            project=self.project,
            name="Project Documents",
        )
        self.project_nested_folder = Folder.objects.create(
            project=self.project,
            parent=self.project_folder,
            name="Specifications",
        )
        self.other_project_folder = Folder.objects.create(
            project=self.other_project,
            name="Project Documents",
        )
        self.freelancer_folder = Folder.objects.create(
            freelancer=self.freelancer,
            name="Freelancer Documents",
        )

    def test_all_folders_in_company_library_share_sequence(self):
        self.assertEqual(allocate_document_sequence(self.company_folder), 1)
        self.assertEqual(allocate_document_sequence(self.company_nested_folder), 2)

    def test_serializer_creates_use_the_shared_company_sequence(self):
        with patch.object(DocumentSerializer, "get_config", return_value=None), patch(
            "project_management.serializers.document.get_abbreviation",
            return_value="OTH",
        ):
            first_document = DocumentSerializer(
                context={"user": self.company_owner}
            ).create(
                {
                    "folder": self.company_folder,
                    "author": self.company_owner,
                    "file": "documents/company-first.pdf",
                    "revision": 0,
                }
            )
            second_document = DocumentSerializer(
                context={"user": self.company_owner}
            ).create(
                {
                    "folder": self.company_nested_folder,
                    "author": self.company_owner,
                    "file": "documents/company-second.pdf",
                    "revision": 0,
                }
            )

        self.assertEqual(first_document.sequence_number, 1)
        self.assertEqual(second_document.sequence_number, 2)
        self.assertTrue(first_document.full_document_number.endswith("-00001"))
        self.assertTrue(second_document.full_document_number.endswith("-00002"))

    def test_company_project_and_freelancer_libraries_are_independent(self):
        self.assertEqual(allocate_document_sequence(self.company_folder), 1)
        self.assertEqual(allocate_document_sequence(self.project_folder), 1)
        self.assertEqual(allocate_document_sequence(self.other_project_folder), 1)
        self.assertEqual(allocate_document_sequence(self.other_company_folder), 1)
        self.assertEqual(allocate_document_sequence(self.freelancer_folder), 1)

        self.assertEqual(allocate_document_sequence(self.project_nested_folder), 2)
        self.assertEqual(allocate_document_sequence(self.company_nested_folder), 2)

    def test_nested_folder_inherits_parent_library_owner(self):
        nested_folder = Folder.objects.create(
            parent=self.company_folder,
            name="Inherited Company Folder",
        )

        self.assertEqual(nested_folder.company_profile_id, self.company.id)
        self.assertEqual(
            resolve_document_library_scope(nested_folder),
            ("company_profile", self.company.id),
        )

    def test_counter_recovers_from_existing_library_maximum(self):
        Document.objects.create(
            folder=self.company_nested_folder,
            file="documents/existing.pdf",
            sequence_number=7,
            revision=0,
            full_document_number="COM-OTH-OTH-OTH-00007-00",
        )

        self.assertEqual(allocate_document_sequence(self.company_folder), 8)

    def test_allocated_numbers_are_not_reused(self):
        self.assertEqual(allocate_document_sequence(self.company_folder), 1)
        self.assertEqual(allocate_document_sequence(self.company_folder), 2)

        self.assertEqual(allocate_document_sequence(self.company_folder), 3)

    def test_cross_library_document_move_is_rejected(self):
        document = Document(
            folder=self.company_folder,
            file="documents/company.pdf",
            sequence_number=1,
            revision=0,
            full_document_number="COM-OTH-OTH-OTH-00001-00",
        )
        serializer = DocumentSerializer(instance=document, partial=True)

        with self.assertRaises(serializers.ValidationError):
            serializer.update(document, {"folder": self.project_folder})

    def test_counter_rows_are_separate_per_library(self):
        allocate_document_sequence(self.company_folder)
        allocate_document_sequence(self.project_folder)
        allocate_document_sequence(self.freelancer_folder)

        self.assertEqual(DocumentReferenceCounter.objects.count(), 3)

    def test_backfill_repairs_duplicates_within_each_library(self):
        first_company_document = Document.objects.create(
            folder=self.company_folder,
            file="documents/company-first.pdf",
            sequence_number=1,
            revision=0,
            full_document_number="COM-OTH-OTH-OTH-00001-00",
        )
        duplicate_company_document = Document.objects.create(
            folder=self.company_nested_folder,
            file="documents/company-duplicate.pdf",
            sequence_number=1,
            revision=0,
            full_document_number="COM-OTH-OTH-OTH-00001-00",
        )
        other_company_document = Document.objects.create(
            folder=self.other_company_folder,
            file="documents/other-company.pdf",
            sequence_number=1,
            revision=0,
            full_document_number="OTH-OTH-OTH-OTH-00001-00",
        )
        unnumbered_document = Document.objects.create(
            folder=self.company_folder,
            file="documents/generated-invoice.pdf",
            sequence_number=0,
            revision=0,
            full_document_number="",
        )
        migration = import_module(
            "project_management.migrations.0174_document_reference_counter"
        )

        migration.backfill_document_reference_counters(
            apps,
            SimpleNamespace(connection=connection),
        )

        first_company_document.refresh_from_db()
        duplicate_company_document.refresh_from_db()
        other_company_document.refresh_from_db()
        unnumbered_document.refresh_from_db()
        self.assertEqual(first_company_document.sequence_number, 1)
        self.assertEqual(duplicate_company_document.sequence_number, 2)
        self.assertEqual(
            duplicate_company_document.full_document_number,
            "COM-OTH-OTH-OTH-00002-00",
        )
        self.assertEqual(other_company_document.sequence_number, 1)
        self.assertEqual(unnumbered_document.sequence_number, 0)
        self.assertEqual(unnumbered_document.full_document_number, "")
        self.assertEqual(
            DocumentReferenceCounter.objects.get(
                company_profile=self.company,
            ).last_sequence,
            2,
        )
        self.assertEqual(
            DocumentReferenceCounter.objects.get(
                company_profile=self.other_company,
            ).last_sequence,
            1,
        )


class ConcurrentDocumentNumberingTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        self.notification_patcher.start()
        self.addCleanup(self.notification_patcher.stop)

        self.freelancer = create_user(
            "concurrent-freelancer@example.com",
            user_type="freelancer",
        )
        self.folder = Folder.objects.create(
            freelancer=self.freelancer,
            name="Concurrent Documents",
        )

    def _allocate_sequence(self):
        close_old_connections()
        try:
            folder = Folder.objects.get(id=self.folder.id)
            return allocate_document_sequence(folder)
        finally:
            close_old_connections()

    def test_concurrent_allocations_are_unique(self):
        if not connection.features.has_select_for_update:
            self.skipTest("Database does not support row-level locking.")

        with ThreadPoolExecutor(max_workers=2) as executor:
            sequences = list(executor.map(lambda _: self._allocate_sequence(), range(2)))

        self.assertEqual(sorted(sequences), [1, 2])


class DocumentReferenceMigrationHelperTests(SimpleTestCase):
    def test_replaces_standard_document_sequence_suffix(self):
        migration = import_module(
            "project_management.migrations.0174_document_reference_counter"
        )

        self.assertEqual(
            migration._replace_sequence(
                "SHA-OTH-OTH-OTH-00001-00",
                3,
                0,
            ),
            "SHA-OTH-OTH-OTH-00003-00",
        )
