"""External supplier contract: whitelist fields; no internal serializers or storage URLs."""
from functools import wraps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import serializers
from ecpmpapp.permissions import LicensePermission

from project_management.models import SupplierSurveillance
from project_management.models.supplier_portal import SupplierPortalGrant, SupplierPortalReturn
from project_management.services.document_register import get_register_document_or_404
from project_management.services.supplier_portal import (
    portal_grants, portal_grant, visible_revisions, grant_access, revoke_access,
    publish_return, submit_revision, assert_internal_manager,
)
from project_management.views.recette_document_file import DocumentCookieJWTAuthentication


def business_errors(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except ValidationError as error:
            return Response(getattr(error, 'message_dict', None) or {'detail': error.messages}, status=400)
    return wrapped


def revision_data(revision):
    return {'id': revision.pk, 'version': revision.version_number, 'external_revision': revision.external_revision,
            'file_name': revision.file_name, 'submitted_at': revision.created_at}


def document_data(grant):
    return {'id': grant.document_id, 'reference': grant.document.identification_number,
            'title': grant.document.file_name, 'project': {'id': grant.project_id, 'name': grant.project.project_name},
            'supplier': grant.supplier.company_name}


class SupplierPortalView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        # Enable only after the schema and nominated supplier accounts have been validated.
        if not getattr(settings, 'SUPPLIER_PORTAL_ENABLED', False):
            raise Http404
        return super().initial(request, *args, **kwargs)


class SupplierPortalDocuments(SupplierPortalView):

    def get(self, request):
        return Response({'documents': [document_data(g) for g in portal_grants(request.user)]})


class SupplierPortalDocument(SupplierPortalView):
    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        grant = portal_grant(request.user, document_id)
        header = document_data(grant)
        if getattr(settings, 'SUPPLIER_REGISTER_ENABLED', False):
            from supplier_portal.queries import public_document
            header = public_document(grant.document)
        returns = SupplierPortalReturn.objects.filter(surveillance__document_revision__document_id=document_id,
            supplier_id=grant.supplier_id, project_id=grant.project_id).select_related('surveillance__document_revision').order_by('-published_at')
        return Response({**header, 'revisions': [revision_data(r) for r in visible_revisions(grant)],
                         'returns': [{'id': r.pk, 'revision': r.surveillance.document_revision_id,
                                      'external_revision': r.surveillance.document_revision.external_revision,
                                      'version': r.surveillance.document_revision.version_number, 'result': r.result,
                                      'new_revision_required': r.new_revision_required, 'comment': r.public_comment,
                                      'published_at': r.published_at} for r in returns]})

    @business_errors
    def post(self, request, document_id):
        class SubmissionInput(serializers.Serializer):
            file = serializers.FileField()
            external_revision = serializers.CharField(max_length=20, required=False, allow_blank=True, default='')
            comment = serializers.CharField(max_length=10000, required=False, allow_blank=True, default='')
        data = SubmissionInput(data=request.data)
        data.is_valid(raise_exception=True)
        import json
        try: feedback_responses=json.loads(request.data.get('feedback_responses','[]'))
        except (ValueError,TypeError): return Response({'feedback_responses':'Invalid response list.'},status=400)
        result = submit_revision(user=request.user, document_id=document_id, uploaded_file=data.validated_data['file'],
                                 external_revision=data.validated_data['external_revision'], comment=data.validated_data['comment'], feedback_responses=feedback_responses)
        return Response(revision_data(result.revision), status=201)


class SupplierPortalDownload(SupplierPortalView):
    authentication_classes = [DocumentCookieJWTAuthentication]
    permission_classes = [IsAuthenticated]

    def get(self, request, document_id, revision_id):
        grant = portal_grant(request.user, document_id)
        revision = visible_revisions(grant).filter(pk=revision_id).first()
        if revision is None:
            raise Http404
        try:
            response = FileResponse(revision.file.open('rb'), as_attachment=True,
                                    filename=revision.file_name or 'document', content_type='application/octet-stream')
        except (OSError, ValueError):
            raise Http404
        response['Cache-Control'] = 'private, no-store'
        response['X-Content-Type-Options'] = 'nosniff'
        response['Content-Security-Policy'] = "sandbox; default-src 'none'"
        return response


class SupplierPortalAccess(SupplierPortalView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def managed_document(self, request, document_id):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_internal_manager(request.user, document)
        return document

    def get(self, request, document_id):
        document = self.managed_document(request, document_id)
        rows = SupplierPortalGrant.objects.filter(document=document).select_related('user').order_by('-created_at')
        return Response({'grants': [{'id': g.pk, 'email': g.user.email, 'created_at': g.created_at, 'revoked_at': g.revoked_at} for g in rows]})

    @business_errors
    def post(self, request, document_id):
        document = self.managed_document(request, document_id)
        class GrantInput(serializers.Serializer):
            email = serializers.EmailField()
        data = GrantInput(data=request.data)
        data.is_valid(raise_exception=True)
        grant = grant_access(document=document, actor=request.user, email=data.validated_data['email'])
        return Response({'id': grant.pk}, status=201)

    @business_errors
    def delete(self, request, document_id):
        document = self.managed_document(request, document_id)
        class RevokeInput(serializers.Serializer):
            grant = serializers.IntegerField(min_value=1)
        data = RevokeInput(data=request.data)
        data.is_valid(raise_exception=True)
        grant = SupplierPortalGrant.objects.filter(document=document, pk=data.validated_data['grant']).first()
        if grant is None:
            raise Http404
        revoke_access(grant=grant, actor=request.user)
        return Response(status=204)


class SupplierPortalPublish(SupplierPortalView):
    permission_classes = [IsAuthenticated, LicensePermission]

    def managed_surveillance(self, request, document_id, surveillance_id):
        document = get_register_document_or_404(request, document_id=document_id)
        assert_internal_manager(request.user, document)
        surveillance = SupplierSurveillance.objects.filter(pk=surveillance_id, document_revision__document=document).select_related('document_revision__document').first()
        if surveillance is None:
            raise Http404
        return surveillance

    def get(self, request, document_id, surveillance_id):
        surveillance = self.managed_surveillance(request, document_id, surveillance_id)
        publication = SupplierPortalReturn.objects.filter(surveillance=surveillance).first()
        return Response({'published_at': publication.published_at if publication else None})

    @business_errors
    def post(self, request, document_id, surveillance_id):
        surveillance = self.managed_surveillance(request, document_id, surveillance_id)
        class ReturnInput(serializers.Serializer):
            public_comment = serializers.CharField(max_length=20000, required=False, allow_blank=True, default='')
        data = ReturnInput(data=request.data)
        data.is_valid(raise_exception=True)
        result = publish_return(surveillance=surveillance, actor=request.user, **data.validated_data)
        return Response({'id': result.pk, 'published_at': result.published_at}, status=201)
