"""Recipe-only file access using the existing LINKS token and document permissions."""
import mimetypes
import posixpath

from django.conf import settings
from django.http import FileResponse, Http404
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

from ecpmpapp.permissions import LicensePermission
from models.models import BlockedToken
from project_management.models import Document, DocumentAttachment, DocumentRevision
from project_management.services.document_register import get_register_document_or_404


class DocumentCookieJWTAuthentication(JWTAuthentication):
    """The browser already sends this same-origin LINKS cookie for file requests.

    Validate it exactly as a bearer token; never accept an unsigned payload or
    a blocked token. This authentication class is used only by this read-only view.
    """
    def authenticate(self, request):
        header = self.get_header(request)
        raw = self.get_raw_token(header) if header is not None else None
        if raw is None:
            raw = request.COOKIES.get('auth-token')
        if not raw:
            return None
        validated = self.get_validated_token(raw)
        user = self.get_user(validated)
        token = raw.decode() if isinstance(raw, bytes) else raw
        if BlockedToken.objects.filter(token=token, is_blocked=True).exists():
            raise AuthenticationFailed('This token has been logged out.')
        return user, validated


class RecetteDocumentFileView(APIView):
    authentication_classes = [DocumentCookieJWTAuthentication]
    permission_classes = [IsAuthenticated, LicensePermission]
    http_method_names = ['get', 'head', 'options']

    def initial(self, request, *args, **kwargs):
        if not getattr(settings, 'RECETTE_MODE', False):
            raise Http404
        return super().initial(request, *args, **kwargs)

    def get(self, request, relative_path):
        if (not relative_path or '\\' in relative_path or
                any(ord(char) < 32 for char in relative_path) or
                any(part in ('', '.', '..') for part in relative_path.split('/'))):
            raise Http404
        stored_name = 'documents/' + relative_path
        # A URL is never treated as a filesystem path. Only registered GED files
        # qualify, and each one must pass the existing Document access resolver.
        for model, document_key in ((Document, 'pk'), (DocumentRevision, 'document_id'),
                                    (DocumentAttachment, 'document_id')):
            for record in model.objects.filter(file=stored_name).iterator():
                try:
                    get_register_document_or_404(request, document_id=getattr(record, document_key))
                except Http404:
                    continue
                filename = posixpath.basename(record.file_name or stored_name)
                content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                inline = content_type in {
                    'application/pdf', 'image/png', 'image/jpeg', 'image/gif',
                    'image/webp', 'image/bmp', 'text/plain',
                }
                try:
                    response = FileResponse(record.file.open('rb'), as_attachment=not inline,
                                            filename=filename, content_type=content_type)
                except (FileNotFoundError, OSError):
                    raise Http404
                response['Cache-Control'] = 'private, no-store'
                response['Vary'] = 'Cookie, Authorization'
                response['X-Content-Type-Options'] = 'nosniff'
                response['Cross-Origin-Resource-Policy'] = 'same-origin'
                response['Content-Security-Policy'] = "sandbox; default-src 'none'"
                return response
        raise Http404
