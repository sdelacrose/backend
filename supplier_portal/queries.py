from django.db.models import Q
from django.http import Http404
from project_management.models import Document, DocumentRevision, SupplierSurveillance
from project_management.services.supplier_portal import portal_grants
from supplier_portal.models import SupplierPortalReturn, SupplierDocumentProposal
from supplier_portal.register import project_accesses, document_scope


def projects_for(user):
    result = {}
    for member in project_accesses(user):
        result.setdefault(member.project_id, {'id': member.project_id, 'name': member.project.project_name, 'accesses': []})['accesses'].append(
            {'id': member.pk, 'supplier': member.supplier.company_name})
    for grant in portal_grants(user):
        result.setdefault(grant.project_id, {'id': grant.project_id, 'name': grant.project.project_name, 'accesses': []})
    return list(result.values())


def documents_for(user, project_id):
    members = project_accesses(user).filter(project_id=project_id)
    if not members.exists():
        raise Http404
    return Document.objects.filter(folder__project_id=project_id, document_origin='SUPPLIER', withdrawn_at__isnull=True).filter(
        document_scope(members)
    ).select_related('folder__project', 'external_issuer_company').order_by('identification_number', 'id').distinct()


def public_document(document, state=None):
    if state is None:
        revision = DocumentRevision.objects.filter(document=document).order_by('-version_number').first()
        publication = SupplierPortalReturn.objects.filter(surveillance__document_revision=revision,
            supplier_id=document.external_issuer_company_id, project_id=document.folder.project_id).first() if revision else None
        result = publication.result if publication else None
        surveilled = bool(revision and SupplierSurveillance.objects.filter(document_revision=revision).exists())
    else:
        revision, result, surveilled = state[:3]
        publication = state[3] if len(state)>3 else None
    status = 'TO_SUBMIT'
    if revision or document.file:
        status = result if result else 'UNDER_REVIEW' if surveilled else 'DEPOSITED'
    return {'id': document.pk, 'reference': document.identification_number, 'title': document.file_name,
        'document_type': document.document_type, 'discipline': document.discipline,
        'supplier': document.external_issuer_company.company_name, 'supplier_id': document.external_issuer_company_id,
        'project': {'id': document.folder.project_id, 'name': document.folder.project.project_name},
        'external_revision': revision.external_revision if revision else None, 'status': status,
        'current_revision': revision.pk if revision else None,
        'file_received': bool(revision or document.file),
        'feedback_required': bool(publication and getattr(publication,'feedback_snapshot',None) and publication.feedback_snapshot.content.get('comments'))}


def document_rows(native, project_id):
    """Resolve latest revisions and published reviews in bounded queries."""
    revisions = {r.document_id: r for r in DocumentRevision.objects.filter(document_id__in=[d.pk for d in native]).order_by('document_id', '-version_number').distinct('document_id')}
    ids = [r.pk for r in revisions.values()]
    publications = {r.surveillance.document_revision_id: r for r in SupplierPortalReturn.objects.filter(
        surveillance__document_revision_id__in=ids, project_id=project_id).select_related('surveillance','feedback_snapshot')}
    reviewed_ids = set(SupplierSurveillance.objects.filter(document_revision_id__in=ids).values_list('document_revision_id', flat=True))
    documents = []
    for doc in native:
        revision = revisions.get(doc.pk)
        publication = publications.get(revision.pk) if revision else None
        result = publication.result if publication and publication.supplier_id == doc.external_issuer_company_id else None
        documents.append(public_document(doc, (revision, result, bool(revision and revision.pk in reviewed_ids), publication)))
    return documents


def dashboard(user, project_id):
    documents = document_rows(list(documents_for(user, project_id)), project_id)
    total = len(documents)
    received = sum(doc['file_received'] for doc in documents)
    counts = {status: sum(doc['status'] == status for doc in documents) for status in ['REVIEWED', 'COMMENTED', 'REJECTED']}
    reviewed = sum(counts.values())
    suppliers = project_accesses(user).filter(project_id=project_id).values('supplier_id')
    pending = SupplierDocumentProposal.objects.filter(project_id=project_id, supplier_id__in=suppliers, status='PENDING').count()
    return {'documents': documents, 'kpis': {'total': total, 'pending': pending, 'received': received,
        'received_percent': round(100 * received / total, 1) if total else 0,
        'reviewed_total': reviewed, 'reviewed_percent': round(100 * reviewed / total, 1) if total else 0, **{k.lower(): v for k, v in counts.items()}}}
