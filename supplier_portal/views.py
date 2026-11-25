import json
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from ecpmpapp.permissions import LicensePermission
from rest_framework.permissions import IsAuthenticated
from project_management.models import Project, ContactCompany, Folder
from project_management.views.supplier_portal import SupplierPortalView, business_errors
from project_management.services.supplier_portal import submit_revision
from supplier_portal.models import SupplierProjectAccess, SupplierProposalBatch, SupplierProposalEvent
from supplier_portal.register import (access_for, project_accesses, assert_project_manager, grant_project, revoke_project, propose_batch, decide_batch)
from supplier_portal.queries import projects_for, dashboard
from supplier_portal.files import parse_list, template_bytes


def project_choices(access):
    from types import SimpleNamespace
    from project_management.serializers.time_expense import get_valid_choices
    from models.hierarchical_configurations import resolve_nested_configurations, DOCUMENT_DISCIPLINES_KEY
    from project_management.models import ECPMPConfig
    request = SimpleNamespace(query_params={'solution': access.project.solution})
    types = sorted({choice.split(':')[0] for choice in get_valid_choices('document_types', access.project_id, request)})
    config = ECPMPConfig.objects.filter(project_id=access.project_id).first()
    disciplines = []
    if config:
        for group in resolve_nested_configurations(config.nested_configurations)[DOCUMENT_DISCIPLINES_KEY]['groups']:
            for child in group['children']:
                disciplines.append({'label': child['label'], 'display_label': f"{group['label']} / {child['label']}", 'group_id': group['id'], 'option_id': child['id']})
    for choice in get_valid_choices('document_disciplines', access.project_id, request):
        label = choice.split(':')[0]
        if not any(d['label'] == label for d in disciplines):
            disciplines.append({'label': label, 'display_label': label, 'group_id': '', 'option_id': ''})
    return {'document_types': types, 'disciplines': sorted(disciplines, key=lambda d: d['display_label'])}


class RegisterView(SupplierPortalView):
    def initial(self, request, *args, **kwargs):
        if not getattr(settings, 'SUPPLIER_REGISTER_ENABLED', False):
            raise Http404
        return super().initial(request, *args, **kwargs)


def rows_data(batch, internal=False):
    from django.db.models import Prefetch
    return [{'id': row.pk, 'line': row.line_number, 'metadata': row.metadata, 'status': row.status, 'errors': row.errors,
        'document': row.document_id, 'decided_at': row.decided_at, 'reason': row.decision_reason,
        **({'decided_by': f'{row.decided_by.first_name} {row.decided_by.last_name}'.strip() if row.decided_by_id else ''} if internal else {}),
        'events': [{'event_type': event.event_type, 'reason': event.reason, 'created_at': event.created_at} for event in row.events.all()]} for row in batch.rows.select_related('decided_by').prefetch_related(Prefetch('events', queryset=SupplierProposalEvent.objects.order_by('created_at', 'pk'))).all()]


def batch_data(batch, detail=False, internal=False):
    result = {'id': batch.pk, 'project': batch.project_id, 'supplier': batch.supplier_id,
        'supplier_name': batch.supplier.company_name, 'source': batch.source, 'created_at': batch.created_at}
    if detail:
        result['rows'] = rows_data(batch, internal=internal)
    else:
        from django.db.models import Count
        result['counts'] = {status: getattr(batch, f'{status.lower()}_count') for status in ('PENDING', 'ACCEPTED', 'REFUSED')} if hasattr(batch, 'pending_count') else dict(batch.rows.values_list('status').annotate(count=Count('id')))
    return result


def batch_page(queryset, request):
    from django.db.models import Count, Q
    field = serializers.IntegerField(min_value=1, max_value=100000)
    page = field.run_validation(request.query_params.get('page', 1))
    count, size = queryset.count(), 50
    rows = queryset.select_related('supplier').annotate(**{f'{status.lower()}_count': Count('rows', filter=Q(rows__status=status)) for status in ('PENDING', 'ACCEPTED', 'REFUSED')})[(page - 1) * size:page * size]
    return {'batches': [batch_data(batch) for batch in rows], 'count': count, 'page': page,
        'next_page': page + 1 if page * size < count else None, 'previous_page': page - 1 if page > 1 else None}


class PortalProjects(RegisterView):
    def get(self, request):
        return Response({'projects': projects_for(request.user)})


class PortalDashboard(RegisterView):
    def get(self, request, project_id):
        return Response(dashboard(request.user, project_id))


class PortalTemplate(RegisterView):
    def get(self, request, access_id):
        access = access_for(request.user, access_id)
        response = HttpResponse(template_bytes(project_choices(access), request.query_params.get('language', 'en')), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="supplier-document-list.xlsx"'
        response['Cache-Control'] = 'private, no-store'
        return response


class PortalChoices(RegisterView):
    def get(self, request, access_id):
        return Response(project_choices(access_for(request.user, access_id)))


class PortalBatches(RegisterView):
    def get(self, request, access_id):
        access = access_for(request.user, access_id)
        batches = SupplierProposalBatch.objects.filter(project=access.project, supplier=access.supplier).order_by('-created_at', '-pk')
        return Response(batch_page(batches, request))

    @business_errors
    def post(self, request, access_id):
        upload = request.FILES.get('file')
        if upload:
            rows, source = parse_list(upload), 'IMPORT'
        else:
            class ProposalInput(serializers.Serializer):
                rows = serializers.ListField(child=serializers.DictField(), min_length=1, max_length=1000)
            data = ProposalInput(data=request.data)
            data.is_valid(raise_exception=True)
            rows, source = data.validated_data['rows'], 'MANUAL' if len(data.validated_data['rows']) == 1 else 'IMPORT'
        batch = propose_batch(user=request.user, access_id=access_id, rows=rows, source=source, line_numbers=getattr(rows, 'line_numbers', None))
        return Response(batch_data(batch, detail=True), status=201)


class PortalBatchDetail(RegisterView):
    def get(self, request, access_id, batch_id):
        access = access_for(request.user, access_id)
        batch = SupplierProposalBatch.objects.filter(pk=batch_id, project=access.project, supplier=access.supplier).select_related('supplier').first()
        if batch is None:
            raise Http404
        return Response(batch_data(batch, detail=True))


class PortalBulkFiles(RegisterView):
    @business_errors
    def post(self, request, project_id):
        from supplier_portal.queries import documents_for
        allowed_ids = set(documents_for(request.user, project_id).values_list('id', flat=True))
        class Entry(serializers.Serializer):
            document = serializers.IntegerField(min_value=1)
            external_revision = serializers.CharField(max_length=20, allow_blank=True, default='')
            comment = serializers.CharField(max_length=10000, allow_blank=True, default='')
        try:
            entries = json.loads(request.data.get('manifest', '[]'))
        except (TypeError, ValueError):
            raise ValidationError('The bulk file manifest is invalid.')
        data = Entry(data=entries, many=True)
        data.is_valid(raise_exception=True)
        entries = data.validated_data
        files = request.FILES.getlist('files')
        if not 1 <= len(entries) <= 50 or len(files) != len(entries):
            raise ValidationError('Match each of up to 50 files to one accepted document.')
        if len({entry['document'] for entry in entries}) != len(entries):
            raise ValidationError('A document may only appear once in a file batch.')
        if sum(file.size for file in files) > 250 * 1024 * 1024:
            raise ValidationError('The file batch exceeds 250 MiB.')
        results = []
        for index, (entry, upload) in enumerate(zip(entries, files), 1):
            try:
                if entry['document'] not in allowed_ids:
                    raise Http404
                submission = submit_revision(user=request.user, document_id=entry['document'], uploaded_file=upload,
                    external_revision=entry['external_revision'], comment=entry['comment'])
                results.append({'line': index, 'document': entry['document'], 'status': 'SUBMITTED', 'revision': submission.revision_id})
            except Http404:
                results.append({'line': index, 'status': 'REFUSED', 'errors': {'document': ['This document is not accepted or accessible.']}})
            except ValidationError as error:
                results.append({'line': index, 'status': 'REFUSED', 'errors': getattr(error, 'message_dict', None) or {'file': error.messages}})
        return Response({'results': results})


class InternalRegisterView(RegisterView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def project(self, request, project_id):
        project = Project.objects.filter(pk=project_id, company_profile_id=request.user.company_id).select_related('company_profile').first()
        if project is None:
            raise Http404
        assert_project_manager(request.user, project)
        return project


class InternalProjects(InternalRegisterView):
    def get(self, request):
        projects = []
        from rest_framework.exceptions import PermissionDenied
        for project in Project.objects.filter(company_profile_id=request.user.company_id).select_related('company_profile'):
            try:
                assert_project_manager(request.user, project)
                projects.append({'id': project.pk, 'name': project.project_name})
            except PermissionDenied:
                continue
        return Response({'projects': projects})


class InternalProjectAccess(InternalRegisterView):
    def get(self, request, project_id):
        project = self.project(request, project_id)
        accesses = SupplierProjectAccess.objects.filter(project=project).select_related('user', 'supplier').order_by('-created_at')
        return Response({'accesses': [{'id': a.pk, 'email': a.user.email, 'supplier': a.supplier.company_name, 'revoked_at': a.revoked_at} for a in accesses],
            'suppliers': list(ContactCompany.objects.filter(company_profile=project.company_profile).values('id', 'company_name')),
            'folders': list(Folder.objects.filter(project=project).values('id', 'name'))})

    @business_errors
    def post(self, request, project_id):
        project = self.project(request, project_id)
        class Input(serializers.Serializer):
            email = serializers.EmailField()
            supplier = serializers.IntegerField(min_value=1)
            folder = serializers.IntegerField(min_value=1, required=False)
        data = Input(data=request.data)
        data.is_valid(raise_exception=True)
        supplier = ContactCompany.objects.filter(pk=data.validated_data['supplier'], company_profile=project.company_profile).first()
        if not supplier:
            raise Http404
        access = grant_project(actor=request.user, project=project, supplier=supplier, email=data.validated_data['email'])
        return Response({'id': access.pk, 'folder': {'id': access.folder_id, 'name': access.folder.name}}, status=201)

    @business_errors
    def delete(self, request, project_id):
        project = self.project(request, project_id)
        class Input(serializers.Serializer):
            access = serializers.IntegerField(min_value=1)
        data = Input(data=request.data)
        data.is_valid(raise_exception=True)
        access = SupplierProjectAccess.objects.filter(pk=data.validated_data['access'], project=project).first()
        if access is None:
            raise Http404
        revoke_project(actor=request.user, access=access)
        return Response(status=204)


class InternalBatches(InternalRegisterView):
    def get(self, request, project_id):
        project = self.project(request, project_id)
        batches = SupplierProposalBatch.objects.filter(project=project).order_by('-created_at', '-pk')
        return Response(batch_page(batches, request))


class InternalDocuments(InternalRegisterView):
    def get(self, request, project_id):
        from django.db.models import Q
        from project_management.services.document_access import get_accessible_document_queryset
        from supplier_portal.queries import document_rows
        project = self.project(request, project_id)
        documents = get_accessible_document_queryset(request, project_id=project.pk).filter(
            folder__project=project, document_origin='SUPPLIER', withdrawn_at__isnull=True,
            external_issuer_company__company_profile_id=project.company_profile_id,
        ).select_related('folder__project', 'external_issuer_company').distinct()
        suppliers = list(documents.order_by('external_issuer_company__company_name', 'external_issuer_company_id')
            .values('external_issuer_company_id', 'external_issuer_company__company_name').distinct())
        class Filters(serializers.Serializer):
            supplier = serializers.IntegerField(min_value=1, required=False)
            page = serializers.IntegerField(min_value=1, default=1)
            search = serializers.CharField(max_length=250, allow_blank=True, default='')
        filters = Filters(data=request.query_params)
        filters.is_valid(raise_exception=True)
        values = filters.validated_data
        if 'supplier' in values:
            documents = documents.filter(external_issuer_company_id=values['supplier'])
        if values['search']:
            documents = documents.filter(Q(identification_number__icontains=values['search']) |
                Q(file_name__icontains=values['search']) | Q(external_issuer_company__company_name__icontains=values['search']))
        count, size = documents.count(), 25
        page = values['page']
        rows = list(documents.order_by('identification_number', 'pk')[(page - 1) * size:page * size])
        return Response({'documents': document_rows(rows, project.pk), 'count': count, 'page': page,
            'page_size': size, 'next_page': page + 1 if page * size < count else None,
            'previous_page': page - 1 if page > 1 else None,
            'suppliers': [{'id': s['external_issuer_company_id'], 'name': s['external_issuer_company__company_name']} for s in suppliers]})


class InternalTasks(InternalRegisterView):
    def get(self, request, project_id):
        from django.db.models import F, OuterRef, Subquery
        from project_management.models import DocumentRevision
        from supplier_portal.models import SupplierDocumentProposal, SupplierPortalSubmission
        project = self.project(request, project_id)
        pending = SupplierDocumentProposal.objects.filter(project=project, status='PENDING')
        latest = DocumentRevision.objects.filter(document_id=OuterRef('revision__document_id')).order_by('-version_number').values('pk')[:1]
        submissions = SupplierPortalSubmission.objects.filter(revision__document__folder__project=project,
            revision__document__withdrawn_at__isnull=True, revision__supplier_surveillance__isnull=True
        ).annotate(latest_id=Subquery(latest)).filter(revision_id=F('latest_id')).select_related('revision__document__external_issuer_company')
        return Response({'pending_proposals': pending.count(), 'pending_batches': pending.values('batch_id').distinct().count(),
            'received_documents': submissions.count(), 'documents': [{'id': s.revision.document_id,
                'title': s.revision.document.file_name, 'reference': s.revision.document.identification_number,
                'supplier': s.revision.document.external_issuer_company.company_name,
                'revision': s.revision.external_revision, 'new_revision': s.revision.version_number > 1,
                'url': f'/company-documents/{s.revision.document_id}'} for s in submissions.order_by('-submitted_at')],
            'batches': list(pending.values_list('batch_id', flat=True).distinct()[:100])})


class InternalBatchDetail(InternalRegisterView):
    def batch(self, request, project_id, batch_id):
        project = self.project(request, project_id)
        batch = SupplierProposalBatch.objects.filter(pk=batch_id, project=project).select_related('supplier', 'project').first()
        if batch is None:
            raise Http404
        return batch

    def get(self, request, project_id, batch_id):
        return Response(batch_data(self.batch(request, project_id, batch_id), detail=True, internal=True))

    @business_errors
    def post(self, request, project_id, batch_id):
        batch = self.batch(request, project_id, batch_id)
        class Decision(serializers.Serializer):
            action = serializers.ChoiceField(choices=['ACCEPT', 'REFUSE'])
            row_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False, min_length=1, max_length=1000)
            reason = serializers.CharField(max_length=10000, required=False, allow_blank=True, default='')
        data = Decision(data=request.data)
        data.is_valid(raise_exception=True)
        return Response(decide_batch(actor=request.user, batch=batch, **data.validated_data))
