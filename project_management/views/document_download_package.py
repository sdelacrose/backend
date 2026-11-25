from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from project_management.services.document_download_package import build_document_download_package
from project_management.services.document_register import get_register_document_or_404
from project_management.views.document_revision import private_file_response


class DocumentDownloadPackageView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, document_id):
        document = get_register_document_or_404(request, document_id=document_id)
        archive, filename = build_document_download_package(document)
        try:
            return private_file_response(archive, filename, content_type='application/zip')
        except BaseException:
            archive.close()
            raise
