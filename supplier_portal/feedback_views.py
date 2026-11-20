from io import BytesIO
from django.http import Http404, FileResponse
from django.template.loader import render_to_string
from django.db import transaction
from rest_framework.response import Response
from rest_framework import serializers
from project_management.models import DocumentRevision, SupplierSurveillance
from project_management.models.supplier_portal import SupplierPortalReturn
from project_management.services.supplier_portal import portal_grant, assert_internal_manager
from project_management.services.document_register import get_register_document_or_404, assert_can_update_document
from project_management.views.supplier_portal import SupplierPortalView, business_errors
from supplier_portal.models import SupplierFeedbackAnswer, SupplierFeedbackAssessment
from supplier_portal.feedback import public_feedback, published_feedback, answers_data
from supplier_portal.views import InternalRegisterView

class PortalFeedback(SupplierPortalView):
    def get(self,request,document_id,publication_id):
        grant=portal_grant(request.user,document_id)
        publication=SupplierPortalReturn.objects.filter(pk=publication_id,surveillance__document_revision__document_id=document_id,
            project_id=grant.project_id,supplier_id=grant.supplier_id).select_related('surveillance__document_revision__document').first()
        if not publication: raise Http404
        content=published_feedback(publication)
        answers=answers_data(publication)
        if request.query_params.get('download')=='pdf':
            from weasyprint import HTML
            html=render_to_string('supplier_public_feedback.html',{'sheet':content,'answers':answers})
            result=FileResponse(BytesIO(HTML(string=html).write_pdf()),as_attachment=True,filename=content['reference']+'-supplier-feedback.pdf',content_type='application/pdf')
            result['Cache-Control']='private, no-store';result['X-Content-Type-Options']='nosniff'
            return result
        return Response({'publication':publication.pk,'document_id':document_id,'project_id':grant.project_id,'revision':publication.surveillance.document_revision_id,
            'sheet':content,'answers':answers})

class InternalSurveillanceFeedback(InternalRegisterView):
    def resolve(self,request,surveillance_id):
        surveillance=SupplierSurveillance.objects.select_related('document_revision__document__folder').filter(pk=surveillance_id).first()
        if not surveillance: raise Http404
        document=get_register_document_or_404(request,document_id=surveillance.document_revision.document_id)
        return surveillance,document

    def get(self,request,surveillance_id):
        surveillance,document=self.resolve(request,surveillance_id)
        publication=SupplierPortalReturn.objects.filter(surveillance=surveillance).first()
        incoming=[]
        sources=SupplierPortalReturn.objects.filter(answers__submission__revision=surveillance.document_revision,
            supplier_id=document.external_issuer_company_id,project_id=document.folder.project_id).distinct()
        for source in sources:
            incoming.append({'publication':source.pk,'sheet':published_feedback(source),'answers':[
                row for row in answers_data(source,include_internal=True) if row['revision']==surveillance.document_revision_id]})
        can_assess=surveillance.reviewer_assignments.filter(reviewer=request.user).exclude(status='COMPLETED').exists()
        if not can_assess:
            from rest_framework.exceptions import PermissionDenied
            try:
                assert_can_update_document(request.user,document)
                can_assess=True
            except PermissionDenied: pass
        return Response({'can_assess':can_assess and surveillance.status not in ('READY_FOR_CLOSURE','CLOSED'), 'sheet':published_feedback(publication) if publication else public_feedback(surveillance),
            'published':bool(publication),'incoming':incoming})

    @business_errors
    def post(self,request,surveillance_id):
        surveillance,document=self.resolve(request,surveillance_id)
        if surveillance.status in ('READY_FOR_CLOSURE','CLOSED'):
            return Response({'detail':'The completed review is read-only.'},status=400)
        assigned=surveillance.reviewer_assignments.filter(reviewer=request.user).exclude(status='COMPLETED').exists()
        if not assigned: assert_can_update_document(request.user,document)
        class Input(serializers.Serializer):
            answer=serializers.IntegerField(min_value=1)
            accepted=serializers.BooleanField()
            comment=serializers.CharField(max_length=10000,allow_blank=True,required=False,default='')
        data=Input(data=request.data);data.is_valid(raise_exception=True)
        with transaction.atomic():
            locked=SupplierSurveillance.objects.select_for_update().get(pk=surveillance.pk)
            if locked.status in ('READY_FOR_CLOSURE','CLOSED'): return Response({'detail':'The review is read-only.'},status=400)
            answer=SupplierFeedbackAnswer.objects.filter(pk=data.validated_data['answer'],submission__revision=locked.document_revision,
                publication__supplier_id=document.external_issuer_company_id,publication__project_id=document.folder.project_id).first()
            if not answer: raise Http404
            assessment=SupplierFeedbackAssessment.objects.create(answer=answer,assessed_by=request.user,
                accepted=data.validated_data['accepted'],comment=data.validated_data['comment'])
            from project_management.services.supplier_surveillance_review import record_event
            record_event(locked,'RESPONSE_UPDATED',request.user,metadata={'supplier_feedback_answer_id':answer.pk,'assessment_id':assessment.pk,'accepted':assessment.accepted})
        return Response({'id':assessment.pk},status=201)

class InternalSupplierPreview(InternalRegisterView):
    def get(self,request,document_id):
        document=get_register_document_or_404(request,document_id=document_id)
        assert_internal_manager(request.user,document)
        if document.document_origin!='SUPPLIER': raise Http404
        rows=SupplierPortalReturn.objects.filter(surveillance__document_revision__document=document,
            supplier_id=document.external_issuer_company_id,project_id=document.folder.project_id).select_related('surveillance__document_revision').order_by('-published_at')
        reviews=[{'publication':row.pk,'sheet':published_feedback(row),'answers':answers_data(row)} for row in rows]
        current=SupplierSurveillance.objects.filter(document_revision__document=document).order_by('-document_revision__version_number','-created_at').first()
        if current and not any(row['sheet']['reference']==current.reference for row in reviews):
            reviews.insert(0,{'publication':None,'sheet':public_feedback(current),'answers':[]})
        return Response({'document_id':document.pk,'reference':document.identification_number,'title':document.file_name,
            'reviews':reviews})
