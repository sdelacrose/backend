"""Supplier endpoints never reuse the internal document serializer or its broad queryset."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.http import Http404
from django.utils import timezone
from types import SimpleNamespace
from rest_framework.exceptions import PermissionDenied

from models.models import User
from project_management.models import Document, DocumentOrigin, DocumentRevision, SupplierSurveillance
from project_management.models.supplier_portal import SupplierPortalGrant, SupplierPortalSubmission, SupplierPortalReturn
from project_management.services.document_register import assert_can_update_document
from project_management.services.document_revision import create_document_revision


def assert_active_user(user):
    if (not user or not user.is_authenticated or not user.is_active or
            getattr(user, 'is_deleted', False) or getattr(user, 'is_permanently_deleted', False)):
        raise PermissionDenied('This account cannot access the supplier portal.')


def portal_grants(user):
    assert_active_user(user)
    # Legacy grants are retained as evidence, but no longer authorize portal access.
    return SupplierPortalGrant.objects.none().filter(
        user=user, revoked_at__isnull=True,
        document__document_origin=DocumentOrigin.SUPPLIER,
        document__withdrawn_at__isnull=True,
        document__folder__project_id=F('project_id'),
        document__external_issuer_company_id=F('supplier_id'),
        supplier__company_profile_id=F('project__company_profile_id'),
    ).select_related('document', 'project', 'supplier')


def portal_grant(user, document_id):
    grant = portal_grants(user).filter(document_id=document_id).first()
    if grant is None and getattr(settings, 'SUPPLIER_REGISTER_ENABLED', False):
        from supplier_portal.register import project_accesses, document_scope
        document = Document.objects.filter(pk=document_id, document_origin='SUPPLIER', withdrawn_at__isnull=True).select_related('folder__project', 'external_issuer_company').first()
        if document:
            member = project_accesses(user).filter(project_id=document.folder.project_id, supplier_id=document.external_issuer_company_id).first()
            if member and Document.objects.filter(pk=document.pk).filter(document_scope([member])).exists():
                grant = SimpleNamespace(document=document, document_id=document.pk, project=member.project, project_id=member.project_id,
                    supplier=member.supplier, supplier_id=member.supplier_id, project_access=member)
    if grant is None:
        raise Http404
    return grant


def assert_internal_manager(actor, document):
    assert_active_user(actor)
    project = document.folder.project if document.folder_id else None
    # A portal grant never supplies management rights, even when the person has another role.
    if not project or not project.company_profile_id or actor.company_id != project.company_profile_id:
        raise PermissionDenied('Only an authorized member of the owning company can manage supplier access.')
    assert_can_update_document(actor, document)


@transaction.atomic
def grant_access(*, document, email, actor):
    raise PermissionDenied("Manage supplier access from the supplier portal dedicated folder.")
    document = Document.objects.select_for_update().get(pk=document.pk)
    assert_internal_manager(actor, document)
    if document.document_origin != DocumentOrigin.SUPPLIER or not document.external_issuer_company_id:
        raise ValidationError('Assign a supplier to a supplier-origin document first.')
    if document.external_issuer_company.company_profile_id != document.folder.project.company_profile_id:
        raise ValidationError('The supplier must belong to the project company contact register.')
    if document.withdrawn_at:
        raise ValidationError('A withdrawn document cannot be shared.')
    user = User.objects.filter(email__iexact=(email or '').strip()).first()
    if user is None:
        raise ValidationError('An active LINKS account is required. No invitation has been sent.')
    assert_active_user(user)
    grant, _ = SupplierPortalGrant.objects.get_or_create(
        document=document, user=user, revoked_at__isnull=True,
        defaults={'project': document.folder.project, 'supplier': document.external_issuer_company, 'granted_by': actor},
    )
    return grant


@transaction.atomic
def revoke_access(*, grant, actor):
    Document.objects.select_for_update().get(pk=grant.document_id)
    assert_internal_manager(actor, grant.document)
    grant = SupplierPortalGrant.objects.select_for_update().get(pk=grant.pk)
    if grant.revoked_at is None:
        grant.revoked_at, grant.revoked_by = timezone.now(), actor
        grant.save(update_fields=['revoked_at', 'revoked_by'])
    return grant


@transaction.atomic
def publish_return(*, surveillance, public_comment, actor):
    Document.objects.select_for_update().get(pk=surveillance.document_revision.document_id)
    assert_internal_manager(actor, surveillance.document_revision.document)
    surveillance = SupplierSurveillance.objects.select_for_update().get(pk=surveillance.pk)
    document = surveillance.document_revision.document
    if document.document_origin != DocumentOrigin.SUPPLIER or not document.external_issuer_company_id:
        raise ValidationError('Assign the supplier before publishing a return.')
    if surveillance.status != SupplierSurveillance.Status.CLOSED:
        raise ValidationError('Only a closed surveillance can be published to the supplier.')
    comment = (public_comment or '').strip()
    if SupplierPortalReturn.objects.filter(surveillance=surveillance).exists():
        raise ValidationError('This return has already been published and cannot be changed.')
    if surveillance.result not in SupplierSurveillance.Result.values or surveillance.new_revision_required is None:
        raise ValidationError('The closed surveillance has no final decision.')
    publication = SupplierPortalReturn.objects.create(surveillance=surveillance, public_comment=comment, published_by=actor,
                                               project=surveillance.document_revision.document.folder.project,
                                               supplier=surveillance.document_revision.document.external_issuer_company,
                                               result=surveillance.result, new_revision_required=surveillance.new_revision_required)

    from supplier_portal.feedback import freeze_feedback
    freeze_feedback(publication)
    return publication


def publish_closed_feedback(*, surveillance, actor):
    """Called inside authorized closure's transaction; never grants supplier access."""
    document = surveillance.document_revision.document
    project = document.folder.project
    if not document.external_issuer_company_id or not project:
        return None  # Legacy records with no named supplier cannot be published safely.
    if document.document_origin != DocumentOrigin.SUPPLIER:
        raise ValidationError('Only supplier documents can publish supplier feedback.')
    from project_management.services.supplier_surveillance_template import resolve_company_for_project
    if document.external_issuer_company.company_profile_id != resolve_company_for_project(project):
        raise ValidationError('The supplier does not belong to this project company.')
    if surveillance.status != SupplierSurveillance.Status.CLOSED or surveillance.result not in SupplierSurveillance.Result.values:
        raise ValidationError('A closed surveillance with a final result is required.')
    publication, _ = SupplierPortalReturn.objects.get_or_create(surveillance=surveillance, defaults={
        'public_comment': surveillance.closure_comment or '', 'published_by': actor,
        'project': project, 'supplier': document.external_issuer_company,
        'result': surveillance.result, 'new_revision_required': surveillance.new_revision_required})
    from supplier_portal.feedback import freeze_feedback
    freeze_feedback(publication)
    return publication


@transaction.atomic
def submit_revision(*, user, document_id, uploaded_file, external_revision='', comment='', feedback_responses=None):
    portal_grant(user, document_id)
    document = Document.objects.select_for_update().get(pk=document_id)
    grant = portal_grant(user, document_id)
    if uploaded_file is None:
        raise ValidationError({'file': 'Select a document to submit.'})
    name = uploaded_file.name
    if not name or any(c in name for c in ('/', '\\')) or any(ord(c) < 32 for c in name):
        raise ValidationError({'file': 'The file name is invalid.'})
    if uploaded_file.size > getattr(settings, 'SUPPLIER_PORTAL_MAX_FILE_BYTES', 50 * 1024 * 1024):
        raise ValidationError({'file': 'The file exceeds the supplier portal upload limit.'})
    latest = DocumentRevision.objects.filter(document=document).order_by('-version_number').first()
    if latest and SupplierPortalSubmission.objects.filter(revision=latest).exists():
        if not SupplierPortalReturn.objects.filter(surveillance__document_revision=latest).exists():
            raise ValidationError('Wait for the published review before submitting another revision.')
    if latest and SupplierSurveillance.objects.filter(document_revision=latest).exclude(status='CLOSED').exists():
        raise ValidationError('A surveillance is still in progress for the current revision.')
    from supplier_portal.feedback import validate_answers, record_answers
    publication = SupplierPortalReturn.objects.filter(surveillance__document_revision=latest,supplier_id=grant.supplier_id,project_id=grant.project_id).first() if latest else None
    answers = validate_answers(publication, feedback_responses or []) if publication else []
    revision = create_document_revision(document, uploaded_file, user, 'minor', comment, external_revision=external_revision)
    submission = SupplierPortalSubmission.objects.create(grant=grant if isinstance(grant, SupplierPortalGrant) else None,
        project_access=getattr(grant, 'project_access', None), revision=revision)
    if publication: record_answers(submission, publication, answers, user)
    from supplier_portal.notifications import schedule_supplier_notification
    schedule_supplier_notification('submission', submission.pk)
    return submission


def visible_revisions(grant):
    from django.db.models import Q
    return DocumentRevision.objects.filter(document_id=grant.document_id).filter(
        Q(portal_submission__grant__supplier_id=grant.supplier_id, portal_submission__grant__project_id=grant.project_id) |
        Q(portal_submission__project_access__supplier_id=grant.supplier_id, portal_submission__project_access__project_id=grant.project_id) |
        Q(supplier_surveillance__portal_return__supplier_id=grant.supplier_id,
          supplier_surveillance__portal_return__project_id=grant.project_id)
    ).distinct().order_by('-version_number')
