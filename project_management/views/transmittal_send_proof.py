import hashlib
import io
from django.http import FileResponse, Http404
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from project_management.models import TransmittalSendProof
from project_management.permissions import TransmittalPermission
from project_management.views.transmittal import _transmittal_for_action
from project_management.services.transmittal_send_proof import proof_enabled, archive_graph_original, _limit
from userapis.mailbox_read import MailboxReadError


class TransmittalProofView(viewsets.GenericViewSet):
    def _parent(self, request, transmittal_id):
        return _transmittal_for_action(request, transmittal_id, 'view')

    def list(self, request, transmittal_id=None):
        parent = self._parent(request, transmittal_id)
        if not proof_enabled():
            return Response({'enabled': False, 'proofs': []})
        can_issue = TransmittalPermission('issue').has_object_permission(request, None, parent)
        proofs = TransmittalSendProof.objects.filter(event__transmittal=parent).prefetch_related('files')
        return Response({'enabled': True, 'proofs': [{
            'id': p.pk, 'event': p.event_id, 'provider': p.provider, 'state': p.state,
            'error_code': p.error_code, 'archived_at': p.archived_at,
            'can_archive': can_issue and p.provider == 'graph' and p.state != 'ARCHIVED',
            'files': [{'id': f.pk, 'kind': f.kind, 'file_name': f.file_name,
                       'sha256': f.sha256, 'file_size': f.file_size,
                       'can_download': can_issue or f.kind == 'attachment'} for f in p.files.all()],
        } for p in proofs]})

    def _proof(self, parent, proof_id):
        if not proof_enabled():
            raise Http404
        proof = TransmittalSendProof.objects.filter(pk=proof_id, event__transmittal=parent).first()
        if proof is None:
            raise Http404
        return proof

    def archive(self, request, transmittal_id=None, proof_id=None):
        parent = self._parent(request, transmittal_id)
        _transmittal_for_action(request, transmittal_id, 'issue')
        proof = self._proof(parent, proof_id)
        try:
            proof = archive_graph_original(proof.pk, actor=request.user)
        except MailboxReadError:
            raise PermissionDenied('Mailbox read access is required to archive its original.')
        return Response({'id': proof.pk, 'state': proof.state, 'error_code': proof.error_code})

    def download(self, request, transmittal_id=None, proof_id=None, file_id=None):
        parent = self._parent(request, transmittal_id)
        proof = self._proof(parent, proof_id)
        evidence = proof.files.select_related('blob').filter(pk=file_id).first()
        if evidence is None:
            raise Http404
        if evidence.kind != 'attachment':
            _transmittal_for_action(request, transmittal_id, 'issue')
        blob = evidence.blob
        if blob.company_profile_id != parent.project.company_profile_id:
            raise Http404
        try:
            with blob.file.open('rb') as stream:
                content = stream.read(_limit() + 1)
        except (OSError, ValueError):
            return Response({'error': 'evidence_unavailable'}, status=409)
        if len(content) > _limit() or len(content) != evidence.file_size or hashlib.sha256(content).hexdigest() != evidence.sha256:
            return Response({'error': 'evidence_integrity_failed'}, status=409)
        response = FileResponse(io.BytesIO(content), as_attachment=True, filename=evidence.file_name,
                                content_type='message/rfc822' if evidence.kind != 'attachment' else 'application/octet-stream')
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        return response
