"""Supplier events in the existing inbox; operational tasks remain independent of read state."""
from functools import partial
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from models.models import User
from ecpmpapp.utils import create_notification_with_recipients
from project_management.services.document_cycle import _explicit_role_right
from project_management.services.company_owner_document_access import documentary_owner_company_id


def recipients(project):
    owner_id = project.company_profile.admin_user_id
    return [user for user in User.objects.filter(company_id=project.company_profile_id,
        is_active=True, is_deleted=False, is_permanently_deleted=False)
        if (user.pk == owner_id and documentary_owner_company_id(user) == project.company_profile_id) or _explicit_role_right(user, 'company', {}) or
        _explicit_role_right(user, 'project', {'project': project.pk})]


def can_receive(user, project, document=None):
    """A cycle right alone must never disclose an inaccessible document or proposal."""
    if document is None:
        from project_management.permissions import DocumentPermission
        return DocumentPermission(action='create', scope='project')._check_create_permission(user, {'project': project.pk})
    from types import SimpleNamespace
    from project_management.services.document_access import get_accessible_document_queryset
    request = SimpleNamespace(user=user, query_params={'solution': project.solution})
    return get_accessible_document_queryset(request, project_id=project.pk).filter(pk=document.pk).exists()


def schedule_supplier_notification(kind, pk):
    transaction.on_commit(partial(deliver_supplier_notification, kind, pk), robust=True)


def deliver_supplier_notification(kind, pk):
    from supplier_portal.models import SupplierProposalBatch, SupplierPortalSubmission
    if kind == 'proposal':
        obj = SupplierProposalBatch.objects.select_related('project__company_profile', 'supplier').get(pk=pk)
        project, supplier = obj.project, obj.supplier
        count = obj.rows.filter(status='PENDING').count()
        if not count:
            return
    else:
        obj = SupplierPortalSubmission.objects.select_related('revision__document__folder__project__company_profile', 'revision__document__external_issuer_company').get(pk=pk)
        doc = obj.revision.document
        project, supplier = doc.folder.project, doc.external_issuer_company
    labels = {
        'French': ('Liste documentaire à valider', 'Nouveau document reçu', 'Nouvelle révision reçue'),
        'English': ('Document list awaiting validation', 'New document received', 'New revision received'),
        'German': ('Dokumentenliste zur Freigabe', 'Neues Dokument eingegangen', 'Neue Revision eingegangen'),
    }
    # One inbox entry per language, one proposal notification per batch, never email.
    grouped = {}
    for user in recipients(project):
        if not can_receive(user, project, None if kind == 'proposal' else doc):
            continue
        grouped.setdefault(user.preferred_language, []).append(user)
    for language, users in grouped.items():
        text = labels.get(language, labels['English'])
        title = text[0] if kind == 'proposal' else text[1 if obj.revision.version_number == 1 else 2]
        detail = str(count) if kind == 'proposal' else f'{doc.identification_number} — {obj.revision.external_revision or obj.revision.version_number}'
        create_notification_with_recipients(message=f'{title} · {project.project_name} · {supplier.company_name} · {detail}',
            content_type=ContentType.objects.get_for_model(obj), object_id=obj.pk, content_object=obj,
            notification_type='documents', action_type='create', recipients=users,
            important=True, project=project, bypass_preference_check=True)


def notification_target(notification):
    from supplier_portal.models import SupplierProposalBatch, SupplierPortalSubmission
    if notification.content_type.app_label != 'supplier_portal':
        return None
    if notification.content_type.model == 'supplierproposalbatch':
        batch = SupplierProposalBatch.objects.filter(pk=notification.object_id).first()
        return f'/supplier-register?project={batch.project_id}&batch={batch.pk}' if batch else None
    if notification.content_type.model == 'supplierportalsubmission':
        submission = SupplierPortalSubmission.objects.select_related('revision').filter(pk=notification.object_id).first()
        return f'/company-documents/{submission.revision.document_id}' if submission else None
    return None
