from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection, IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from chat.models import ChatRoom
from companyprofileapis.models import Opportunity
from companyprofileapis.tests.test_opportunity_offer_flow import (
    create_company_user, NotificationPreferenceStubMixin,
)
from project_management.models import Document, Folder
from project_management.services.company_root_repair import repair_company_roots
from project_management.views.related_objects_management import CreateRelatedObjectsOnCompanyCreate


@override_settings(MATRIX_ENABLED=False)
class CompanyRootRepairTests(NotificationPreferenceStubMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user, self.company = create_company_user()
        self.name = f'{self.company.pk}_company_root_folder'

    def root(self):
        return Folder.objects.create(company_profile=self.company, name=self.name)

    def legacy_roots(self):
        # Model the legacy schema that allowed duplicate NULL-parent roots.
        with connection.cursor() as cursor:
            cursor.execute('DROP INDEX folder_unique_company_root_name')
        return self.root(), self.root()

    def test_database_rejects_duplicate_null_parent_root(self):
        self.root()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.root()

    def test_merge_preserves_documents_nested_folders_and_links(self):
        old, new = self.legacy_roots()
        left = Folder.objects.create(parent=old, company_profile=self.company, name='invoices')
        right = Folder.objects.create(parent=new, company_profile=self.company, name='invoices')
        nested = Folder.objects.create(parent=left, company_profile=self.company, name='nested')
        doc = Document.objects.create(folder=left, file='existing/old/path.pdf')
        offer = Opportunity.objects.create(created_by=self.user, company_profile=self.company, title='Test')
        offer.linked_folders.add(old, new, left)
        report = repair_company_roots([self.company.pk], apply=True)
        doc.refresh_from_db(); nested.refresh_from_db()
        self.assertEqual(doc.folder_id, right.pk)
        self.assertEqual(doc.file.name, 'existing/old/path.pdf')
        self.assertEqual(nested.parent_id, right.pk)
        self.assertEqual(set(offer.linked_folders.values_list('pk', flat=True)), {new.pk, right.pk})
        self.assertEqual(report[0]['documents'], 1)
        self.assertFalse(Folder.objects.filter(pk__in=[old.pk, left.pk]).exists())
        self.assertEqual(repair_company_roots([self.company.pk], apply=True), [])

    def test_command_defaults_to_rollback(self):
        old, new = self.legacy_roots()
        doc = Document.objects.create(folder=old, file='existing.pdf')
        call_command('repair_company_document_roots', company_ids=[self.company.pk], stdout=StringIO())
        doc.refresh_from_db()
        self.assertEqual(doc.folder_id, old.pk)
        self.assertEqual(Folder.objects.filter(pk__in=[old.pk, new.pk]).count(), 2)

    def test_conflict_rolls_back_entire_repair(self):
        old, new = self.legacy_roots()
        Folder.objects.filter(pk=old.pk).update(system_key='a')
        Folder.objects.filter(pk=new.pk).update(system_key='b')
        with self.assertRaisesMessage(ValueError, 'Conflicting system'):
            repair_company_roots([self.company.pk], apply=True)
        self.assertEqual(Folder.objects.filter(pk__in=[old.pk, new.pk]).count(), 2)

    @override_settings(API_SECRET_KEY='test-company-setup')
    def test_repeated_setup_reuses_existing_tree(self):
        view = CreateRelatedObjectsOnCompanyCreate.as_view()
        def request():
            return APIRequestFactory().post('/', {'SECRET_TOKEN': 'test-company-setup', 'company_id': self.company.pk}, format='json')
        self.assertEqual(view(request()).status_code, 200)
        self.assertEqual(view(request()).status_code, 200)
        root = Folder.objects.get(company_profile=self.company, name=self.name, parent=None)
        self.assertEqual(Folder.objects.filter(parent=root).count(), 5)
        self.assertEqual(ChatRoom.objects.filter(company=self.company, is_direct=False).count(), 1)

    @override_settings(API_SECRET_KEY='test-company-setup')
    def test_setup_failure_rolls_back_new_folders(self):
        request = APIRequestFactory().post('/', {
            'SECRET_TOKEN': 'test-company-setup', 'company_id': self.company.pk,
        }, format='json')
        with patch.object(ChatRoom.objects, 'create', side_effect=ValueError('failed')):
            response = CreateRelatedObjectsOnCompanyCreate.as_view()(request)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Folder.objects.filter(company_profile=self.company).exists())
