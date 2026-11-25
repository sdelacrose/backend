"""Document UX: tenant boundaries, dedicated folders, recipient-bound publications and client follow-up."""
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from django.test import override_settings
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.test_transmittal import TransmittalTestBase, make_user
from project_management.models import ContactCompany, Folder, Document, Correspondence, ClientIssue, ClientSurveillance, ClientSurveillanceEvent
from project_management.models.correspondence import CorrespondenceAttachment
from project_management.models.supplier_portal import SupplierPortalGrant
from project_management.services.transmittal_items import add_transmittal_item
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.supplier_portal import portal_grant, grant_access
from project_management.services.document_history_events import list_document_history_events
from project_management.views.client_surveillance import ClientSurveillanceView
from supplier_portal.register import grant_project, revoke_project
from supplier_portal.queries import documents_for
from supplier_portal.models import SupplierProjectAccess, SupplierSharedExchange
from supplier_portal.shared import InternalShared, PortalShared, PortalSharedFile

@override_settings(SUPPLIER_REGISTER_ENABLED=True, SUPPLIER_PORTAL_ENABLED=True)
class FolderAndSharedTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.temp = TemporaryDirectory(prefix='links-document-ux-')
        self.addCleanup(self.temp.cleanup)
        config = override_settings(MEDIA_ROOT=self.temp.name + "/media", SUPPLIER_SHARED_STORAGE_ROOT=self.temp.name + "/private"); config.enable(); self.addCleanup(config.disable)
        self.a = ContactCompany.objects.create(company_profile=self.company, company_name='Supplier A')
        self.b = ContactCompany.objects.create(company_profile=self.company, company_name='Supplier B')
        self.user_a, self.user_b = make_user('supplier-a@local.invalid'), make_user('supplier-b@local.invalid')
        with patch('supplier_portal.register.assert_project_manager'):
            self.access_a = grant_project(actor=self.owner, project=self.project, supplier=self.a, email=self.user_a.email)
            self.access_b = grant_project(actor=self.owner, project=self.project, supplier=self.b, email=self.user_b.email)

    def call(self, view, user, method='get', body=None, **kwargs):
        request = getattr(APIRequestFactory(), method)('/x/', body or {}, format='json')
        if user: force_authenticate(request, user=user)
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            return view.as_view()(request, **kwargs)

    def supplied(self, folder, supplier):
        doc = self.make_document(folder=folder, origin='SUPPLIER')
        doc.external_issuer_company = supplier; doc.save(update_fields=['external_issuer_company'])
        return doc

    def correspondence(self, supplier=None, project=None, status='ISSUED', direction='OUT'):
        return Correspondence.objects.create(project=project or self.project, recipient_company=supplier or self.a,
            created_by=self.owner, subject='Private supplier exchange', message='For the selected supplier only',
            reference='COR-UX-' + str(Correspondence.objects.count()+1), status=status, direction=direction)

    def publish(self, source, supplier=None, kind='CORRESPONDENCE'):
        with patch('supplier_portal.views.assert_project_manager'), patch('project_management.permissions.CorrespondencePermission._check_action_or_full_access', return_value=True), patch('project_management.permissions.TransmittalPermission._check_action_or_full_access', return_value=True):
            return self.call(InternalShared, self.owner, 'post', {'supplier': (supplier or self.a).pk, 'kind': kind, 'source': source.pk}, project_id=self.project.pk)

    def test_new_access_creates_named_folder_and_reuses_it_for_another_account(self):
        self.assertEqual(self.access_a.folder.name, self.a.company_name)
        self.assertEqual(self.access_a.folder.system_key, f'supplier:{self.a.pk}')
        another = make_user('another-a@local.invalid')
        with patch('supplier_portal.register.assert_project_manager'):
            again = grant_project(actor=self.owner, project=self.project, supplier=self.a, email=self.user_a.email)
            other = grant_project(actor=self.owner, project=self.project, supplier=self.a, email=another.email)
        self.assertEqual(again.pk, self.access_a.pk)
        self.assertEqual(other.folder_id, self.access_a.folder_id)
        self.assertEqual(Folder.objects.filter(project=self.project, system_key=f'supplier:{self.a.pk}').count(), 1)

    def test_folder_and_subtree_are_enforced_for_lists_and_direct_document_access(self):
        own = self.supplied(self.access_a.folder, self.a)
        child = Folder.objects.create(project=self.project, parent=self.access_a.folder, name='Drawings')
        nested = self.supplied(child, self.a)
        outside = self.supplied(self.folder, self.a)
        wrong_supplier = self.supplied(self.access_a.folder, self.b)
        self.assertEqual(set(documents_for(self.user_a, self.project.pk).values_list('id', flat=True)), {own.pk, nested.pk})
        self.assertEqual(portal_grant(self.user_a, nested.pk).document_id, nested.pk)
        for user, doc in ((self.user_a, outside), (self.user_a, wrong_supplier), (self.user_b, own)):
            with self.assertRaises(Http404): portal_grant(user, doc.pk)

    def test_legacy_per_document_grants_are_retained_but_do_not_authorize_access(self):
        doc = self.supplied(self.folder, self.a)
        legacy = SupplierPortalGrant.objects.create(document=doc, supplier=self.a, project=self.project, user=self.user_a, granted_by=self.owner)
        with self.assertRaises(Http404): portal_grant(self.user_a, doc.pk)
        self.assertTrue(SupplierPortalGrant.objects.filter(pk=legacy.pk).exists())
        with self.assertRaises(PermissionDenied): grant_access(document=doc, actor=self.owner, email=self.user_a.email)

    def test_external_user_cannot_grant_an_access(self):
        with self.assertRaises(PermissionDenied): grant_project(actor=self.user_b, project=self.project, supplier=self.a, email=self.user_b.email)

    def test_supplier_a_exchange_and_files_are_invisible_to_b_even_with_known_ids(self):
        source = self.correspondence()
        CorrespondenceAttachment.objects.create(correspondence=source, file=SimpleUploadedFile('private.txt', b'only supplier A'), file_name='private.txt', uploaded_by=self.owner)
        published = self.publish(source); self.assertEqual(published.status_code, 201, published.data)
        exchange = SupplierSharedExchange.objects.get(pk=published.data['id']); file = exchange.files.get()
        with self.assertRaises(ValueError): _ = file.file.url
        allowed = self.call(PortalSharedFile, self.user_a, access_id=self.access_a.pk, exchange_id=exchange.pk, file_id=file.pk)
        self.assertEqual(allowed.status_code, 200); self.assertEqual(b''.join(allowed.streaming_content), b'only supplier A'); allowed.file_to_stream.close()
        self.assertEqual(len(self.call(PortalShared, self.user_a, access_id=self.access_a.pk).data['results']), 1)
        self.assertEqual(self.call(PortalShared, self.user_b, access_id=self.access_b.pk).data['results'], [])
        for user, access in ((self.user_b, self.access_b), (self.user_b, self.access_a)):
            denied = self.call(PortalSharedFile, user, access_id=access.pk, exchange_id=exchange.pk, file_id=file.pk)
            self.assertEqual(denied.status_code, 404)
        anonymous = self.call(PortalSharedFile, None, access_id=self.access_a.pk, exchange_id=exchange.pk, file_id=file.pk)
        self.assertIn(anonymous.status_code, (401, 403))
        revoked = self.access_a; revoked.revoked_at = exchange.published_at; revoked.save(update_fields=['revoked_at'])
        self.assertEqual(self.call(PortalSharedFile, self.user_a, access_id=revoked.pk, exchange_id=exchange.pk, file_id=file.pk).status_code, 404)

    def test_identical_supplier_names_never_merge_their_access_folders(self):
        same_name = ContactCompany.objects.create(company_profile=self.company, company_name=self.a.company_name)
        other_user = make_user('same-name@local.invalid')
        with patch('supplier_portal.register.assert_project_manager'):
            access = grant_project(actor=self.owner, project=self.project, supplier=same_name, email=other_user.email)
        self.assertNotEqual(access.folder_id, self.access_a.folder_id)
        self.supplied(self.access_a.folder, self.a)
        self.assertFalse(documents_for(other_user, self.project.pk).exists())

    def test_wrong_recipient_other_project_draft_or_incoming_cannot_be_published(self):
        for source in (self.correspondence(supplier=self.b), self.correspondence(project=self.other_project), self.correspondence(status='DRAFT'), self.correspondence(direction='IN')):
            response = self.publish(source)
            self.assertEqual(response.status_code, 404, response.data)
        self.assertEqual(SupplierSharedExchange.objects.count(), 0)

    def test_external_user_cannot_publish_or_list_internal_choices(self):
        source = self.correspondence()
        response = self.call(InternalShared, self.user_b, 'post', {'supplier': self.a.pk, 'kind': 'CORRESPONDENCE', 'source': source.pk}, project_id=self.project.pk)
        self.assertEqual(response.status_code, 404)

    def test_publication_is_a_frozen_copy_and_cannot_be_published_twice(self):
        source = self.correspondence()
        attachment = CorrespondenceAttachment.objects.create(correspondence=source, file=SimpleUploadedFile('first.txt', b'first'), file_name='first.txt', uploaded_by=self.owner)
        response = self.publish(source); self.assertEqual(response.status_code, 201, response.data)
        attachment.file.save('changed.txt', SimpleUploadedFile('changed.txt', b'changed'))
        stored = SupplierSharedExchange.objects.get(pk=response.data['id']).files.get()
        with stored.file.open('rb') as file: self.assertEqual(file.read(), b'first')
        self.assertEqual(self.publish(source).status_code, 400)

    def test_transmittal_to_a_supplier_publishes_only_its_exact_revisions(self):
        revision = self.make_approved_internal_revision()
        transmittal = self.create_transmittal()
        transmittal.external_company = self.a; transmittal.external_contact = None; transmittal.save()
        add_transmittal_item(transmittal, revision, 'FOR_REVIEW', self.owner)
        transmittal = finalize_transmittal(transmittal, self.owner)
        response = self.publish(transmittal, kind='TRANSMITTAL')
        self.assertEqual(response.status_code, 201, response.data)
        row = SupplierSharedExchange.objects.get(pk=response.data['id'])
        self.assertEqual(row.supplier_id, self.a.pk)
        self.assertEqual(row.snapshot['reference'], transmittal.transmittal_number)
        self.assertEqual(row.files.count(), 1)

class ClientFollowUpTests(TransmittalTestBase):
    def setUp(self):
        super().setUp()
        self.revision = self.make_approved_internal_revision()
        self.doc = self.revision.document
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, self.revision, 'FOR_REVIEW', self.owner)
        self.sent = finalize_transmittal(transmittal, self.owner)
        self.issue = self.sent.items.get().client_issue

    def call(self, method='get', issue=None, body=None, user=None, relaxed=True):
        request = getattr(APIRequestFactory(), method)('/x/', body or {}, format='json'); force_authenticate(request, user=user or self.owner)
        kwargs = {'document_id': self.doc.pk}
        if issue is not None: kwargs['issue_id'] = issue
        with patch('ecpmpapp.permissions.LicensePermission.has_permission', return_value=True):
            if relaxed:
                with patch('project_management.views.client_surveillance.get_register_document_or_404', return_value=self.doc), patch('project_management.views.client_surveillance.assert_can_update_document'), patch('project_management.views.client_surveillance.can_manage_client_surveillance', return_value=True):
                    return ClientSurveillanceView.as_view()(request, **kwargs)
            return ClientSurveillanceView.as_view()(request, **kwargs)

    def test_get_is_read_only_and_creation_requires_an_actual_client_distribution(self):
        response = self.call(); self.assertEqual(response.status_code, 200)
        self.assertEqual(ClientSurveillance.objects.count(), 0)
        self.assertIsNone(response.data['results'][0]['surveillance'])
        self.assertEqual(self.call('post', issue=self.issue.pk+9999).status_code, 404)
        created = self.call('post', issue=self.issue.pk); self.assertEqual(created.status_code, 201, created.data)
        self.assertEqual(self.call('post', issue=self.issue.pk).status_code, 200)
        self.assertEqual(ClientSurveillanceEvent.objects.count(), 1)

    def test_real_result_dates_and_events_are_recorded_without_overwriting_source_revision(self):
        # GED-WF.4.3: the result is a recorded client return, retained explicitly at closure (never typed at closure).
        from django.utils import timezone
        from project_management.services.client_surveillance_return import record_client_return
        source_label = self.revision.revision_label
        self.call('post', issue=self.issue.pk)
        self.assertEqual(self.call('patch', issue=self.issue.pk, body={'status': 'CLOSED', 'result': 'REVIEWED'}).status_code, 400)
        for status in ('UNDER_REVIEW', 'READY_FOR_CLOSURE'):
            response = self.call('patch', issue=self.issue.pk, body={'status': status}); self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.call('patch', issue=self.issue.pk, body={'status': 'CLOSED'}).status_code, 400)  # no retained return
        record = record_client_return(self.issue, self.owner, category='COMMENTED', received_at=timezone.now(), source_channel='EMAIL', comment='Revise drawing')
        response = self.call('patch', issue=self.issue.pk, body={'status': 'CLOSED', 'retained_return': record.pk})
        self.assertEqual(response.status_code, 200, response.data); self.assertIsNotNone(response.data['closed_at'])
        self.assertEqual((response.data['result'], response.data['comment']), ('COMMENTED', 'Revise drawing'))
        self.issue.refresh_from_db(); self.revision.refresh_from_db()
        self.assertIsNone(self.issue.client_revision_label); self.assertEqual(self.revision.revision_label, source_label)
        self.assertEqual(self.call('patch', issue=self.issue.pk, body={'status': 'DRAFT'}).status_code, 400)
        self.assertEqual(ClientSurveillanceEvent.objects.count(), 5)  # opened by hand, 2 steps, the recorded return, the closure
        self.assertTrue(any(e['event_group'] == 'CLIENT_SURVEILLANCE' for e in list_document_history_events(self.doc)))

    def test_another_tenant_cannot_read_or_modify_client_follow_up(self):
        self.call('post', issue=self.issue.pk)
        for method in ('get', 'post', 'patch'):
            response = self.call(method, issue=self.issue.pk, body={'status': 'UNDER_REVIEW'}, user=self.other_owner, relaxed=False)
            self.assertIn(response.status_code, (403, 404), response.data)
        self.assertEqual(ClientSurveillance.objects.get().status, 'DRAFT')

    def test_surveillance_cannot_change_the_issued_client_revision(self):
        self.call('post', issue=self.issue.pk)
        ClientIssue.objects.filter(pk=self.issue.pk).update(client_revision_label='C1')
        response = self.call('patch', issue=self.issue.pk, body={'status': 'UNDER_REVIEW', 'client_revision': 'C2'})
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('client_revision', response.data)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.client_revision_label, 'C1')
        self.assertEqual(ClientSurveillance.objects.get(client_issue=self.issue).status, 'DRAFT')
        self.assertEqual(ClientSurveillanceEvent.objects.count(), 1)
