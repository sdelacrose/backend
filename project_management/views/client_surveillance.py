from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from ecpmpapp.permissions import LicensePermission
from django.http import FileResponse
from rest_framework import status as http_status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from project_management.models import (
    ClientIssue, ClientSurveillance, ClientSurveillanceEvent, ClientSurveillanceReturn, ClientSurveillanceReturnFile,
    Transmittal, TransmittalItem,
)
from project_management.permissions import TransmittalPermission, CorrespondencePermission
from project_management.services.client_surveillance_code import list_effective_codes
from project_management.services.client_surveillance_followup import next_step as followup_next_step, provide_file as provide_followup_file, replaceable as followup_replaceable, confirm_retained_return, retainable_returns, is_automated_document
from project_management.services.client_surveillance_return import (
    eligible_surveillance_correspondences, eligible_in_transmittals, incoming_transmittal_candidates, incoming_transmittal_candidate, issue_requires_category, issue_send_events, record_client_return, retained_return_id,
    validate_retained_return,
)
from project_management.services.document_register import get_register_document_or_404, assert_can_update_document
from project_management.services.client_distribution_history import list_client_distribution_history
from project_management.services.client_surveillance_opening import (
    can_manage_client_surveillance, can_open_transmittal, can_process_client_return, can_view_client_surveillance_detail, change_due_date,
)
from project_management.services.client_distribution_history import mask_distribution_row
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import PermissionDenied
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied

def serialize_surveillance(surveillance, detail=True):
    if surveillance is None:
        return None
    if not detail:
        # GED-SEC.2: summary only (status, result, dates, due date); the free comment, the opening Transmittal
        # reference and the stored events are detail.
        return {'id': surveillance.pk, 'reference': f'CS-{surveillance.pk}', 'status': surveillance.status,
            'result': surveillance.result, 'comment': None, 'created_at': surveillance.created_at,
            'updated_at': surveillance.updated_at, 'closed_at': surveillance.closed_at, 'opened_at': surveillance.opened_at,
            'review_duration_days': surveillance.review_duration_days, 'due_at': surveillance.due_at,
            'opening_transmittal': None, 'opening_send_event_id': None,
            'retained_return_id': retained_return_id(surveillance), 'events': [], 'detail_restricted': True}
    return {'id': surveillance.pk, 'reference': f'CS-{surveillance.pk}', 'status': surveillance.status,
        'result': surveillance.result, 'comment': surveillance.comment, 'created_at': surveillance.created_at,
        'updated_at': surveillance.updated_at, 'closed_at': surveillance.closed_at,
        # GED-WF.4.2: opening link, effective send instant, duration and due date (UTC instants).
        'opened_at': surveillance.opened_at, 'review_duration_days': surveillance.review_duration_days, 'due_at': surveillance.due_at,
        'opening_transmittal': ({'id': surveillance.opening_send_event.transmittal_id, 'reference': surveillance.opening_send_event.transmittal.transmittal_number}
            if surveillance.opening_send_event_id else None),
        'opening_send_event_id': surveillance.opening_send_event_id,
        # GED-WF.4.3: the return kept at closure (from the immutable closing event); null for legacy/open ones.
        'retained_return_id': retained_return_id(surveillance),
        'events': [{'id': e.pk, 'event_type': e.event_type, 'data': e.data, 'status': e.status, 'result': e.result, 'comment': e.comment,
            'actor': e.actor.full_name if e.actor else None, 'created_at': e.created_at} for e in surveillance.events.select_related('actor').all()]}

def proof_sources(issue, used, can_view, limit=150):
    """LOT A: the readable source-proof candidates (ranked: Surveillance IN of the client, other Surveillance IN, other IN),
    each with what the user needs to recognise it. The right to read each correspondence is checked here; the tier and
    the company/type confirmations are enforced again by record_client_return."""
    from project_management.models import ClientSurveillanceReturn
    candidates = [c for c in eligible_surveillance_correspondences(issue).select_related('sender_company').prefetch_related('attachments')[:limit * 3]
                  if c.pk not in used and can_view(c)][:limit]
    linked = {}
    for correspondence_id, issue_id in ClientSurveillanceReturn.objects.filter(source_correspondence__in=[c.pk for c in candidates], supersedes__isnull=True).values_list('source_correspondence_id', 'client_issue_id'):
        linked.setdefault(correspondence_id, set()).add(issue_id)
    rows = []
    for c in candidates:
        names = [a.file_name or '' for a in c.attachments.all()]
        eml = sum(1 for n in names if n.lower().endswith('.eml'))
        rows.append({
            'id': c.pk, 'reference': c.display_reference or c.reference or c.external_reference or '', 'subject': c.subject,
            'received_date': c.received_date, 'correspondence_type': c.correspondence_type, 'tier': c.proof_tier,
            'sender_company_id': c.sender_company_id, 'sender_company_name': c.sender_company.company_name if c.sender_company_id else None,
            'same_company': c.sender_company_id == issue.client_company_id, 'is_surveillance': (c.correspondence_type or '').lower() == 'surveillance',
            'has_original_email': eml > 0, 'attachment_count': len(names) - eml, 'linked_documents': len(linked.get(c.pk, set())),
        })
    return rows


def serialize_followup(followup, can_provide):
    """LOT 1: one automated follow-up, with the next business step derived from the real data."""
    revision = followup.derived_revision
    step = followup_next_step(followup)
    from project_management.services.client_surveillance_followup import SAME_INDEX, display_index
    return {
        'id': followup.pk, 'return_id': followup.client_return_id, 'category': followup.client_return.category,
        'code': followup.client_return.code_snapshot, 'expected_state': followup.expected_state, 'expected_index': followup.expected_index,
        'same_index': followup.client_return.category in SAME_INDEX, 'next_step': step['code'], 'next_step_detail': step['detail'],
        'derived_revision': ({'id': revision.pk, 'version_number': revision.version_number, 'revision_label': revision.revision_label,
            'index_label': display_index(revision), 'issue_state': revision.issue_state, 'file_name': revision.file_name, 'validated': bool(revision.revision_label)}
            if revision else None),
        'can_provide_file': bool(can_provide and not followup.closed_at and not followup.needs_review and followup_replaceable(followup)),
        'closed_at': followup.closed_at, 'applicable_at': followup.applicable_at, 'closing_send_event_id': followup.closing_send_event_id,
        'next_surveillance_id': followup.next_surveillance_id, 'needs_review': followup.needs_review or None,
    }


def serialize_retention(record):
    """LOT C: what the automatic retention of a just recorded return did (None when it was not attempted)."""
    outcome = getattr(record, 'retention', None)
    if outcome is None:
        return None
    followup = outcome['followup']
    return {'retained': outcome['retained'], 'automatic': outcome['retained'], 'reason': outcome['reason'],
            'followup': serialize_followup(followup, True) if followup is not None else None}


def surveillance_is_automated(surveillance):
    """Manual step transitions are removed for a surveillance driven by a follow-up; a surveillance with no follow-up
    (historical) or only follow-ups flagged for an explicit review keeps the manual path."""
    if surveillance is None:
        return False
    return surveillance.followups.filter(needs_review='').exists()


def return_file_url(document_id, issue_id, record_id, file_id):
    return f'/api/project_management/document/{document_id}/client-surveillance/{issue_id}/returns/{record_id}/files/{file_id}'


def serialize_return(record, document_id, surveillance, detail=True, can_view_correspondence=None):
    """One immutable return, with the code/labels frozen at recording time (never recomputed)."""
    closed_at = surveillance.closed_at if surveillance is not None else None
    corrections = list(record.corrections.all())
    if not detail:
        # GED-SEC.2: summary only (result category, frozen code and labels, dates, correction chain structure).
        return {
            'id': record.pk, 'client_issue_id': record.client_issue_id, 'category': record.category,
            'code': record.code_snapshot, 'label_en': record.label_en_snapshot, 'label_fr': record.label_fr_snapshot,
            'label_de': record.label_de_snapshot, 'received_at': record.received_at, 'comment': None,
            'source_reference': None, 'source_channel': None, 'source_correspondence': None, 'send_event_id': None, 'send_transmittal': None,
            'in_transmittal': None, 'supersedes_id': record.supersedes_id,
            'superseded_by_id': corrections[0].pk if corrections else None, 'correction_reason': None,
            'recorded_by': None, 'recorded_at': record.recorded_at,
            'late': bool(closed_at and record.recorded_at > closed_at), 'files': [], 'detail_restricted': True,
        }
    return {
        'id': record.pk, 'client_issue_id': record.client_issue_id, 'category': record.category,
        'code': record.code_snapshot, 'label_en': record.label_en_snapshot, 'label_fr': record.label_fr_snapshot,
        'label_de': record.label_de_snapshot, 'received_at': record.received_at, 'comment': record.comment,
        'source_reference': record.source_reference, 'source_channel': record.source_channel,
        'source_correspondence': ({'id': record.source_correspondence_id, 'reference': record.source_reference, 'project': record.source_correspondence.project_id}
            if record.source_correspondence_id and can_view_correspondence and can_view_correspondence(record.source_correspondence) else None),
        'send_event_id': record.send_event_id,
        'send_transmittal': ({'id': record.send_event.transmittal_id, 'reference': record.send_event.transmittal.transmittal_number,
                              'sent_at': record.send_event.created_at} if record.send_event_id else None),
        'in_transmittal': ({'id': record.in_transmittal_id, 'reference': record.in_transmittal.transmittal_number}
                           if record.in_transmittal_id else None),
        'supersedes_id': record.supersedes_id, 'superseded_by_id': corrections[0].pk if corrections else None,
        'correction_reason': record.correction_reason,
        'recorded_by': record.recorded_by.full_name if record.recorded_by_id else None, 'recorded_at': record.recorded_at,
        'late': bool(closed_at and record.recorded_at > closed_at),
        'files': [{'id': f.pk, 'file_name': f.file_name, 'content_type': f.content_type, 'file_size': f.file_size,
                   'sha256': f.sha256, 'kind': f.kind, 'download_url': return_file_url(document_id, record.client_issue_id, record.pk, f.pk)}
                  for f in record.files.all()],
    }


class ClientSurveillanceView(APIView):
    permission_classes = [IsAuthenticated, LicensePermission]
    def get(self, request, document_id, issue_id=None):
        document = get_register_document_or_404(request, document_id=document_id)
        distributions = list_client_distribution_history(document)
        detail = can_view_client_surveillance_detail(request.user, document)
        allowed = {row['client_issue_id'] for row in distributions if row['client_issue_id']}
        issues = ClientIssue.objects.filter(pk__in=allowed, document_revision__document=document).select_related(
            'client_company', 'surveillance', 'surveillance__opening_send_event__transmittal')
        can_manage = can_manage_client_surveillance(request.user, document)
        can_process = can_process_client_return(request.user, document)  # LOT 2: documents.update
        from project_management.services.document_register import can_update_document
        can_update = can_update_document(request.user, document)
        project = document.folder.project
        codes = list_effective_codes(project)
        rows = []
        for issue in issues:
            surveillance = getattr(issue, 'surveillance', None)
            row = {'client_issue_id': issue.pk, 'document_revision_id': issue.document_revision_id,
                'client_company_name': issue.client_company.company_name, 'client_company_id': issue.client_company_id, 'client_revision': issue.client_revision_label,
                'source_revision': next((d['source_revision'] for d in distributions if d['document_revision_id'] == issue.document_revision_id), None),
                'surveillance': serialize_surveillance(surveillance, detail), 'can_manage': can_manage, 'can_process': can_process, 'detail_visible': detail,
                # GED-WF.4.3: the returns (also for a distribution without surveillance), the sends they can answer,
                # whether a result is mandatory, the project's visible codes and the confirmed incoming transmittals.
                'returns': [serialize_return(r, document.pk, surveillance, detail, lambda correspondence: CorrespondencePermission('view').has_object_permission(request, None, correspondence)) for r in issue.client_returns.select_related(
                    'send_event__transmittal', 'in_transmittal', 'recorded_by', 'source_correspondence').prefetch_related('files', 'corrections')],
                'requires_category': issue_requires_category(issue), 'codes': codes}
            if surveillance is not None and detail:
                row['followups'] = [serialize_followup(f, can_update) for f in surveillance.followups.select_related('client_return', 'derived_revision')]
                row['surveillance']['automated'] = surveillance_is_automated(surveillance)
                row['internal_document'] = is_automated_document(document)  # LOT C: drives the standard path / the folded administrative closure
                # LOT 2: the returns that can still be retained (the explicit confirmation is a distinct operation)
                row['retainable_returns'] = [r.pk for r in retainable_returns(issue)] if can_process and surveillance.status != 'CLOSED' else []
            else:
                row['followups'] = []
            if can_process:
                used = set(issue.client_returns.filter(source_correspondence__isnull=False, supersedes__isnull=True).values_list('source_correspondence_id', flat=True))
                row['surveillance_correspondences'] = proof_sources(issue, used, lambda correspondence: CorrespondencePermission('view').has_object_permission(request, None, correspondence))
                purposes = {i.transmittal_id: i.distribution_purpose for i in TransmittalItem.objects.filter(client_issue=issue)}
                row['send_events'] = [{'id': e.pk, 'transmittal_id': e.transmittal_id, 'transmittal_reference': e.transmittal.transmittal_number,
                    'sent_at': e.created_at, 'distribution_purpose': purposes.get(e.transmittal_id)} for e in issue_send_events(issue)]
                row['in_transmittals'] = [{'id': t.pk, 'reference': t.transmittal_number, 'subject': t.subject, 'received_at': t.received_at}
                    for t in eligible_in_transmittals(issue, project.pk)
                    if TransmittalPermission('view').has_object_permission(request, None, t)]
                row['incoming_candidates'] = [incoming_transmittal_candidate(t, issue.client_company_id)
                    for t in incoming_transmittal_candidates(project.pk)
                    if TransmittalPermission('view').has_object_permission(request, None, t)]
            rows.append(row)
        if issue_id is not None:
            rows = [row for row in rows if row['client_issue_id'] == issue_id]
            if not rows:
                raise Http404
        links = can_open_transmittal(request.user, document)  # GED-WF.4.4-B: an identifier only for who can open it
        if not detail:
            distributions = [mask_distribution_row(d) for d in distributions]
        elif not links:
            distributions = [{**d, 'transmittal_id': None} for d in distributions]
        # B1 / LOT 4: applicability per client company for the CURRENT logical version (read only, never global)
        from project_management.services.client_applicability import client_applicability
        applicability = client_applicability(document, include_transmittal=bool(detail and links))
        return Response({'results': rows, 'distributions': distributions, 'transmittal_links': links, 'applicability': applicability})

    @transaction.atomic
    def post(self, request, document_id, issue_id):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        allowed = {row['client_issue_id'] for row in list_client_distribution_history(document)}
        if issue_id not in allowed:
            raise Http404
        issue = ClientIssue.objects.select_for_update().filter(pk=issue_id, document_revision__document=document).first()
        if issue is None:
            raise Http404
        surveillance, created = ClientSurveillance.objects.get_or_create(client_issue=issue, defaults={'created_by': request.user})
        if created:
            ClientSurveillanceEvent.objects.create(surveillance=surveillance, actor=request.user, status=surveillance.status)
        return Response(serialize_surveillance(surveillance), status=201 if created else 200)

    @transaction.atomic
    def patch(self, request, document_id, issue_id):
        document = get_register_document_or_404(request, document_id=document_id)
        if "client_revision" in request.data:
            raise ValidationError({"client_revision": "The issued revision is defined by document distribution and cannot be changed during client surveillance."})
        class Input(serializers.Serializer):
            status = serializers.ChoiceField(choices=ClientSurveillance.Status.choices)
            result = serializers.ChoiceField(choices=ClientSurveillance.Result.choices, allow_null=True, required=False)
            comment = serializers.CharField(allow_blank=True, required=False, max_length=10000)
            retained_return = serializers.IntegerField(required=False, allow_null=True)
        payload = Input(data=request.data)
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        # GED-WF.4.3: closing with a result needs documents.client_surveillance_manage; documents.update alone
        # still moves the intermediate steps (DRAFT -> UNDER_REVIEW -> READY_FOR_CLOSURE).
        if values['status'] == 'CLOSED':
            if not can_manage_client_surveillance(request.user, document):
                raise PermissionDenied('You do not have permission to close this client surveillance.')
        else:
            assert_can_update_document(request.user, document)
        if issue_id not in {row['client_issue_id'] for row in list_client_distribution_history(document)}:
            raise Http404
        surveillance = ClientSurveillance.objects.select_for_update().filter(client_issue_id=issue_id, client_issue__document_revision__document=document).first()
        if surveillance is None:
            raise Http404
        if surveillance.status == 'CLOSED':
            raise ValidationError('A closed client surveillance cannot be changed.')
        if surveillance_is_automated(surveillance):
            raise ValidationError({'status': 'This client surveillance follows its client return automatically: it is closed by the verified send of the resulting revision, not by hand.'})
        order = list(ClientSurveillance.Status.values)
        if values['status'] != 'CLOSED' and (order.index(values['status']) < order.index(surveillance.status) or order.index(values['status']) > order.index(surveillance.status) + 1):
            raise ValidationError({'status': 'Advance one step at a time; a completed step cannot be reversed.'})
        if values.get('result'):
            raise ValidationError({'result': 'The result comes from the client return retained at closure.'})
        comment = values.get('comment', surveillance.comment)
        retained = None
        if values['status'] == 'CLOSED':
            try:
                retained = validate_retained_return(surveillance, values.get('retained_return'))
            except DjangoValidationError as error:
                raise ValidationError(error.message_dict if hasattr(error, 'message_dict') else error.messages)
            comment = (comment or '').strip() or retained.comment
        elif values.get('retained_return'):
            raise ValidationError({'retained_return': 'A return is retained only when the surveillance is closed.'})
        surveillance.status, surveillance.comment = values['status'], comment
        if retained is not None:
            surveillance.result = retained.category
            surveillance.closed_at = timezone.now()
        surveillance.save()
        ClientSurveillanceEvent.objects.create(
            surveillance=surveillance, actor=request.user, status=surveillance.status, result=surveillance.result, comment=comment,
            data=({'retained_return': retained.pk, 'category': retained.category, 'code': retained.code_snapshot} if retained is not None else None))
        return Response(serialize_surveillance(surveillance))


class ClientSurveillanceDueDateView(APIView):
    """GED-WF.4.2: manual, traced change of the effective due date. Needs documents.client_surveillance_manage
    in the document's project (documents.update alone never suffices); the read access check comes first."""
    permission_classes = [IsAuthenticated, LicensePermission]

    def patch(self, request, document_id, issue_id):
        document = get_register_document_or_404(request, document_id=document_id)
        if not can_manage_client_surveillance(request.user, document):
            raise PermissionDenied('You do not have permission to manage this client surveillance.')
        from project_management.services.document_withdrawal import WITHDRAWN_MESSAGE, is_withdrawn
        if is_withdrawn(document):
            raise ValidationError({'detail': WITHDRAWN_MESSAGE})
        if issue_id not in {row['client_issue_id'] for row in list_client_distribution_history(document)}:
            raise Http404
        surveillance = ClientSurveillance.objects.filter(client_issue_id=issue_id, client_issue__document_revision__document=document).first()
        if surveillance is None:
            raise Http404
        class Input(serializers.Serializer):
            due_at = serializers.DateTimeField()
            reason = serializers.CharField(allow_blank=True, required=False, max_length=500)
        payload = Input(data=request.data)
        payload.is_valid(raise_exception=True)
        try:
            updated = change_due_date(surveillance, payload.validated_data['due_at'], payload.validated_data.get('reason', ''), request.user)
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict if hasattr(error, 'message_dict') else error.messages)
        return Response(serialize_surveillance(updated))


class DocumentTimelineTargetsView(APIView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def get(self, request, document_id):
        from project_management.services.document_timeline_targets import document_timeline_targets
        document = get_register_document_or_404(request, document_id=document_id)
        return Response(document_timeline_targets(document))


def _allowed_issue(document, issue_id):
    if issue_id not in {row['client_issue_id'] for row in list_client_distribution_history(document)}:
        raise Http404
    issue = ClientIssue.objects.filter(pk=issue_id, document_revision__document=document).first()
    if issue is None:
        raise Http404
    return issue


class ClientSurveillanceReturnView(APIView):
    """GED-WF.4.3: record a client return (new, or correcting a previous one) with its annotated files.
    LOT 2: needs documents.update on the document (project or company scope), like any document work; attaching an
    incoming transmittal also needs transmittal_view on it. documents.client_surveillance_manage is no longer required."""
    permission_classes = [IsAuthenticated, LicensePermission]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def post(self, request, document_id, issue_id):
        document = get_register_document_or_404(request, document_id=document_id)
        if not can_process_client_return(request.user, document):
            raise PermissionDenied('You do not have permission to record a client return.')
        from project_management.services.document_withdrawal import WITHDRAWN_MESSAGE, is_withdrawn
        if is_withdrawn(document):
            raise ValidationError({'detail': WITHDRAWN_MESSAGE})
        issue = _allowed_issue(document, issue_id)

        class Input(serializers.Serializer):
            category = serializers.ChoiceField(choices=ClientSurveillanceReturn.Category.choices, allow_null=True, allow_blank=True, required=False)
            received_at = serializers.DateTimeField()
            source_channel = serializers.ChoiceField(choices=ClientSurveillanceReturn.Channel.choices)
            comment = serializers.CharField(allow_blank=True, required=False, max_length=10000)
            source_reference = serializers.CharField(allow_blank=True, required=False, max_length=255)
            send_event = serializers.IntegerField(required=False, allow_null=True)
            in_transmittal = serializers.IntegerField(required=False, allow_null=True)
            source_correspondence = serializers.IntegerField(required=False, allow_null=True)
            supersedes = serializers.IntegerField(required=False, allow_null=True)
            correction_reason = serializers.CharField(allow_blank=True, required=False, max_length=2000)
            confirm_other_company = serializers.BooleanField(required=False)
            confirm_other_type = serializers.BooleanField(required=False)
            auto_retain = serializers.BooleanField(required=False)  # LOT C: default true; false = record only

        data = request.data
        payload = Input(data={k: (None if data.get(k) in ('', 'null') and k in ('send_event', 'in_transmittal', 'supersedes', 'source_correspondence') else data.get(k))
                              for k in Input().fields if data.get(k) is not None})
        payload.is_valid(raise_exception=True)
        values = payload.validated_data
        uploads = request.FILES.getlist('files') if hasattr(request.FILES, 'getlist') else []
        kinds = data.getlist('file_kinds') if hasattr(data, 'getlist') else []

        def can_view_transmittal(transmittal):
            return TransmittalPermission('view').has_object_permission(request, None, transmittal)

        try:
            record = record_client_return(
                issue, request.user, category=values.get('category') or None, received_at=values['received_at'],
                source_channel=values['source_channel'], comment=values.get('comment', ''),
                source_reference=values.get('source_reference', ''), send_event_id=values.get('send_event'),
                in_transmittal_id=values.get('in_transmittal'), supersedes_id=values.get('supersedes'),
                correction_reason=values.get('correction_reason', ''), uploads=uploads, kinds=kinds,
                can_view_transmittal=can_view_transmittal, source_correspondence_id=values.get('source_correspondence'),
                can_view_correspondence=lambda correspondence: CorrespondencePermission('view').has_object_permission(request, None, correspondence),
                confirm_other_company=bool(values.get('confirm_other_company')), confirm_other_type=bool(values.get('confirm_other_type')),
                auto_retain=values.get('auto_retain', True))
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict if hasattr(error, 'message_dict') else error.messages)
        except DjangoPermissionDenied as error:
            raise PermissionDenied(str(error))
        retention = serialize_retention(record)
        record = ClientSurveillanceReturn.objects.select_related('send_event__transmittal', 'in_transmittal', 'recorded_by').prefetch_related('files', 'corrections').get(pk=record.pk)
        data = serialize_return(record, document.pk, getattr(issue, 'surveillance', None), can_view_correspondence=lambda correspondence: CorrespondencePermission('view').has_object_permission(request, None, correspondence))
        if retention is not None:
            data['retention'] = retention
        return Response(data, status=http_status.HTTP_201_CREATED)


class ClientSurveillanceReturnFileView(APIView):
    """GED-WF.4.3: authenticated, forced download of one kept return file (never a public link). The document
    must be readable by the user (project / company scope) and the file must belong to that document's issue."""
    permission_classes = [IsAuthenticated, LicensePermission]

    def get(self, request, document_id, issue_id, return_id, file_id):
        document = get_register_document_or_404(request, document_id=document_id)
        # GED-SEC.2: annotated files are detail -- refused (403) before any lookup, so a reader without the specific
        # right learns nothing about which file ids exist.
        if not can_view_client_surveillance_detail(request.user, document):
            raise PermissionDenied('You do not have permission to download client return files.')
        _allowed_issue(document, issue_id)
        stored = ClientSurveillanceReturnFile.objects.select_related('blob').filter(
            pk=file_id, client_return_id=return_id, client_return__client_issue_id=issue_id,
            client_return__client_issue__document_revision__document=document).first()
        if stored is None or not stored.blob.file or not stored.blob.file.storage.exists(stored.blob.file.name):
            raise Http404
        response = FileResponse(stored.blob.file.open('rb'), as_attachment=True, filename=stored.file_name,
                                content_type=stored.content_type or 'application/octet-stream')
        response['X-Content-Type-Options'] = 'nosniff'
        return response


class ClientSurveillanceRetainedReturnView(APIView):
    """LOT 2: explicit confirmation of the retained return (the only operation that opens the documentary follow-up).
    documents.update on the document; body: {"return": <id>} -- optional when a single return can be retained, mandatory
    when several can. Business refusals are 400 answers carrying a machine-readable `code`, never a 500."""
    permission_classes = [IsAuthenticated, LicensePermission]

    def post(self, request, document_id, issue_id):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        issue = _allowed_issue(document, issue_id)
        raw = request.data.get('return')
        try:
            return_id = int(raw) if raw not in (None, '', 'null') else None
        except (TypeError, ValueError):
            raise ValidationError({'return': 'The return must be an identifier.', 'code': 'unknown_return'})
        try:
            followup = confirm_retained_return(issue, request.user, return_id)
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict if hasattr(error, 'message_dict') else error.messages)
        from project_management.models import ClientSurveillanceFollowUp
        followup = ClientSurveillanceFollowUp.objects.select_related('client_return', 'derived_revision').get(pk=followup.pk)
        return Response(serialize_followup(followup, True), status=http_status.HTTP_201_CREATED)


class ClientSurveillanceFollowUpFileView(APIView):
    """LOT 1: provide the physical file that answers a client return (BPE version of the same index for code A, next
    index otherwise). Needs the right to update the document; the filiation is recorded server side."""
    permission_classes = [IsAuthenticated, LicensePermission]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, document_id, issue_id, followup_id):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_can_update_document(request.user, document)
        issue = _allowed_issue(document, issue_id)
        from project_management.models import ClientSurveillanceFollowUp
        followup = ClientSurveillanceFollowUp.objects.select_related('client_return', 'derived_revision').filter(pk=followup_id, client_return__client_issue=issue).first()
        if followup is None:
            raise Http404
        try:
            provide_followup_file(followup, request.user, request.FILES.get('file'), request.data.get('comment', ''),
                                  confirm_bpe=str(request.data.get('confirm_bpe', '')).lower() in ('1', 'true', 'yes'))
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict if hasattr(error, 'message_dict') else error.messages)
        followup = ClientSurveillanceFollowUp.objects.select_related('client_return', 'derived_revision').get(pk=followup.pk)
        return Response(serialize_followup(followup, True), status=http_status.HTTP_201_CREATED)
