"""Supplier-facing snapshots never reuse internal review serializers or reports."""
from django.core.exceptions import ValidationError
from supplier_portal.models import SupplierFeedbackSnapshot, SupplierFeedbackAnswer

def public_feedback(surveillance):
    from project_management.models import SupplierReviewAnnotation
    document=surveillance.document_revision.document
    lookup={item['id']:(section.get('title',''),item.get('label',''),si,ii)
        for si,section in enumerate((surveillance.template_snapshot or {}).get('sections',[])) for ii,item in enumerate(section.get('items',[]))}
    annotations=SupplierReviewAnnotation.objects.filter(assignment__surveillance=surveillance,visibility='EXTERNAL',retracted_at__isnull=True).select_related('assignment__reviewer')
    rows=[]
    for row in annotations:
        section,item,si,ii=lookup.get(row.item_snapshot_id,('','',0,0))
        rows.append({'id':row.pk,'page':row.page,'x':row.x,'y':row.y,'text':row.text,'reviewer':row.assignment.reviewer.full_name,
            'section':section,'item':item,'_order':(si,ii,row.page,row.y,row.x,row.pk)})
    rows.sort(key=lambda row:row['_order'])
    for row in rows: row.pop('_order')
    return {'reference':surveillance.reference,'document_reference':document.identification_number,'title':document.file_name,
        'supplier_revision':surveillance.document_revision.external_revision,'result':surveillance.result,
        'comment':surveillance.closure_comment or '', 'closed_at':surveillance.closed_at.isoformat() if surveillance.closed_at else None,
        'comments':rows}

def freeze_feedback(publication):
    content=public_feedback(publication.surveillance)
    content['comment']=publication.public_comment
    snapshot,_=SupplierFeedbackSnapshot.objects.get_or_create(publication=publication,defaults={'content':content})
    return snapshot.content

def published_feedback(publication):
    snapshot=getattr(publication,'feedback_snapshot',None)
    if snapshot: return snapshot.content
    # Historical publications predate explicit visibility: never infer external comments.
    doc=publication.surveillance.document_revision.document
    return {'reference':publication.surveillance.reference,'document_reference':doc.identification_number,'title':doc.file_name,
        'supplier_revision':publication.surveillance.document_revision.external_revision,'result':publication.result,
        'comment':publication.public_comment,'closed_at':publication.published_at.isoformat(),'comments':[]}

def validate_answers(publication, answers):
    comments=published_feedback(publication)['comments']
    required={row['id'] for row in comments}
    if not isinstance(answers,list): raise ValidationError({'responses':'Respond to each supplier comment.'})
    cleaned={}
    for row in answers:
        if not isinstance(row,dict) or type(row.get('comment_id')) is not int or row['comment_id'] not in required or row['comment_id'] in cleaned:
            raise ValidationError({'responses':'Invalid or duplicate supplier comment.'})
        text=row.get('response','')
        if not isinstance(text,str) or not text.strip() or len(text)>10000 or row.get('disposition') not in ('ADDRESSED','PARTIAL','NOT_ADDRESSED'):
            raise ValidationError({'responses':'Each comment requires a disposition and a written response.'})
        cleaned[row['comment_id']]={'comment_id':row['comment_id'],'disposition':row['disposition'],'response':text.strip()}
    if set(cleaned)!=required: raise ValidationError({'responses':'Respond to every external comment before submitting the next revision.'})
    return list(cleaned.values())

def record_answers(submission, publication, answers, actor):
    for values in answers:
        SupplierFeedbackAnswer.objects.create(submission=submission,publication=publication,responded_by=actor,**values)

def answers_data(publication, submission=None, include_internal=False):
    rows=publication.answers.select_related('submission__revision','responded_by').prefetch_related('assessments__assessed_by')
    if submission: rows=rows.filter(submission=submission)
    result=[]
    for row in rows.order_by('created_at','pk'):
        assessments=list(row.assessments.all())
        latest=max(assessments,key=lambda a:(a.created_at,a.pk)) if assessments else None
        result.append({'id':row.pk,'comment_id':row.comment_id,'disposition':row.disposition,'response':row.response,
            'revision':row.submission.revision_id,'external_revision':row.submission.revision.external_revision,
            'responded_at':row.created_at,
            **({'responded_by':row.responded_by.full_name,'assessment_history':[{'accepted':a.accepted,'comment':a.comment,'by':a.assessed_by.full_name,'at':a.created_at} for a in sorted(assessments,key=lambda a:(a.created_at,a.pk))]} if include_internal else {}),
            'assessment':{'accepted':latest.accepted,
                **({'by':latest.assessed_by.full_name,'comment':latest.comment} if include_internal else {}),'at':latest.created_at} if latest else None})
    return result
