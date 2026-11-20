from unittest.mock import patch
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from rest_framework.test import APIRequestFactory, force_authenticate
from project_management.test_supplier_surveillance_review import ReviewBase
from project_management.services.supplier_review_annotations import change_annotation, compiled_annotations
from project_management.services.supplier_surveillance_review import assign_reviewer, complete_review, save_responses, close_supplier_surveillance
from project_management.services.supplier_surveillance_evidence import _build_report_sections, render_supplier_surveillance_report_html, build_evidence_metadata
from project_management.views.supplier_review_annotations import SupplierReviewAnnotationViewSet
from project_management.views.supplier_surveillance import SupplierSurveillanceViewSet
from project_management.models import SupplierReviewAnnotation, SupplierSurveillance, SupplierSurveillanceReviewerAssignment
from django.utils import timezone

class PdfReviewTests(ReviewBase):
    def upload_first_file(self, document, name='plan.pdf', **fields):
        result=self.revisions_api('post',document,{'file':SimpleUploadedFile(name,b'%PDF-1.4\nreview test'),**fields})
        self.assertEqual(result.status_code,201)
        return result.data

    def setUp(self):
        super().setUp()
        self.sv=self.make_surveillance(with_two_items=True)
        self.ids=self.snapshot_item_ids(self.sv)
        self.a=self.make_reviewer('pdf-reviewer-a'); self.b=self.make_reviewer('pdf-reviewer-b')
        self.aa=assign_reviewer(surveillance=self.sv,reviewer=self.a,actor=self.creator)
        self.ab=assign_reviewer(surveillance=self.sv,reviewer=self.b,actor=self.creator)

    def comment(self, actor=None, **kwargs):
        values={'item_snapshot_id':self.ids['General observations'],'page':2,'x':0.2,'y':0.4,'text':'Fix dimensions','visibility':'EXTERNAL'}
        values.update(kwargs)
        return change_annotation(surveillance=self.sv,actor=actor or self.a,values=values)

    def test_comments_fill_text_template_and_keep_authorship_and_page_order(self):
        self.comment(page=2,text='Second page')
        self.comment(page=1,text='First page')
        self.comment(actor=self.b,page=1,text='Other reviewer')
        self.aa.refresh_from_db(); self.sv.refresh_from_db()
        self.assertEqual(self.aa.status,'IN_PROGRESS'); self.assertEqual(self.sv.status,'UNDER_REVIEW')
        self.assertEqual(self.aa.responses.get(item_snapshot_id=self.ids['General observations']).text_value,'[Page 1] First page\n[Page 2] Second page')
        item=_build_report_sections(self.sv)[0]['items'][1]
        self.assertEqual(len(item['reviewer_entries']),2)
        self.assertEqual([row.text for row in item['reviewer_entries'][0]['annotations']],['First page','Second page'])
        self.assertEqual(item['reviewer_entries'][0]['display_value'],'')
        self.assertEqual(item['reviewer_entries'][1]['annotations'][0].text,'Other reviewer')

    def test_check_comments_never_invent_compliance_or_overwrite_manual_text(self):
        self.comment(item_snapshot_id=self.ids['Document reference correct'])
        self.assertFalse(self.aa.responses.filter(item_snapshot_id=self.ids['Document reference correct']).exists())
        save_responses(assignment=self.aa,actor=self.a,answers=[{'item_snapshot_id':self.ids['General observations'],'text_value':'Manual decision'}])
        self.comment(text='PDF note')
        self.assertEqual(self.aa.responses.get(item_snapshot_id=self.ids['General observations']).text_value,'Manual decision')
        with self.assertRaises(ValidationError): complete_review(assignment=self.aa,actor=self.a)

    def test_retraction_is_owner_only_and_read_only_after_completion(self):
        row=self.comment()
        with self.assertRaises(Http404): change_annotation(surveillance=self.sv,actor=self.b,retract_id=row.pk)
        change_annotation(surveillance=self.sv,actor=self.a,retract_id=row.pk)
        self.assertTrue(SupplierReviewAnnotation.objects.filter(pk=row.pk,retracted_at__isnull=False).exists())
        self.assertEqual(compiled_annotations(self.aa,self.ids['General observations']),'')
        row=self.comment()
        save_responses(assignment=self.aa,actor=self.a,answers=[{'item_snapshot_id':self.ids['Document reference correct'],'check_value':'COMPLIANT'}])
        complete_review(assignment=self.aa,actor=self.a)
        with self.assertRaises(ValidationError): self.comment()
        with self.assertRaises(ValidationError): change_annotation(surveillance=self.sv,actor=self.a,retract_id=row.pk)
        with self.assertRaises(ValidationError): row.delete()
        with self.assertRaises(ValidationError): SupplierReviewAnnotation.objects.filter(pk=row.pk).update(text='tamper')
        with self.assertRaises(PermissionDenied): self.comment(actor=self.creator)

    def test_pdf_report_compiles_reviewers_under_the_frozen_template_once(self):
        self.comment(text='Unique note A')
        self.comment(actor=self.b,text='Unique note B')
        for reviewer,assignment in [(self.a,self.aa),(self.b,self.ab)]:
            save_responses(assignment=assignment,actor=reviewer,answers=[{'item_snapshot_id':self.ids['Document reference correct'],'check_value':'COMPLIANT'}])
            complete_review(assignment=assignment,actor=reviewer)
        self.sv.refresh_from_db()
        closed=close_supplier_surveillance(surveillance=self.sv,actor=self.creator,result='COMMENTED',closure_comment='')
        html=render_supplier_surveillance_report_html(closed,build_evidence_metadata(closed),generated_at=timezone.now())
        self.assertEqual(html.count('Unique note A'),1); self.assertEqual(html.count('Unique note B'),1)
        self.assertIn('Page 2',html); self.assertIn('General observations',html)

    def test_api_validates_page_coordinates_text_and_document_scope(self):
        def call(values, actor=self.a):
            request=APIRequestFactory().post('/x/',values,format='json'); force_authenticate(request,user=actor)
            with patch('project_management.views.supplier_surveillance_review.get_register_document_or_404',return_value=self.sv.document_revision.document):
                return SupplierReviewAnnotationViewSet.as_view({'post':'add_annotation'},permission_classes=[])(request,surveillance_id=self.sv.pk)
        base={'item_snapshot_id':self.ids['General observations'],'page':1,'x':0.2,'y':0.4,'text':'Note'}
        for invalid in [{'page':0},{'x':1.2},{'y':-0.2},{'text':''},{'item_snapshot_id':999999}]:
            self.assertEqual(call({**base,**invalid}).status_code,400)
        self.assertEqual(call(base,actor=self.creator).status_code,403)
        request=APIRequestFactory().get('/x/');force_authenticate(request,user=self.a)
        with patch('project_management.views.supplier_surveillance_review.get_register_document_or_404',side_effect=Http404):
            result=SupplierReviewAnnotationViewSet.as_view({'get':'annotations'},permission_classes=[])(request,surveillance_id=self.sv.pk)
        self.assertEqual(result.status_code,404)

class ReviewerCreationTests(ReviewBase):
    def call(self,revision,reviewers):
        template=self.make_template()
        request=APIRequestFactory().post('/x/',{'template':template.pk,'reviewers':reviewers},format='json');force_authenticate(request,user=self.creator)
        with patch('project_management.views.supplier_surveillance.get_register_document_or_404',return_value=revision.document),patch('project_management.views.supplier_surveillance.assert_can_update_document'):
            return SupplierSurveillanceViewSet.as_view({'post':'create'},permission_classes=[])(request,document_id=revision.document_id,revision_id=revision.pk)

    def test_template_and_reviewers_are_created_together(self):
        revision=self.make_supplier_revision()
        reviewer=self.make_reviewer('creation-reviewer')
        result=self.call(revision,[reviewer.pk])
        self.assertEqual(result.status_code,201)
        self.assertTrue(SupplierSurveillanceReviewerAssignment.objects.filter(surveillance_id=result.data['id'],reviewer=reviewer).exists())

    def test_invalid_reviewer_rolls_back_the_surveillance(self):
        revision=self.make_supplier_revision()
        outsider=self.make_reviewer('creation-outsider',same_company=False)
        result=self.call(revision,[outsider.pk])
        self.assertEqual(result.status_code,400)
        self.assertFalse(SupplierSurveillance.objects.filter(document_revision=revision).exists())
