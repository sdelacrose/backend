"""HISTORY-2: a tiny, read-only endpoint -- never a CRUD surface, never a replacement for the
Transmittal API itself (section 12/13). Access is exactly the Documents access, same helper
DocumentRevisionViewSet already uses: the Document is resolved from the accessible queryset
first (404 otherwise, same permission scoping as the Register/revisions endpoints).
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.services.client_distribution_history import (
    list_client_distribution_history,
)
from project_management.services.document_register import get_register_document_or_404


class ClientDistributionHistoryView(APIView):
    def get(self, request, document_id):
        document = get_register_document_or_404(request, document_id=document_id)
        return Response({"results": list_client_distribution_history(document)})
