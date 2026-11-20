from django.core.exceptions import ValidationError as DjangoValidationError, PermissionDenied
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied
from rest_framework.response import Response
from project_management.models import SupplierReviewAnnotation
from project_management.views.supplier_surveillance_review import SupplierSurveillanceReviewViewSet
from project_management.services.supplier_review_annotations import change_annotation

class AnnotationInput(serializers.Serializer):
    def validate(self, values):
        import math
        if not all(math.isfinite(values[key]) for key in ("x","y")):
            raise serializers.ValidationError("Select a finite point on the PDF.")
        return values

    visibility=serializers.ChoiceField(choices=["INTERNAL","EXTERNAL"],default="INTERNAL")
    mentioned_reviewer_id=serializers.IntegerField(min_value=1,required=False,allow_null=True)
    item_snapshot_id=serializers.IntegerField(min_value=1)
    page=serializers.IntegerField(min_value=1,max_value=100000)
    x=serializers.FloatField(min_value=0,max_value=1)
    y=serializers.FloatField(min_value=0,max_value=1)
    text=serializers.CharField(max_length=10000,allow_blank=False,trim_whitespace=True)

class SupplierReviewAnnotationViewSet(SupplierSurveillanceReviewViewSet):
    def annotations(self,request,surveillance_id=None):
        surveillance=self._surveillance(request,surveillance_id)
        revision=surveillance.document_revision
        rows=SupplierReviewAnnotation.objects.filter(assignment__surveillance=surveillance,retracted_at__isnull=True).select_related('assignment__reviewer','mentioned_reviewer')
        return Response({'current_user':request.user.pk,'document':{'id':revision.document_id,'file':request.build_absolute_uri(revision.file.url) if revision.file else None,'file_name':revision.file_name},
            'snapshot':surveillance.template_snapshot,
            'reviewers':[{'id':a.reviewer_id,'name':a.reviewer.full_name} for a in surveillance.reviewer_assignments.select_related('reviewer')],
            'annotations':[{'visibility':row.visibility,'mentioned_reviewer_id':row.mentioned_reviewer_id,'mentioned_reviewer_name':row.mentioned_reviewer.full_name if row.mentioned_reviewer_id else None,'id':row.pk,'item_snapshot_id':row.item_snapshot_id,'page':row.page,'x':row.x,'y':row.y,'text':row.text,
                'reviewer_id':row.assignment.reviewer_id,'reviewer_name':row.assignment.reviewer.full_name,'created_at':row.created_at,
                'can_retract':row.assignment.reviewer_id==request.user.pk and row.assignment.status!='COMPLETED' and surveillance.status not in ('READY_FOR_CLOSURE','CLOSED')}
                for row in rows]})

    def add_annotation(self,request,surveillance_id=None):
        surveillance=self._surveillance(request,surveillance_id)
        data=AnnotationInput(data=request.data); data.is_valid(raise_exception=True)
        return self._change(request,surveillance,values=data.validated_data)

    def retract_annotation(self,request,surveillance_id=None,annotation_id=None):
        surveillance=self._surveillance(request,surveillance_id)
        return self._change(request,surveillance,retract_id=annotation_id)

    def _change(self,request,surveillance,**values):
        try:
            annotation=change_annotation(surveillance=surveillance,actor=request.user,**values)
        except PermissionDenied as error: raise ApiPermissionDenied(str(error))
        except DjangoValidationError as error: return Response({'detail':error.messages},status=400)
        return Response({'id':annotation.pk},status=200 if values.get('retract_id') else 201)
