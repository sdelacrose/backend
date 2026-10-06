"""HISTORY-4: a tiny, read-only endpoint -- never a CRUD surface, same convention as HISTORY-2's
ClientDistributionHistoryView. Access is exactly the Documents access: the Document is resolved
from the accessible queryset first (404 otherwise, same helper every other History read path
already uses -- section 24/26).
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.services.document_history_events import (
    list_document_history_events,
)
from project_management.services.document_register import get_register_document_or_404


class DocumentHistoryEventsView(APIView):
    def get(self, request, document_id):
        document = get_register_document_or_404(request, document_id=document_id)
        return Response(
            {
                "document_origin": document.document_origin,
                "results": list_document_history_events(document),
            }
        )
