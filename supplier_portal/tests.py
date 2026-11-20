from io import BytesIO
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate
from openpyxl import Workbook
from project_management import test_supplier_portal as portal_tests
from project_management.models import Document, DocumentRevision
from project_management.services.supplier_portal import portal_grant, submit_revision, visible_revisions
from supplier_portal.models import SupplierProjectAccess, SupplierDocumentProposal
from supplier_portal.register import propose_batch, decide_batch, identity_key, grant_project, revoke_project
from supplier_portal.queries import dashboard, projects_for
from supplier_portal.files import template_bytes, parse_list
from supplier_portal.views import PortalBatchDetail, PortalBulkFiles, InternalBatchDetail, InternalProjectAccess, project_choices


@override_settings(SUPPLIER_REGISTER_ENABLED=True)
class SupplierRegisterTests(portal_tests.SupplierPortalTests):
    def member(self, user=None, project=None, folder=None, supplier=None):
        return SupplierProjectAccess.objects.create(user=user or self.supplier, project=project or self.project,
            folder=folder or self.folder, supplier=supplier or self.supplier_company, granted_by=self.creator)

    def metadata(self, reference='S-001', title='Pump drawing'):
        return {'file_name': title, 'identification_number': reference, 'document_type': 'Drawing'}

    def propose(self, rows=None, member=None):
        return propose_batch(user=self.supplier, access_id=(member or self.member()).pk, rows=rows or [self.metadata()])

    def decide(self, batch, action='ACCEPT', **kwargs):
        with patch('supplier_portal.register.assert_project_manager'):
            return decide_batch(actor=self.creator, batch=batch, action=action, **kwargs)

    def test_proposal_is_not_a_document_or_a_physical_version(self):
        count, versions = Document.objects.count(), DocumentRevision.objects.count()
        batch = self.propose()
        row = batch.rows.get()
        self.assertEqual(row.status, 'PENDING')
        self.assertIsNone(row.document_id)
        self.assertEqual(Document.objects.count(), count)
        self.assertEqual(DocumentRevision.objects.count(), versions)

    def test_missing_title_is_refused_and_cannot_be_accepted(self):
        batch = self.propose([{'identification_number': 'NO-TITLE'}])
        self.assertEqual(batch.rows.get().status, 'REFUSED')
        self.assertIn('file_name', batch.rows.get().errors)
        result = self.decide(batch)
        self.assertEqual(result['accepted'], [])
        self.assertIsNone(batch.rows.get().document_id)

    def test_unknown_or_privileged_metadata_is_refused(self):
        batch = self.propose([{**self.metadata(), 'folder': 999, 'file': '/private/file', 'status': 'APPROVED'}])
        self.assertEqual(batch.rows.get().status, 'REFUSED')
        self.assertIn('columns', batch.rows.get().errors)

    def test_invalid_type_uses_native_form_validation(self):
        batch = self.propose([{**self.metadata(), 'document_type': 'NONEXISTENT-TYPE'}])
        self.assertEqual(batch.rows.get().status, 'REFUSED')
        self.assertIn('document_type', batch.rows.get().errors)

    def test_all_duplicates_inside_batch_are_refused(self):
        batch = self.propose([self.metadata(' S-001 '), self.metadata('s-001')])
        self.assertEqual(list(batch.rows.values_list('status', flat=True)), ['REFUSED', 'REFUSED'])

    def test_existing_supplier_reference_is_detected(self):
        batch = self.propose([self.metadata(self.doc.identification_number.lower())])
        self.assertIn('duplicate', batch.rows.get().errors)

    def test_pending_proposal_reserves_reference(self):
        member = self.member()
        self.propose(member=member)
        second = self.propose(member=member)
        self.assertEqual(second.rows.get().status, 'REFUSED')

    def test_same_reference_is_allowed_for_another_supplier(self):
        first = self.propose()
        self.client_company.company_profile = self.creator.company
        self.client_company.save(update_fields=['company_profile'])
        access = self.member(supplier=self.client_company)
        second = self.propose(member=access)
        self.assertEqual(first.rows.get().status, second.rows.get().status)
        self.assertEqual(second.rows.get().status, 'PENDING')

    def test_accept_all_accepts_only_valid_rows_and_creates_no_file(self):
        batch = self.propose([self.metadata(), {'file_name': ''}])
        result = self.decide(batch)
        self.assertEqual(len(result['accepted']), 1)
        self.assertEqual(len(result['skipped']), 1)
        accepted = batch.rows.get(status='ACCEPTED')
        self.assertFalse(accepted.document.file)
        self.assertFalse(accepted.document.revisions.exists())
        self.assertEqual(accepted.document.external_issuer_company_id, self.supplier_company.pk)
        self.assertEqual(accepted.document.folder.project_id, self.project.pk)

    def test_selective_acceptance_unblocks_only_selected_documents(self):
        member = self.member()
        batch = self.propose([self.metadata('A'), self.metadata('B')], member)
        selected = batch.rows.first()
        self.decide(batch, row_ids=[selected.pk])
        selected.refresh_from_db()
        portal_grant(self.supplier, selected.document_id)
        self.assertEqual(batch.rows.filter(status='PENDING').count(), 1)

    def test_client_refusal_requires_reason_and_is_historised(self):
        batch = self.propose()
        with self.assertRaises(ValidationError):
            self.decide(batch, 'REFUSE', reason='   ')
        self.decide(batch, 'REFUSE', reason='Not within supply scope')
        row = batch.rows.get()
        self.assertEqual(row.status, 'REFUSED')
        self.assertEqual(row.decided_by_id, self.creator.pk)
        self.assertEqual(list(row.events.values_list('event_type', flat=True)), ['SUBMITTED', 'REFUSED'])

    def test_corrected_refusal_is_a_new_submission_without_erasing_history(self):
        member = self.member()
        first = self.propose([{'identification_number': 'A'}], member)
        corrected = self.propose([self.metadata('A')], member)
        self.assertEqual(first.rows.get().status, 'REFUSED')
        self.assertEqual(corrected.rows.get().status, 'PENDING')

    def test_reference_created_after_submission_is_rechecked_at_approval(self):
        batch = self.propose()
        self.create(document_origin='SUPPLIER', external_issuer_company=self.supplier_company.pk, identification_number='S-001')
        result = self.decide(batch)
        self.assertEqual(result['accepted'], [])
        self.assertEqual(batch.rows.get().status, 'REFUSED')

    def test_foreign_row_id_cannot_be_injected_into_bulk_decision(self):
        member = self.member()
        batch = self.propose(member=member)
        other = self.propose([self.metadata('OTHER')], member)
        with self.assertRaises(Http404):
            self.decide(batch, row_ids=[other.rows.get().pk])
        self.assertEqual(batch.rows.get().status, 'PENDING')

    def test_supplier_cannot_validate_or_grant_itself_project_access(self):
        batch = self.propose()
        with self.assertRaises(PermissionDenied):
            decide_batch(actor=self.supplier, batch=batch, action='ACCEPT')
        with self.assertRaises(PermissionDenied):
            grant_project(actor=self.supplier, project=self.project, supplier=self.supplier_company, folder=self.folder, email=self.other.email)

    def test_project_access_exists_before_any_document_and_is_revocable(self):
        project, folder = self.make_project('Empty supplier project')
        member = self.member(project=project, folder=folder)
        self.assertIn(project.pk, [p['id'] for p in projects_for(self.supplier)])
        with patch('supplier_portal.register.assert_project_manager'):
            revoke_project(actor=self.creator, access=member)
        self.assertNotIn(project.pk, [p['id'] for p in projects_for(self.supplier)])
        with self.assertRaises(Http404):
            self.propose(member=member)

    def test_project_selector_and_kpis_never_mix_projects(self):
        self.member()
        project, folder = self.make_project('Second supplier project')
        self.member(project=project, folder=folder)
        self.assertEqual(len(projects_for(self.supplier)), 2)
        self.assertEqual(dashboard(self.supplier, project.pk)['kpis']['total'], 0)
        self.assertEqual(dashboard(self.supplier, self.project.pk)['kpis']['total'], 1)
        with self.assertRaises(Http404):
            dashboard(self.other, self.project.pk)

    def test_pending_is_separate_from_accepted_total_and_percentage(self):
        member = self.member()
        batch = self.propose(member=member)
        before = dashboard(self.supplier, self.project.pk)['kpis']
        self.assertEqual((before['total'], before['pending']), (1, 1))
        self.decide(batch)
        after = dashboard(self.supplier, self.project.pk)['kpis']
        self.assertEqual((after['total'], after['pending'], after['received_percent']), (2, 0, 50))

    def test_accepted_placeholder_receives_real_supplier_revision(self):
        batch = self.propose()
        self.decide(batch)
        row = batch.rows.get()
        result = submit_revision(user=self.supplier, document_id=row.document_id,
            uploaded_file=SimpleUploadedFile('pump_A.pdf', b'file bytes'), external_revision='A')
        self.assertEqual(result.revision.created_by_id, self.supplier.pk)
        self.assertIsNone(result.grant_id)
        self.assertEqual(result.project_access.user_id, self.supplier.pk)
        self.assertEqual(visible_revisions(portal_grant(self.supplier, row.document_id)).count(), 1)

    def test_other_supplier_cannot_read_batch_or_submit_into_project(self):
        member = self.member()
        batch = self.propose(member=member)
        request = APIRequestFactory().get('/x')
        force_authenticate(request, user=self.other)
        response = PortalBatchDetail.as_view()(request, access_id=member.pk, batch_id=batch.pk)
        self.assertEqual(response.status_code, 404)
        with self.assertRaises(Http404):
            propose_batch(user=self.other, access_id=member.pk, rows=[self.metadata()])

    def test_template_roundtrip_does_not_create_a_fake_document(self):
        from openpyxl import load_workbook
        workbook = load_workbook(BytesIO(template_bytes()))
        workbook['Documents'].append(['New drawing', 'S-TEST'])
        output = BytesIO()
        workbook.save(output)
        rows = parse_list(SimpleUploadedFile('list.xlsx', output.getvalue()))
        self.assertEqual(rows[0]['file_name'], 'New drawing')
        self.assertEqual(rows[0]['identification_number'], 'S-TEST')

    def test_formula_cells_and_unknown_headers_are_rejected(self):
        workbook = Workbook()
        workbook.active.append(['file_name'])
        workbook.active.append(['=HYPERLINK("https://invalid.example")'])
        output = BytesIO()
        workbook.save(output)
        with self.assertRaises(ValidationError):
            parse_list(SimpleUploadedFile('list.xlsx', output.getvalue()))
        with self.assertRaises(ValidationError):
            parse_list(SimpleUploadedFile('list.csv', b'file_name,folder\nTitle,5'))

    def test_csv_and_limits_are_validated_before_any_document_creation(self):
        self.assertEqual(parse_list(SimpleUploadedFile('list.csv', b'file_name;identification_number\nTitle;A'))[0]['file_name'], 'Title')
        with self.assertRaises(ValidationError):
            self.propose([self.metadata(str(index)) for index in range(1001)])

    def test_bulk_physical_upload_cannot_duplicate_a_document(self):
        member = self.member()
        batch = self.propose(member=member)
        self.decide(batch)
        document_id = batch.rows.get().document_id
        import json
        request = APIRequestFactory().post('/x', {'manifest': json.dumps([{'document': document_id}, {'document': document_id}]),
            'files': [SimpleUploadedFile('A.pdf', b'A'), SimpleUploadedFile('B.pdf', b'B')]}, format='multipart')
        force_authenticate(request, user=self.supplier)
        response = PortalBulkFiles.as_view()(request, project_id=self.project.pk)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(DocumentRevision.objects.filter(document_id=document_id).exists())

    def test_accept_retry_is_idempotent(self):
        batch = self.propose()
        self.decide(batch)
        document = batch.rows.get().document_id
        result = self.decide(batch)
        self.assertEqual(result['accepted'], [])
        self.assertEqual(batch.rows.get().document_id, document)
        self.assertEqual(batch.rows.get().events.filter(event_type='ACCEPTED').count(), 1)

    def test_destination_moved_after_submission_cannot_create_in_wrong_project(self):
        batch = self.propose()
        project, _ = self.make_project('Moved destination')
        self.folder.project = project
        self.folder.save(update_fields=['project'])
        self.decide(batch)
        self.assertEqual(batch.rows.get().status, 'REFUSED')
        self.assertIn('context', batch.rows.get().errors)

    def test_import_line_numbers_match_source_file(self):
        rows = parse_list(SimpleUploadedFile('list.csv', b'file_name,identification_number\nFirst,A\n,\nSecond,B'))
        batch = propose_batch(user=self.supplier, access_id=self.member().pk, rows=rows, line_numbers=rows.line_numbers)
        self.assertEqual(list(batch.rows.values_list('line_number', flat=True)), [2, 4])

    def test_metadata_without_supplier_reference_is_not_made_artificially_mandatory(self):
        batch = self.propose([{'file_name': 'Document without stated supplier ref'}])
        self.assertEqual(batch.rows.get().status, 'PENDING')
        second = self.propose([{'file_name': ' document WITHOUT stated supplier ref '}],
            SupplierProjectAccess.objects.get(user=self.supplier))
        self.assertEqual(second.rows.get().status, 'REFUSED')

    def test_project_choices_use_existing_document_form_configuration(self):
        choices = project_choices(self.member())
        self.assertIn('Drawing', choices['document_types'])
        self.assertTrue(all('label' in choice and 'option_id' in choice for choice in choices['disciplines']))

    def grant_internal_create(self):
        from models.models import Role, Permission, RolePermission, UserRole
        role = Role.objects.create(name='Register approver', company=self.creator.company, scope='company', type='solution')
        permission, _ = Permission.objects.get_or_create(key='documents.create', defaults={'name': 'Create documents', 'module': 'documents'})
        RolePermission.objects.create(role=role, permission=permission)
        UserRole.objects.create(user=self.creator, role=role, company=self.creator.company)

    def test_internal_api_accept_all_never_accepts_invalid_rows(self):
        self.grant_internal_create()
        batch = self.propose([self.metadata(), {'file_name': ''}])
        request = APIRequestFactory().post('/x', {'action': 'ACCEPT'}, format='json')
        force_authenticate(request, user=self.creator)
        # License tables belong to a separate app; only their gate is simulated here.
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            response = InternalBatchDetail.as_view()(request, project_id=self.project.pk, batch_id=batch.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data['accepted']), 1)
        self.assertEqual(batch.rows.get(line_number=2).status, 'REFUSED')

    def test_internal_api_project_access_respects_existing_document_permissions(self):
        self.grant_internal_create()
        request = APIRequestFactory().post('/x', {'email': self.supplier.email, 'supplier': self.supplier_company.pk, 'folder': self.folder.pk}, format='json')
        force_authenticate(request, user=self.creator)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            response = InternalProjectAccess.as_view()(request, project_id=self.project.pk)
        self.assertEqual(response.status_code, 201, response.data)
        denied = APIRequestFactory().get('/x')
        force_authenticate(denied, user=self.supplier)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            response = InternalProjectAccess.as_view()(denied, project_id=self.project.pk)
        self.assertEqual(response.status_code, 404)

    def test_physical_batch_keeps_valid_files_and_refuses_unaccepted_target(self):
        import json
        member = self.member()
        batch = self.propose([self.metadata('A'), self.metadata('B')], member)
        self.decide(batch)
        ids = list(batch.rows.values_list('document_id', flat=True))
        request = APIRequestFactory().post('/x', {'manifest': json.dumps([{'document': ids[0]}, {'document': 999999}, {'document': ids[1]}]),
            'files': [SimpleUploadedFile('A.pdf', b'A'), SimpleUploadedFile('X.pdf', b'X'), SimpleUploadedFile('B.pdf', b'B')]}, format='multipart')
        force_authenticate(request, user=self.supplier)
        response = PortalBulkFiles.as_view()(request, project_id=self.project.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([result['status'] for result in response.data['results']], ['SUBMITTED', 'REFUSED', 'SUBMITTED'])
        self.assertEqual(DocumentRevision.objects.filter(document_id__in=ids).count(), 2)

    def test_physical_batch_cannot_cross_selected_project(self):
        import json
        self.member()
        project, folder = self.make_project('Another project')
        self.member(project=project, folder=folder)
        request = APIRequestFactory().post('/x', {'manifest': json.dumps([{'document': self.doc.pk}]),
            'files': [SimpleUploadedFile('A.pdf', b'changed')]}, format='multipart')
        force_authenticate(request, user=self.supplier)
        response = PortalBulkFiles.as_view()(request, project_id=project.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['results'][0]['status'], 'REFUSED')

    def test_current_unpublished_result_is_not_counted_as_public_review(self):
        self.member()
        sv, _ = self.make_ready_surveillance()
        from project_management.services.supplier_surveillance_review import close_supplier_surveillance
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='REJECTED', closure_comment='SECRET')
        data = dashboard(self.supplier, self.project.pk)
        self.assertEqual(data['kpis']['rejected'], 0)
        self.assertNotIn('SECRET', str(data))

    def test_dashboard_queries_are_bounded_for_multiple_documents(self):
        self.member()
        for index in range(5):
            self.make_supplier_revision()
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        with CaptureQueriesContext(connection) as queries:
            data = dashboard(self.supplier, self.project.pk)
        self.assertEqual(len(data['documents']), 6)
        self.assertLess(len(queries), 14)

    def test_french_template_is_readable_and_uses_project_dropdowns(self):
        from openpyxl import load_workbook
        options = project_choices(self.member())
        book = load_workbook(BytesIO(template_bytes(options, language='fr')))
        self.assertEqual(book['Documents']['A1'].value, 'Titre du document')
        self.assertEqual(len(book['Documents'].data_validations.dataValidation), 3)
        self.assertNotIn('discipline_option_id', [cell.value for cell in book['Project choices'][1]])
        book['Documents'].append(['Nouveau plan', 'FR-01', '', '', 'Pour revue'])
        output = BytesIO()
        book.save(output)
        data = parse_list(SimpleUploadedFile('liste.xlsx', output.getvalue()))
        batch = self.propose(data, SupplierProjectAccess.objects.get(user=self.supplier))
        self.assertEqual(batch.rows.get().metadata['supplier_transmittal_category'], 'FOR_REVIEW')
        self.assertEqual(batch.rows.get().status, 'PENDING')

    def test_malformed_workbook_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            parse_list(SimpleUploadedFile('broken.xlsx', b'not a workbook'))

    def test_old_batches_remain_accessible_with_pagination(self):
        from supplier_portal.models import SupplierProposalBatch
        from supplier_portal.views import PortalBatches
        member = self.member()
        for _ in range(51):
            SupplierProposalBatch.objects.create(project=self.project, folder=self.folder, supplier=self.supplier_company, submitted_by=self.supplier, source='MANUAL')
        request = APIRequestFactory().get('/x', {'page': 2})
        force_authenticate(request, user=self.supplier)
        response = PortalBatches.as_view()(request, access_id=member.pk)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 51)
        self.assertEqual(len(response.data['batches']), 1)
        self.assertIsNone(response.data['next_page'])

    def test_kpis_follow_latest_revision_not_previous_commented_revision(self):
        self.member()
        sv, _ = self.make_ready_surveillance()
        from project_management.services.supplier_surveillance_review import close_supplier_surveillance
        from project_management.services.supplier_portal import publish_return
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='COMMENTED', closure_comment='Internal')
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            publish_return(surveillance=sv, actor=self.creator, public_comment='Correct dimensions')
        self.assertEqual(dashboard(self.supplier, self.project.pk)['kpis']['commented'], 1)
        correction = submit_revision(user=self.supplier, document_id=sv.document_revision.document_id,
            uploaded_file=SimpleUploadedFile('new.pdf', b'changed'), external_revision='B', comment='Corrected')
        current = dashboard(self.supplier, self.project.pk)['kpis']
        self.assertEqual(current['commented'], 0)
        self.assertEqual(current['reviewed_total'], 0)
        from project_management.views.supplier_portal import SupplierPortalDocument
        detail = self.call(SupplierPortalDocument, self.supplier, document_id=sv.document_revision.document_id)
        self.assertEqual(detail.data['current_revision'], correction.revision_id)
        self.assertEqual(detail.data['status'], 'DEPOSITED')
