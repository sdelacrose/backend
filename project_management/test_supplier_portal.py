from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.test import APIRequestFactory, force_authenticate
from django.test import override_settings
from project_management.test_supplier_surveillance_closure import ClosureBase
from project_management.models import Document, DocumentOrigin, SupplierSurveillance
from project_management.models.supplier_portal import SupplierPortalGrant, SupplierPortalReturn, SupplierPortalSubmission
from project_management.services.supplier_portal import (
    portal_grants, portal_grant, grant_access, revoke_access, publish_return, submit_revision, visible_revisions,
)
from project_management.views.supplier_portal import SupplierPortalDocument, SupplierPortalDownload, SupplierPortalDocuments


@override_settings(SUPPLIER_PORTAL_ENABLED=True)
class SupplierPortalTests(ClosureBase):
    def setUp(self):
        super().setUp()
        company = self.make_company()
        self.creator.company = company
        self.creator.save(update_fields=['company'])
        self.supplier_company.company_profile = company
        self.supplier_company.save(update_fields=['company_profile'])
        self.supplier = self.make_reviewer('external-supplier', same_company=False)
        self.other = self.make_reviewer('other-supplier', same_company=False)
        self.revision = self.make_supplier_revision()
        self.doc = self.revision.document

    def grant(self, document=None, user=None):
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            return grant_access(document=document or self.doc, email=(user or self.supplier).email, actor=self.creator)

    def call(self, view, user, **kwargs):
        request = APIRequestFactory().get('/supplier-portal/')
        if user:
            force_authenticate(request, user=user)
        return view.as_view()(request, **kwargs)

    def test_no_default_access_even_for_company_owner(self):
        self.assertEqual(portal_grants(self.supplier).count(), 0)
        self.assertEqual(portal_grants(self.creator).count(), 0)

    @override_settings(SUPPLIER_PORTAL_ENABLED=False)
    def test_disabled_feature_does_not_access_portal_tables(self):
        with self.assertNumQueries(0):
            self.assertEqual(self.call(SupplierPortalDocuments, self.supplier).status_code, 404)

    def test_only_explicit_document_is_visible(self):
        self.grant()
        other = self.make_supplier_revision().document
        self.assertEqual(list(portal_grants(self.supplier).values_list('document_id', flat=True)), [self.doc.pk])
        with self.assertRaises(Http404):
            portal_grant(self.supplier, other.pk)
        with self.assertRaises(Http404):
            portal_grant(self.other, self.doc.pk)

    def test_unauthenticated_list_is_denied(self):
        self.assertIn(self.call(SupplierPortalDocuments, None).status_code, [401, 403])

    def test_external_grantee_cannot_grant_access(self):
        self.grant()
        with self.assertRaises(PermissionDenied):
            grant_access(document=self.doc, email=self.other.email, actor=self.supplier)

    def test_internal_update_permission_is_required(self):
        with patch('project_management.services.supplier_portal.assert_can_update_document', side_effect=PermissionDenied):
            with self.assertRaises(PermissionDenied):
                grant_access(document=self.doc, email=self.supplier.email, actor=self.creator)

    def test_internal_and_client_documents_cannot_be_shared(self):
        for origin in [DocumentOrigin.INTERNAL, DocumentOrigin.CLIENT]:
            Document.objects.filter(pk=self.doc.pk).update(document_origin=origin)
            with self.assertRaises(ValidationError):
                self.grant()

    def test_revoke_removes_list_detail_and_download_access(self):
        grant = self.grant()
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            revoke_access(grant=grant, actor=self.creator)
        self.assertEqual(portal_grants(self.supplier).count(), 0)
        self.assertEqual(self.call(SupplierPortalDocument, self.supplier, document_id=self.doc.pk).status_code, 404)
        self.assertEqual(self.call(SupplierPortalDownload, self.supplier, document_id=self.doc.pk, revision_id=self.revision.pk).status_code, 404)

    def test_disabled_account_cannot_use_grant(self):
        self.grant()
        self.supplier.is_active = False
        with self.assertRaises(PermissionDenied):
            portal_grant(self.supplier, self.doc.pk)

    def test_changed_supplier_invalidates_access(self):
        self.grant()
        Document.objects.filter(pk=self.doc.pk).update(external_issuer_company=self.client_company)
        self.assertEqual(portal_grants(self.supplier).count(), 0)

    def test_internal_revision_is_hidden_until_published(self):
        grant = self.grant()
        self.assertEqual(visible_revisions(grant).count(), 0)
        response = self.call(SupplierPortalDownload, self.supplier, document_id=self.doc.pk, revision_id=self.revision.pk)
        self.assertEqual(response.status_code, 404)

    def test_upload_records_real_author_and_preserves_previous_bytes(self):
        self.grant()
        result = submit_revision(user=self.supplier, document_id=self.doc.pk,
            uploaded_file=SimpleUploadedFile('supplier-plan_B.pdf', b'modified supplier bytes'), external_revision='B', comment='Corrected drawing')
        self.assertEqual(result.revision.created_by_id, self.supplier.pk)
        self.assertEqual(result.revision.external_revision, 'B')
        self.assertEqual(self.revision.file.read(), b'pdf')
        self.assertEqual(result.revision.file.read(), b'modified supplier bytes')

    def test_second_upload_waits_for_published_review(self):
        self.grant()
        submit_revision(user=self.supplier, document_id=self.doc.pk,
            uploaded_file=SimpleUploadedFile('B.pdf', b'B'), comment='B')
        with self.assertRaises(ValidationError):
            submit_revision(user=self.supplier, document_id=self.doc.pk,
                uploaded_file=SimpleUploadedFile('C.pdf', b'C'), comment='C')
        self.assertEqual(SupplierPortalSubmission.objects.count(), 1)

    def test_oversized_upload_has_no_submission(self):
        from django.test import override_settings
        self.grant()
        with override_settings(SUPPLIER_PORTAL_MAX_FILE_BYTES=2), self.assertRaises(ValidationError):
            submit_revision(user=self.supplier, document_id=self.doc.pk,
                uploaded_file=SimpleUploadedFile('B.pdf', b'large'), comment='B')
        self.assertFalse(SupplierPortalSubmission.objects.exists())

    def test_draft_surveillance_cannot_be_published(self):
        sv = self.make_surveillance()
        with patch('project_management.services.supplier_portal.assert_can_update_document'), self.assertRaises(ValidationError):
            publish_return(surveillance=sv, actor=self.creator, public_comment='Do not expose draft')

    def test_published_return_has_no_internal_comments_or_reviewers(self):
        sv, _ = self.make_ready_surveillance()
        from project_management.services.supplier_surveillance_review import close_supplier_surveillance
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='COMMENTED', closure_comment='INTERNAL CONFIDENTIAL')
        self.grant(document=sv.document_revision.document)
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            publication = publish_return(surveillance=sv, actor=self.creator, public_comment='Correct the dimensions')
        response = self.call(SupplierPortalDocument, self.supplier, document_id=sv.document_revision.document_id)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('INTERNAL CONFIDENTIAL', str(response.data))
        self.assertNotIn('reviewer', str(response.data))
        self.assertNotIn('template_snapshot', str(response.data))
        self.assertEqual(response.data['returns'][0]['comment'], 'Correct the dimensions')
        self.assertTrue(response.data['returns'][0]['new_revision_required'])
        with self.assertRaises(ValidationError):
            publication.save()

    def test_commented_or_rejected_return_requires_public_instructions(self):
        sv, _ = self.make_ready_surveillance()
        from project_management.services.supplier_surveillance_review import close_supplier_surveillance
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='REJECTED', closure_comment='internal')
        with patch('project_management.services.supplier_portal.assert_can_update_document'), self.assertRaises(ValidationError):
            publish_return(surveillance=sv, actor=self.creator, public_comment='   ')

    def test_grant_is_idempotent_and_revocation_preserves_history(self):
        first, second = self.grant(), self.grant()
        self.assertEqual(first.pk, second.pk)
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            revoke_access(grant=first, actor=self.creator)
        third = self.grant()
        self.assertNotEqual(first.pk, third.pk)
        self.assertEqual(SupplierPortalGrant.objects.count(), 2)

    def test_other_company_manager_cannot_publish_or_grant(self):
        with self.assertRaises(PermissionDenied):
            grant_access(document=self.doc, email=self.supplier.email, actor=self.other)

    def test_changed_project_invalidates_existing_grant(self):
        self.grant()
        _, folder = self.make_project('Different project')
        Document.objects.filter(pk=self.doc.pk).update(folder=folder)
        self.assertEqual(portal_grants(self.supplier).count(), 0)

    def test_withdrawn_document_disappears_from_supplier_portal(self):
        from django.utils import timezone
        self.grant()
        Document.objects.filter(pk=self.doc.pk).update(withdrawn_at=timezone.now())
        self.assertEqual(portal_grants(self.supplier).count(), 0)

    def test_rejected_revision_can_be_corrected_after_publication(self):
        self.grant()
        submitted = submit_revision(user=self.supplier, document_id=self.doc.pk,
            uploaded_file=SimpleUploadedFile('B.pdf', b'B'), external_revision='B', comment='B')
        from project_management.services.supplier_surveillance import create_supplier_surveillance
        from project_management.services.supplier_surveillance_review import assign_reviewer, save_responses, complete_review, close_supplier_surveillance
        sv = create_supplier_surveillance(document_revision=submitted.revision, actor=self.creator, template=self.make_template())
        reviewer = self.make_reviewer('supplier-reviewer')
        assignment = assign_reviewer(surveillance=sv, reviewer=reviewer, actor=self.creator)
        answers = [{'item_snapshot_id': item['id'], 'check_value': 'COMPLIANT'}
                   for section in sv.template_snapshot['sections'] for item in section['items'] if item['type'] == 'CHECK']
        save_responses(assignment=assignment, actor=reviewer, answers=answers)
        complete_review(assignment=assignment, actor=reviewer)
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='REJECTED', closure_comment='INTERNAL')
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            publish_return(surveillance=sv, actor=self.creator, public_comment='Correct drawing')
        corrected = submit_revision(user=self.supplier, document_id=self.doc.pk,
            uploaded_file=SimpleUploadedFile('C.pdf', b'C'), external_revision='C', comment='Corrected')
        self.assertGreater(corrected.revision.version_number, submitted.revision.version_number)
        self.assertEqual(SupplierPortalReturn.objects.count(), 1)

    def test_download_is_authenticated_private_and_revocable(self):
        self.grant()
        result = submit_revision(user=self.supplier, document_id=self.doc.pk,
            uploaded_file=SimpleUploadedFile('B.pdf', b'B'), comment='B')
        response = self.call(SupplierPortalDownload, self.supplier, document_id=self.doc.pk, revision_id=result.revision_id)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'B')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(self.call(SupplierPortalDownload, self.other, document_id=self.doc.pk, revision_id=result.revision_id).status_code, 404)

    def test_published_evidence_cannot_be_bulk_rewritten(self):
        with self.assertRaises(ValidationError):
            SupplierPortalReturn.objects.all().update(public_comment='replacement')

    def test_changing_supplier_does_not_disclose_previous_supplier_feedback(self):
        sv, _ = self.make_ready_surveillance()
        from project_management.services.supplier_surveillance_review import close_supplier_surveillance
        close_supplier_surveillance(surveillance=sv, actor=self.creator, result='REVIEWED', closure_comment='internal')
        document = sv.document_revision.document
        with patch('project_management.services.supplier_portal.assert_can_update_document'):
            publish_return(surveillance=sv, actor=self.creator, public_comment='Only for original supplier')
        self.client_company.company_profile = self.creator.company
        self.client_company.save(update_fields=['company_profile'])
        Document.objects.filter(pk=document.pk).update(external_issuer_company=self.client_company)
        self.grant(document=document, user=self.other)
        response = self.call(SupplierPortalDocument, self.other, document_id=document.pk)
        self.assertEqual(response.data['returns'], [])
        self.assertEqual(response.data['revisions'], [])

    def test_foreign_document_upload_returns_404(self):
        with self.assertRaises(Http404):
            submit_revision(user=self.other, document_id=self.doc.pk,
                uploaded_file=SimpleUploadedFile('B.pdf', b'B'), comment='B')
