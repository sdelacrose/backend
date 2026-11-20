"""HISTORY-2: a tiny, read-only endpoint -- never a CRUD surface, never a replacement for the
Transmittal API itself (section 12/13). Access is exactly the Documents access, same helper
DocumentRevisionViewSet already uses: the Document is resolved from the accessible queryset
first (404 otherwise, same permission scoping as the Register/revisions endpoints).
"""

from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.services.client_distribution_history import (
    list_client_distribution_history,
    mask_distribution_row,
)
from project_management.services.client_surveillance_opening import can_open_transmittal, can_view_client_surveillance_detail
from project_management.services.document_register import get_register_document_or_404


class ClientDistributionHistoryView(APIView):
    def get(self, request, document_id):
        document = get_register_document_or_404(request, document_id=document_id)
        rows = list_client_distribution_history(document)
        if not can_view_client_surveillance_detail(request.user, document):  # GED-SEC.2
            rows = [mask_distribution_row(row) for row in rows]
        elif not can_open_transmittal(request.user, document):
            # GED-WF.4.4-B: the reference stays (detail), but no identifier -> no link to a drawer they cannot open
            rows = [{**row, "transmittal_id": None} for row in rows]
        return Response({"results": rows})
