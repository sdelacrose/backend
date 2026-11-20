"""Explicit, recipient-bound publications. Internal serializers and raw file URLs are never exposed."""
from hashlib import sha256
from pathlib import PurePath
from django.core.files.base import ContentFile
from django.db import transaction
from django.http import FileResponse, Http404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from project_management.models import Correspondence, Transmittal
from project_management.permissions import CorrespondencePermission, TransmittalPermission
from project_management.views.correspondence import correspondence_queryset_for_user
from project_management.views.transmittal import transmittal_queryset_for_user
from supplier_portal.views import InternalRegisterView, RegisterView
from supplier_portal.register import access_for
from supplier_portal.models import SupplierProjectAccess, SupplierSharedExchange, SupplierSharedFile

def source_choices(user, project, supplier_id):
    rows = []
    for kind, perm, prefix, queryset, recipient in (
        ('CORRESPONDENCE', CorrespondencePermission(), 'correspondence', correspondence_queryset_for_user(user), 'recipient_company_id'),
        ('TRANSMITTAL', TransmittalPermission(), 'transmittal', transmittal_queryset_for_user(user), 'external_company_id'),
    ):
        if not perm._check_action_or_full_access(user, prefix + '_update', 'project', {'project': project}):
            continue
        for obj in queryset.filter(project=project, status='ISSUED', direction='OUT', **{recipient: supplier_id}).order_by('-pk')[:200]:
            rows.append((kind, obj))
    return rows

def shared_rows(queryset):
    return [{'id': row.pk, 'kind': row.kind, 'published_at': row.published_at, **row.snapshot,
        'files': [{'id': file.pk, 'name': file.name} for file in row.files.all()]} for row in queryset.prefetch_related('files').order_by('-published_at', '-pk')]

class InternalShared(InternalRegisterView):
    def get(self, request, project_id):
        project = self.project(request, project_id)
        supplier_id = serializers.IntegerField(min_value=1).run_validation(request.query_params.get('supplier'))
        if not SupplierProjectAccess.objects.filter(project=project, supplier_id=supplier_id, revoked_at__isnull=True).exists():
            raise Http404
        choices = source_choices(request.user, project, supplier_id)
        return Response({'choices': [{'kind': kind, 'id': obj.pk, 'reference': (obj.reference if kind == 'CORRESPONDENCE' else obj.transmittal_number), 'subject': obj.subject} for kind, obj in choices],
            'results': shared_rows(SupplierSharedExchange.objects.filter(project=project, supplier_id=supplier_id))})

    @transaction.atomic
    def post(self, request, project_id):
        project = self.project(request, project_id)
        class Input(serializers.Serializer):
            supplier = serializers.IntegerField(min_value=1)
            kind = serializers.ChoiceField(choices=['CORRESPONDENCE', 'TRANSMITTAL'])
            source = serializers.IntegerField(min_value=1)
        data = Input(data=request.data); data.is_valid(raise_exception=True); values = data.validated_data
        accesses = SupplierProjectAccess.objects.select_for_update().filter(project=project, supplier_id=values['supplier'], revoked_at__isnull=True)
        if not list(accesses):
            raise Http404
        match = next(((kind, obj) for kind, obj in source_choices(request.user, project, values['supplier']) if kind == values['kind'] and obj.pk == values['source']), None)
        if not match:
            raise Http404
        kind, obj = match
        # Serialize concurrent publication against the actual source and project.
        obj = type(obj).objects.select_for_update().get(pk=obj.pk)
        recipient_id = obj.recipient_company_id if kind == 'CORRESPONDENCE' else obj.external_company_id
        if obj.project_id != project.pk or recipient_id != values['supplier'] or obj.status != 'ISSUED' or obj.direction != 'OUT':
            raise Http404
        if SupplierSharedExchange.objects.filter(project=project, supplier_id=values['supplier'], kind=kind, source_id=obj.pk).exists():
            raise ValidationError('This exchange has already been published to this supplier.')
        snapshot = {'reference': (obj.reference if kind == 'CORRESPONDENCE' else obj.transmittal_number), 'subject': obj.subject or '', 'message': obj.message or ''}
        files = []
        if kind == 'CORRESPONDENCE':
            files = [(a.file, a.file_name) for a in obj.attachments.all()]
        else:
            snapshot['items'] = []
            for item in obj.items.select_related('document_revision__document').all():
                revision = item.document_revision
                snapshot['items'].append({'title': revision.document.file_name, 'revision': revision.external_revision or revision.revision_label, 'purpose': item.distribution_purpose})
                if revision.file:
                    files.append((revision.file, PurePath(revision.file.name).name))
        if len(files) > 100:
            raise ValidationError('Too many files to publish in one exchange.')
        contents, total = [], 0
        for file, name in files:
            with file.open('rb') as opened:
                content = opened.read(50 * 1024 * 1024 + 1)
            total += len(content)
            if total > 50 * 1024 * 1024:
                raise ValidationError('The published files exceed 50 MB.')
            contents.append((name, content))
        exchange = SupplierSharedExchange.objects.create(project=project, supplier_id=values['supplier'], kind=kind, source_id=obj.pk, snapshot=snapshot, published_by=request.user)
        saved_files = []
        try:
            for name, content in contents:
                stored = SupplierSharedFile.objects.create(exchange=exchange, name=name, file=ContentFile(content, name=PurePath(name).name), sha256=sha256(content).hexdigest())
                saved_files.append(stored.file)
        except Exception:
            for file in saved_files:
                file.delete(save=False)
            raise
        return Response({'id': exchange.pk}, status=201)

class PortalShared(RegisterView):
    def get(self, request, access_id):
        access = access_for(request.user, access_id)
        return Response({'results': shared_rows(SupplierSharedExchange.objects.filter(project_id=access.project_id, supplier_id=access.supplier_id))})

class PortalSharedFile(RegisterView):
    def get(self, request, access_id, exchange_id, file_id):
        access = access_for(request.user, access_id)
        file = SupplierSharedFile.objects.filter(pk=file_id, exchange_id=exchange_id, exchange__project_id=access.project_id, exchange__supplier_id=access.supplier_id).first()
        if file is None:
            raise Http404
        response = FileResponse(file.file.open('rb'), as_attachment=True, filename=file.name)
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
