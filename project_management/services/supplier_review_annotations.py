from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from project_management.models import SupplierReviewAnnotation, SupplierSurveillance, SupplierSurveillanceReviewerAssignment, SupplierSurveillanceResponse
from project_management.services.supplier_surveillance_review import assert_can_respond, snapshot_items, record_event


def compiled_annotations(assignment, item_id):
    return '\n'.join(f'[Page {row.page}] {row.text}' for row in assignment.annotations.filter(item_snapshot_id=item_id,retracted_at__isnull=True,visibility="EXTERNAL"))


def change_annotation(*, surveillance, actor, values=None, retract_id=None):
    with transaction.atomic():
        locked = SupplierSurveillance.objects.select_for_update().get(pk=surveillance.pk)
        from project_management.services.document_withdrawal import assert_not_withdrawn
        assert_not_withdrawn(locked.document_revision.document)
        assignment = SupplierSurveillanceReviewerAssignment.objects.select_for_update().filter(surveillance=locked,reviewer=actor).first()
        if not assignment:
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied('Only an assigned reviewer can annotate this PDF.')
        assignment.surveillance = locked
        assert_can_respond(assignment, actor)
        if retract_id:
            annotation=SupplierReviewAnnotation.objects.filter(pk=retract_id,assignment=assignment,retracted_at__isnull=True).first()
            if not annotation:
                from django.http import Http404
                raise Http404
            item_id=annotation.item_snapshot_id
        else:
            item_id=values['item_snapshot_id']
            values=dict(values)
            values.setdefault('visibility','INTERNAL')
            mentioned=values.get('mentioned_reviewer_id')
            if mentioned and (values['visibility']!='INTERNAL' or not locked.reviewer_assignments.filter(reviewer_id=mentioned).exists()):
                raise ValidationError('Only another reviewer of this surveillance may be tagged in an internal comment.')
            if mentioned==actor.pk: raise ValidationError('Tag another reviewer.')
        item=next((item for item in (snapshot_items(locked) or []) if item['id']==item_id),None)
        if not item: raise ValidationError('Select an item from this surveillance template.')
        previous=compiled_annotations(assignment,item_id)
        if retract_id:
            annotation.retracted_at=timezone.now(); annotation.save(update_fields=['retracted_at'])
        else:
            revision=locked.document_revision
            if not revision.file or not (revision.file_name or revision.file.name).lower().endswith('.pdf'):
                raise ValidationError('Only the exact reviewed PDF can be annotated.')
            with revision.file.open('rb') as source:
                if source.read(5) != b'%PDF-': raise ValidationError('The reviewed file is not a readable PDF.')
            annotation=SupplierReviewAnnotation.objects.create(assignment=assignment,**values)
        if item.get('type') in ('TEXT','TEXTAREA') and annotation.visibility == 'EXTERNAL':
            response=SupplierSurveillanceResponse.objects.filter(assignment=assignment,item_snapshot_id=item_id).first()
            if response is None or not response.text_value.strip() or response.text_value==previous:
                SupplierSurveillanceResponse.objects.update_or_create(assignment=assignment,item_snapshot_id=item_id,
                    defaults={'item_type':item['type'],'text_value':compiled_annotations(assignment,item_id),'check_value':None})
        if assignment.status=='ASSIGNED':
            assignment.status='IN_PROGRESS'; assignment.started_at=timezone.now()
            assignment.save(update_fields=['status','started_at','updated_at'])
            record_event(locked,'REVIEW_STARTED',actor,assignment=assignment)
        if locked.status=='DRAFT':
            locked.status='UNDER_REVIEW'; locked.save(update_fields=['status','updated_at'])
        record_event(locked,'RESPONSE_UPDATED',actor,assignment=assignment,
            metadata={'annotation_id':annotation.pk,'page':annotation.page,'item_snapshot_ids':[item_id],'annotation_action':'retract' if retract_id else 'add'})
        return annotation
