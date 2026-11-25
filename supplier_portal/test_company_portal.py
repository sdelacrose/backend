from unittest.mock import patch
from django.test import override_settings
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.exceptions import PermissionDenied
from project_management.models import Document
from supplier_portal.tests import SupplierRegisterTests
from supplier_portal.views import InternalDocuments


@override_settings(SUPPLIER_REGISTER_ENABLED=True)
class CompanyPortalTests(SupplierRegisterTests):
    def documents(self, params=None, user=None, scope=None):
        request = APIRequestFactory().get('/supplier-register/documents', params or {})
        force_authenticate(request, user=user or self.creator)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True), \
             patch('supplier_portal.views.assert_project_manager'), \
             patch('project_management.services.document_access.get_accessible_document_queryset',
                   return_value=scope if scope is not None else Document.objects.all()):
            return InternalDocuments.as_view()(request, project_id=self.project.pk)

    def test_company_lists_documents_from_multiple_suppliers_and_fileless_placeholders(self):
        second_supplier = self.network_company('Second Supplier')
        other = self.create(document_origin='SUPPLIER', external_issuer_company=second_supplier.pk,
            identification_number='SECOND-REF', file_name='Second supplier placeholder')
        response = self.documents()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual({r['id'] for r in response.data['documents']}, {self.doc.pk, other.pk})
        self.assertEqual(len(response.data['suppliers']), 2)
        self.assertEqual(next(r for r in response.data['documents'] if r['id'] == other.pk)['status'], 'TO_SUBMIT')

    def test_supplier_filter_does_not_mix_suppliers(self):
        other_supplier = self.network_company('Other Supplier')
        self.create(document_origin='SUPPLIER', external_issuer_company=other_supplier.pk, identification_number='OTHER')
        response = self.documents({'supplier': self.supplier_company.pk})
        self.assertEqual([r['id'] for r in response.data['documents']], [self.doc.pk])
        self.assertEqual(len(response.data['suppliers']), 2)

    def test_internal_documents_and_other_project_are_excluded(self):
        self.create(document_origin='INTERNAL', file_name='Internal private document')
        second_project, folder = self.make_project('Other project')
        other = self.create(document_origin='SUPPLIER', external_issuer_company=self.supplier_company.pk)
        Document.objects.filter(pk=other.pk).update(folder=folder)
        response = self.documents()
        self.assertEqual([r['id'] for r in response.data['documents']], [self.doc.pk])

    def test_existing_document_read_scope_is_preserved(self):
        response = self.documents(scope=Document.objects.none())
        self.assertEqual(response.data['documents'], [])
        self.assertEqual(response.data['suppliers'], [])

    def test_external_supplier_cannot_open_company_view(self):
        self.assertEqual(self.documents(user=self.supplier).status_code, 404)

    def test_invalid_filters_are_rejected(self):
        for params in ({'page': 0}, {'page': 'bad'}, {'supplier': -1}, {'supplier': 'bad'}):
            self.assertEqual(self.documents(params).status_code, 400)

    def test_unknown_supplier_returns_empty_without_disclosing_a_foreign_supplier(self):
        response = self.documents({'supplier': 999999})
        self.assertEqual(response.data['count'], 0)
        self.assertEqual(response.data['documents'], [])

    def test_search_and_pagination(self):
        response = self.documents({'search': self.doc.file_name})
        self.assertEqual(response.data['count'], 1)
        response = self.documents({'page': 2})
        self.assertEqual(response.data['documents'], [])
        self.assertEqual(response.data['previous_page'], 1)

    def test_company_management_rights_are_required(self):
        request = APIRequestFactory().get('/supplier-register/documents')
        force_authenticate(request, user=self.creator)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True), \
             patch('supplier_portal.views.assert_project_manager', side_effect=PermissionDenied):
            response = InternalDocuments.as_view()(request, project_id=self.project.pk)
        self.assertEqual(response.status_code, 403)
