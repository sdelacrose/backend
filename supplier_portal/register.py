"""Forecast proposals: immutable submissions, controlled acceptance, no file creation."""
from collections import Counter
from hashlib import sha256
import json
from types import SimpleNamespace
import unicodedata

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from models.models import User
from project_management.models import Document, Project, Folder, ContactCompany
from project_management.permissions import DocumentPermission
from project_management.serializers.document import DocumentSerializer
from project_management.services.supplier_portal import assert_active_user
from supplier_portal.models import SupplierProjectAccess, SupplierProposalBatch, SupplierDocumentProposal, SupplierProposalEvent

FIELDS = ('file_name', 'identification_number', 'document_type', 'discipline', 'discipline_group_id',
          'discipline_option_id', 'supplier_transmittal_category', 'description')
MAX_ROWS = 1000


def project_accesses(user):
    assert_active_user(user)
    return SupplierProjectAccess.objects.filter(user=user, revoked_at__isnull=True,
        supplier__company_profile_id=F('project__company_profile_id'), folder__project_id=F('project_id')).select_related('project', 'supplier', 'folder')


def access_for(user, access_id):
    access = project_accesses(user).filter(pk=access_id).first()
    if not access:
        raise Http404
    return access


def assert_project_manager(user, project):
    assert_active_user(user)
    if user.company_id != project.company_profile_id or not user.company_id:
        raise PermissionDenied('Only the owning company can validate this supplier register.')
    if not DocumentPermission(action='create', scope='project')._check_create_permission(user, {'project': project.pk}):
        raise PermissionDenied('Document creation rights are required to validate the supplier register.')


@transaction.atomic
def grant_project(*, actor, project, supplier, email, folder=None):
    Project.objects.select_for_update().get(pk=project.pk)
    assert_project_manager(actor, project)
    if (folder is not None and folder.project_id != project.pk) or supplier.company_profile_id != project.company_profile_id:
        raise ValidationError('The folder and supplier must belong to this project company.')
    user = User.objects.filter(email__iexact=email.strip()).first()
    if not user:
        raise ValidationError('An existing active LINKS account is required.')
    assert_active_user(user)
    access = SupplierProjectAccess.objects.filter(project=project, supplier=supplier, user=user, revoked_at__isnull=True).first()
    if access:
        return access
    # Project lock serializes folder creation and access grants. No existing documents are moved.
    folder, _ = Folder.objects.get_or_create(project=project, system_key=f"supplier:{supplier.pk}",
        defaults={"name": supplier.company_name[:255]})
    if folder.name != supplier.company_name[:255]:
        folder.name = supplier.company_name[:255]
        folder.save(update_fields=["name", "updated_at"])
    return SupplierProjectAccess.objects.create(project=project, supplier=supplier, user=user, folder=folder, granted_by=actor)


@transaction.atomic
def revoke_project(*, actor, access):
    Project.objects.select_for_update().get(pk=access.project_id)
    assert_project_manager(actor, access.project)
    if not access.revoked_at:
        access.revoked_at, access.revoked_by = timezone.now(), actor
        access.save(update_fields=['revoked_at', 'revoked_by'])


def canonical(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def identity_key(data):
    reference = canonical(data.get('identification_number'))
    # Supplier Ref is optional in the existing form. Without it, compare title/type/discipline.
    identity = ['reference', reference] if reference else ['metadata'] + [canonical(data.get(k)) for k in
        ('file_name', 'document_type', 'discipline')]
    return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def document_keys(project, supplier):
    return {identity_key(data) for data in Document.objects.filter(folder__project=project,
        external_issuer_company=supplier, document_origin='SUPPLIER').values('file_name', 'identification_number', 'document_type', 'discipline')}


def normalized_data(data):
    if not isinstance(data, dict):
        return {}, {'row': ['A metadata object is required.']}
    errors = {}
    unknown = set(data) - set(FIELDS)
    if unknown:
        errors['columns'] = ['Unknown columns: ' + ', '.join(sorted(unknown))]
    metadata = {}
    for key in FIELDS:
        value = data.get(key, '')
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            errors[key] = ['Use a text value.']
        else:
            metadata[key] = str(value).strip()
    return metadata, errors


def serializer_for(metadata, batch, actor):
    payload = {**metadata, 'folder': batch.folder_id, 'document_origin': 'SUPPLIER', 'external_issuer_company': batch.supplier_id}
    request = SimpleNamespace(user=actor, query_params={'solution': batch.project.solution})
    return DocumentSerializer(data=payload, context={'user': actor, 'request': request})


def resolve_discipline(metadata, batch):
    """Map the project's human-readable discipline to the same IDs used by the form."""
    if not metadata.get('discipline') or metadata.get('discipline_option_id'):
        return {}
    from models.hierarchical_configurations import resolve_nested_configurations, DOCUMENT_DISCIPLINES_KEY
    from project_management.models import ECPMPConfig
    config = ECPMPConfig.objects.filter(project_id=batch.project_id).first()
    if not config:
        return {}
    matches = []
    for group in resolve_nested_configurations(config.nested_configurations)[DOCUMENT_DISCIPLINES_KEY]['groups']:
        for child in group['children']:
            if metadata['discipline'] in (child['label'], f"{group['label']} / {child['label']}"):
                matches.append((group, child))
    if len(matches) > 1:
        return {'discipline': ['This discipline is ambiguous. Copy its full label from the project choices sheet.']}
    if matches:
        group, child = matches[0]
        metadata.update(discipline=child['label'], discipline_group_id=group['id'], discipline_option_id=child['id'])
    return {}


def validation_errors(metadata, batch, actor):
    # Use the existing form's backend validator; never save while checking a proposal.
    serializer = serializer_for(metadata, batch, actor)
    return {} if serializer.is_valid() else json.loads(json.dumps(serializer.errors))


@transaction.atomic
def propose_batch(*, user, access_id, rows, source='IMPORT', line_numbers=None):
    access = access_for(user, access_id)
    Project.objects.select_for_update().get(pk=access.project_id)
    access = access_for(user, access_id)  # revocation uses the same lock
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS or source not in ('MANUAL', 'IMPORT'):
        raise ValidationError('Submit between 1 and 1000 metadata rows.')
    line_numbers = line_numbers or list(range(1, len(rows) + 1))
    if len(line_numbers) != len(rows) or len(set(line_numbers)) != len(rows):
        raise ValidationError('Import line numbers are invalid.')
    batch = SupplierProposalBatch.objects.create(project=access.project, supplier=access.supplier, folder=access.folder,
        submitted_by=user, source=source)
    prepared = [normalized_data(row) for row in rows]
    validator = access.project.company_profile.admin_user
    for metadata, errors in prepared:
        errors.update(resolve_discipline(metadata, batch))
        purposes = {'pour revue': 'FOR_REVIEW', 'for review': 'FOR_REVIEW', 'zur prüfung': 'FOR_REVIEW',
            'pour information': 'FOR_INFORMATION', 'for information': 'FOR_INFORMATION', 'zur information': 'FOR_INFORMATION'}
        purpose = metadata.get('supplier_transmittal_category', '')
        metadata['supplier_transmittal_category'] = purposes.get(purpose.casefold(), purpose)
        serializer = serializer_for(metadata, batch, validator)
        if serializer.is_valid():
            metadata['discipline'] = serializer.validated_data.get('discipline') or ''
        else:
            errors.update(json.loads(json.dumps(serializer.errors)))
    keys = Counter(identity_key(meta) for meta, _ in prepared)
    existing = document_keys(access.project, access.supplier)
    reserved = set(SupplierDocumentProposal.objects.filter(project=access.project, supplier=access.supplier,
        status__in=['PENDING', 'ACCEPTED']).values_list('identity_key', flat=True))
    # The company account is validation context only; no rights or documents are granted here.
    for line, (metadata, errors) in enumerate(prepared, 1):
        key = identity_key(metadata)
        if keys[key] > 1:
            errors['duplicate'] = ['Duplicate document within this batch.']
        elif key in existing or key in reserved:
            errors['duplicate'] = ['This supplier document already exists or is awaiting validation in this project.']
        row = SupplierDocumentProposal.objects.create(batch=batch, project=access.project, supplier=access.supplier,
            line_number=line_numbers[line - 1], metadata=metadata, identity_key=key, errors=errors, status='REFUSED' if errors else 'PENDING')
        SupplierProposalEvent.objects.create(proposal=row, actor=user,
            event_type='VALIDATION_REFUSED' if errors else 'SUBMITTED', reason=json.dumps(errors) if errors else '')
    from supplier_portal.notifications import schedule_supplier_notification
    schedule_supplier_notification('proposal', batch.pk)
    return batch


@transaction.atomic
def decide_batch(*, actor, batch, action, row_ids=None, reason=''):
    Project.objects.select_for_update().get(pk=batch.project_id)
    assert_project_manager(actor, batch.project)
    if action not in ('ACCEPT', 'REFUSE'):
        raise ValidationError('Use ACCEPT or REFUSE.')
    if action == 'REFUSE' and not reason.strip():
        raise ValidationError('A refusal reason is required.')
    if row_ids is not None and (not isinstance(row_ids, list) or not row_ids or len(row_ids) != len(set(row_ids))):
        raise ValidationError('Select distinct rows from this batch.')
    rows = batch.rows.select_for_update().all()
    if row_ids is not None:
        if rows.filter(pk__in=row_ids).count() != len(row_ids):
            raise Http404
        rows = rows.filter(pk__in=row_ids)
    accepted, refused, skipped = [], [], []
    existing = document_keys(batch.project, batch.supplier)
    context_valid = (Folder.objects.filter(pk=batch.folder_id, project_id=batch.project_id).exists() and
        ContactCompany.objects.filter(pk=batch.supplier_id, company_profile_id=batch.project.company_profile_id).exists())
    for row in rows:
        if row.status != 'PENDING':
            skipped.append(row.pk)
            continue
        errors = validation_errors(row.metadata, batch, actor)
        if not context_valid:
            errors['context'] = ['The supplier or destination no longer belongs to the approved project context.']
        if row.identity_key in existing:
            errors['duplicate'] = ['This supplier document already exists in the project.']
        if row.errors:
            errors.update(row.errors)
        if action == 'ACCEPT' and not errors:
            serializer = serializer_for(row.metadata, batch, actor)
            serializer.is_valid(raise_exception=True)
            row.document = serializer.save()
            row.status = 'ACCEPTED'
            accepted.append(row.pk)
            existing.add(row.identity_key)
        else:
            row.status = 'REFUSED'
            refused.append(row.pk)
        row.errors = errors
        row.decided_by, row.decided_at = actor, timezone.now()
        row.decision_reason = reason.strip() if action == 'REFUSE' else ''
        row.save(update_fields=['document', 'status', 'errors', 'decided_by', 'decided_at', 'decision_reason'])
        SupplierProposalEvent.objects.create(proposal=row, actor=actor, event_type=row.status, reason=row.decision_reason or (json.dumps(errors) if errors else ''))
    return {'accepted': accepted, 'refused': refused, 'skipped': skipped}


def document_scope(accesses):
    """Supplier AND dedicated folder subtree. An access never exposes the rest of a project."""
    accesses = list(accesses)
    if not accesses:
        return Q(pk__in=[])
    children = {}
    for folder_id, parent_id in Folder.objects.filter(project_id__in={a.project_id for a in accesses}).values_list('id', 'parent_id'):
        children.setdefault(parent_id, []).append(folder_id)
    scope = Q(pk__in=[])
    for access in accesses:
        seen, pending = set(), [access.folder_id]
        while pending:
            folder_id = pending.pop()
            if folder_id in seen:
                continue
            seen.add(folder_id)
            pending.extend(children.get(folder_id, []))
        scope |= Q(folder_id__in=seen, folder__project_id=access.project_id, external_issuer_company_id=access.supplier_id)
    return scope
