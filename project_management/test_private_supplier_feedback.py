import json
from unittest.mock import patch
from django.test import override_settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template.loader import render_to_string
from rest_framework.test import APIRequestFactory,force_authenticate
from project_management.test_supplier_pdf_review import PdfReviewTests
from project_management.services.supplier_surveillance_review import save_responses,complete_review,close_supplier_surveillance,assign_reviewer
from project_management.services.supplier_review_annotations import change_annotation
from project_management.services.supplier_portal import submit_revision
from project_management.models.supplier_portal import SupplierPortalReturn
from supplier_portal.models import SupplierProjectAccess,SupplierFeedbackAnswer,SupplierFeedbackAssessment
from supplier_portal.feedback import published_feedback,validate_answers,answers_data
from supplier_portal.feedback_views import PortalFeedback
from project_management.models import ContactCompany

@override_settings(SUPPLIER_REGISTER_ENABLED=True,SUPPLIER_PORTAL_ENABLED=True)
class PrivateFeedbackTests(PdfReviewTests):
    def publish(self):
        for user,assignment in ((self.a,self.aa),(self.b,self.ab)):
            save_responses(assignment=assignment,actor=user,answers=[{'item_snapshot_id':self.ids['Document reference correct'],'check_value':'COMPLIANT'}])
            complete_review(assignment=assignment,actor=user)
        self.sv.refresh_from_db()
        closed=close_supplier_surveillance(surveillance=self.sv,actor=self.creator,result='COMMENTED',closure_comment='Supplier summary')
        return SupplierPortalReturn.objects.get(surveillance=closed)

    def access(self):
        doc=self.sv.document_revision.document
        supplier=self.make_reviewer('portal-feedback-owner')
        other=self.make_reviewer('portal-feedback-other')
        other_company=ContactCompany.objects.create(company_profile=self.make_company(),company_name='Other vendor')
        access=SupplierProjectAccess.objects.create(user=supplier,supplier=doc.external_issuer_company,project=doc.folder.project,folder=doc.folder,granted_by=self.creator)
        SupplierProjectAccess.objects.create(user=other,supplier=other_company,project=doc.folder.project,folder=doc.folder,granted_by=self.creator)
        return supplier,other,access

    def test_private_tags_are_excluded_from_text_fields_public_snapshot_and_report(self):
        self.comment(text='CONFIDENTIAL internal reviewer discussion',visibility='INTERNAL',mentioned_reviewer_id=self.b.pk)
        external=self.comment(text='PUBLIC adjust dimension')
        response=self.aa.responses.get(item_snapshot_id=self.ids['General observations'])
        self.assertNotIn('CONFIDENTIAL',response.text_value)
        publication=self.publish()
        content=published_feedback(publication)
        self.assertEqual([row['id'] for row in content['comments']],[external.pk])
        self.assertNotIn('CONFIDENTIAL',json.dumps(content))
        self.assertNotIn('mentioned_reviewer',json.dumps(content))
        html=render_to_string('supplier_public_feedback.html',{'sheet':content,'answers':[]})
        self.assertNotIn('CONFIDENTIAL',html);self.assertIn('PUBLIC adjust dimension',html)
        self.assertIn('break-before:page',html)
        self.assertEqual(content['comments'][0]['item'],'General observations')

    def test_default_private_and_tag_validation(self):
        row=change_annotation(surveillance=self.sv,actor=self.a,values={'item_snapshot_id':self.ids['General observations'],'page':1,'x':0.1,'y':0.2,'text':'Private by default'})
        self.assertEqual(row.visibility,'INTERNAL')
        self.assertFalse(self.aa.responses.exists())
        with self.assertRaises(ValidationError): self.comment(visibility='EXTERNAL',mentioned_reviewer_id=self.b.pk)
        with self.assertRaises(ValidationError): self.comment(visibility='INTERNAL',mentioned_reviewer_id=self.creator.pk)
        self.assertEqual(published_feedback(self.publish())['comments'],[])

    def test_supplier_must_answer_each_external_comment_before_new_revision(self):
        row=self.comment(text='Correct the dimension')
        publication=self.publish();supplier,other,access=self.access()
        doc=self.sv.document_revision.document;before=doc.revisions.count()
        with self.assertRaises(ValidationError):
            submit_revision(user=supplier,document_id=doc.pk,uploaded_file=SimpleUploadedFile('next.pdf',b'%PDF-1.4'),external_revision='B',comment='Changes')
        self.assertEqual(doc.revisions.count(),before)
        for answers in ([{'comment_id':row.pk+999,'disposition':'ADDRESSED','response':'Done'}],[{'comment_id':row.pk,'disposition':'ADDRESSED','response':''}]):
            with self.assertRaises(ValidationError): validate_answers(publication,answers)
        submission=submit_revision(user=supplier,document_id=doc.pk,uploaded_file=SimpleUploadedFile('next.pdf',b'%PDF-1.4'),external_revision='B',comment='Changes',feedback_responses=[{'comment_id':row.pk,'disposition':'PARTIAL','response':'Partly updated, clarification needed'}])
        answer=SupplierFeedbackAnswer.objects.get(submission=submission)
        self.assertEqual(answer.publication,publication);self.assertEqual(answer.comment_id,row.pk)
        self.assertEqual(answer.responded_by,supplier);self.assertEqual(submission.revision.external_revision,'B')
        answer.response='tamper'
        with self.assertRaises(ValidationError): answer.save()
        SupplierFeedbackAssessment.objects.create(answer=answer,accepted=False,comment='Private assessment note',assessed_by=self.creator)
        self.assertNotIn('Private assessment note',json.dumps(answers_data(publication),default=str))
        self.assertIn('Private assessment note',json.dumps(answers_data(publication,include_internal=True),default=str))

    def test_portal_sheet_scoped_to_named_supplier_project_document_and_revocation(self):
        self.comment(text='External only');self.comment(text='PRIVATE NOTE',visibility='INTERNAL')
        publication=self.publish();supplier,other,access=self.access();doc=self.sv.document_revision.document
        def call(user,publication_id=publication.pk):
            request=APIRequestFactory().get('/x/');force_authenticate(request,user=user)
            return PortalFeedback.as_view()(request,document_id=doc.pk,publication_id=publication_id)
        response=call(supplier)
        from supplier_portal.queries import dashboard
        self.assertTrue(dashboard(supplier,doc.folder.project_id)['documents'][0]['feedback_required'])
        self.assertEqual(response.status_code,200);self.assertNotIn('PRIVATE NOTE',json.dumps(response.data,default=str))
        self.assertEqual(call(other).status_code,404);self.assertEqual(call(supplier,publication.pk+999).status_code,404)
        from django.utils import timezone
        access.revoked_at=timezone.now();access.save(update_fields=['revoked_at'])
        self.assertEqual(call(supplier).status_code,404)

    def test_internal_report_requires_auth_and_old_public_media_route_is_blocked(self):
        from project_management.services.supplier_surveillance_evidence import generate_supplier_surveillance_evidence
        from project_management.views.supplier_surveillance import InternalSupplierEvidenceDownload, serialize_evidence_pdf
        from django.test import Client
        publication=self.publish()
        evidence=generate_supplier_surveillance_evidence(surveillance=publication.surveillance,actor=self.creator)
        with self.assertRaises(ValueError): _=evidence.file.url
        self.assertIn('/evidence/download',serialize_evidence_pdf(evidence)['url'])
        self.assertEqual(Client().get('/media/'+evidence.file.name).status_code,404)
        request=APIRequestFactory().get('/x/');force_authenticate(request,user=self.creator)
        with patch('project_management.views.supplier_surveillance.get_register_document_or_404',return_value=self.sv.document_revision.document):
            response=InternalSupplierEvidenceDownload.as_view()(request,surveillance_id=self.sv.pk)
        self.assertEqual(response.status_code,200);response.file_to_stream.close()
        request=APIRequestFactory().get('/x/');force_authenticate(request,user=self.a)
        from django.http import Http404
        with patch('project_management.views.supplier_surveillance.get_register_document_or_404',side_effect=Http404):
            response=InternalSupplierEvidenceDownload.as_view()(request,surveillance_id=self.sv.pk)
        self.assertEqual(response.status_code,404)

    def test_next_review_traces_supplier_responses_and_private_assessment_history(self):
        from supplier_portal.feedback_views import InternalSurveillanceFeedback
        from project_management.services.supplier_surveillance import create_supplier_surveillance
        row=self.comment(text='External correction');publication=self.publish();supplier,_,_=self.access();document=self.sv.document_revision.document
        submission=submit_revision(user=supplier,document_id=document.pk,uploaded_file=SimpleUploadedFile('next.pdf',b'%PDF-1.4'),external_revision='B',comment='Corrected',feedback_responses=[{'comment_id':row.pk,'disposition':'ADDRESSED','response':'Corrected on page two'}])
        next_review=create_supplier_surveillance(document_revision=submission.revision,actor=self.creator,template=self.make_template())
        assignment=assign_reviewer(surveillance=next_review,reviewer=self.a,actor=self.creator)
        answer=SupplierFeedbackAnswer.objects.get(submission=submission)
        def call(method,values=None):
            request=getattr(APIRequestFactory(),method)('/x/',values or {},format='json');force_authenticate(request,user=self.a)
            with patch('supplier_portal.feedback_views.get_register_document_or_404',return_value=document), patch('ecpmpapp.permissions.LicensePermission.has_permission',return_value=True):
                return InternalSurveillanceFeedback.as_view()(request,surveillance_id=next_review.pk)
        self.assertEqual(call('get').data['incoming'][0]['answers'][0]['response'],'Corrected on page two')
        self.assertEqual(call('post',{'answer':answer.pk,'accepted':False,'comment':'PRIVATE verification note'}).status_code,201)
        self.assertEqual(call('post',{'answer':answer.pk,'accepted':True,'comment':'Private second check'}).status_code,201)
        data=call('get').data['incoming'][0]['answers'][0]
        self.assertEqual(len(data['assessment_history']),2)
        self.assertTrue(data['assessment']['accepted'])
        self.assertNotIn('PRIVATE verification note',json.dumps(answers_data(publication),default=str))
        self.assertNotIn('Private second check',json.dumps(answers_data(publication),default=str))
        snapshot_id=next_review.template_snapshot['sections'][0]['items'][0]['id']
        save_responses(assignment=assignment,actor=self.a,answers=[{'item_snapshot_id':snapshot_id,'check_value':'COMPLIANT'}])
        complete_review(assignment=assignment,actor=self.a)
        self.assertEqual(call('post',{'answer':answer.pk,'accepted':False}).status_code,400)

    def test_supplier_pdf_is_a_separate_protected_recapitulative_sheet(self):
        self.comment(text='EXTERNAL supplier action');self.comment(text='DO NOT SHARE',visibility='INTERNAL')
        publication=self.publish();supplier,_,_=self.access();document=self.sv.document_revision.document
        request=APIRequestFactory().get('/x/',{'download':'pdf'});force_authenticate(request,user=supplier)
        response=PortalFeedback.as_view()(request,document_id=document.pk,publication_id=publication.pk)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response['Content-Type'],'application/pdf')
        self.assertEqual(response.file_to_stream.read(5),b'%PDF-');response.file_to_stream.close()
