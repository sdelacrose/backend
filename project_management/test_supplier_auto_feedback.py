from unittest.mock import patch
from django.test import override_settings
from django.core.exceptions import ValidationError
from django.http import Http404
from project_management.test_supplier_surveillance_closure import ClosureBase, Result
from project_management.services.supplier_surveillance_review import close_supplier_surveillance
from project_management.models.supplier_portal import SupplierPortalReturn
from project_management.services.supplier_portal import portal_grant
from supplier_portal.models import SupplierProjectAccess
from project_management.models import ContactCompany

@override_settings(SUPPLIER_REGISTER_ENABLED=True)
class AutoFeedbackTests(ClosureBase):
    def assert_closure_publishes_only_to_named_supplier(self, result):
        surveillance,_=self.make_ready_surveillance(reviewer_names=('auto-feedback',))
        document=surveillance.document_revision.document
        closed=close_supplier_surveillance(surveillance=surveillance,actor=self.creator,result=result,closure_comment='')
        publication=SupplierPortalReturn.objects.get(surveillance=closed)
        self.assertEqual(publication.supplier_id,document.external_issuer_company_id)
        self.assertEqual(publication.project_id,document.folder.project_id)
        self.assertEqual(publication.public_comment,'')
        self.assertEqual(publication.result,result)
        self.assertEqual(publication.new_revision_required,result != Result.REVIEWED)
        self.assertFalse(SupplierProjectAccess.objects.exists())
        with self.assertRaises(ValidationError):
            close_supplier_surveillance(surveillance=closed,actor=self.creator,result=result,closure_comment='')
        self.assertEqual(SupplierPortalReturn.objects.filter(surveillance=closed).count(),1)

    def test_reviewed_publishes_without_comment(self):
        self.assert_closure_publishes_only_to_named_supplier(Result.REVIEWED)

    def test_commented_publishes_without_comment(self):
        self.assert_closure_publishes_only_to_named_supplier(Result.COMMENTED)

    def test_rejected_publishes_without_comment(self):
        self.assert_closure_publishes_only_to_named_supplier(Result.REJECTED)

    def test_failure_rolls_back_closure_and_internal_review_answers_are_not_published(self):
        surveillance,_=self.make_ready_surveillance(reviewer_names=('auto-rollback',))
        with patch('project_management.services.supplier_portal.publish_closed_feedback',side_effect=ValidationError('publication failure')):
            with self.assertRaises(ValidationError):
                close_supplier_surveillance(surveillance=surveillance,actor=self.creator,result=Result.COMMENTED,closure_comment='Public summary')
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.status,'READY_FOR_CLOSURE')
        self.assertFalse(SupplierPortalReturn.objects.filter(surveillance=surveillance).exists())
        closed=close_supplier_surveillance(surveillance=surveillance,actor=self.creator,result=Result.COMMENTED,closure_comment='Public summary')
        self.assertEqual(SupplierPortalReturn.objects.get(surveillance=closed).public_comment,'Public summary')

    def test_existing_supplier_access_boundaries_still_apply_after_auto_publication(self):
        surveillance,_=self.make_ready_surveillance(reviewer_names=('auto-scope',))
        document=surveillance.document_revision.document
        close_supplier_surveillance(surveillance=surveillance,actor=self.creator,result=Result.REVIEWED,closure_comment='')
        a=self.make_reviewer('feedback-supplier-a'); b=self.make_reviewer('feedback-supplier-b')
        other=ContactCompany.objects.create(company_profile=self.make_company(),company_name='Unrelated supplier')
        for user,supplier in ((a,document.external_issuer_company),(b,other)):
            SupplierProjectAccess.objects.create(project=document.folder.project,supplier=supplier,folder=document.folder,user=user,granted_by=self.creator)
        self.assertIsNotNone(portal_grant(a,document.pk))
        with self.assertRaises(Http404): portal_grant(b,document.pk)
