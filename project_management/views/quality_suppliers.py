"""QUALITY Q7: supplier quality list and profile (read-only).

Reading needs `quality.view`; the report links additionally need the Documents right (`can_view_reports`), exactly as
on the evaluation page. A supplier outside the user's company scope is a 404, whatever id is sent.
"""

from django.http import Http404
from rest_framework.response import Response

from project_management.services.supplier_evaluation_scoring import FINAL_STATUSES
from project_management.services.supplier_quality import (
    current_performance,
    latest_finals,
    list_suppliers,
    performance_points,
    reference_date,
    section_comparison,
    supplier_history,
    supplier_overview,
    suppliers_for_user,
)
from project_management.views.quality import MAX_PAGE_SIZE, _positive_int, _QualityView, user_can_view_quality
from project_management.views.quality_report import can_read_documents


def _info(supplier):
    return {
        "id": supplier.id,
        "name": supplier.company_name,
        "city": supplier.company_town or "",
        "country": supplier.company_country or "",
    }


class QualitySuppliersView(_QualityView):
    def get(self, request):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        params = request.query_params
        queryset = list_suppliers(request.user, params.get("search"))
        company = params.get("company")
        if company and company.isdigit():  # used by the Network card to know whether a profile exists
            queryset = queryset.filter(pk=int(company))
        page_size = _positive_int(params.get("page_size"), 25, MAX_PAGE_SIZE)
        page = _positive_int(params.get("page"), 1)
        start = (page - 1) * page_size
        rows = list(queryset[start:start + page_size])
        finals = latest_finals(request.user, [row.id for row in rows])
        return Response({
            "count": queryset.count(),
            "page": page,
            "page_size": page_size,
            "results": [
                {
                    **_info(row),
                    "current": current_performance(finals.get(row.id)),
                    "open": row.evaluations_open,
                    "partial": row.evaluations_partial,
                    "fnc": row.evaluations_fnc,
                    "reports": row.reports_available,
                }
                for row in rows
            ],
        })


class QualitySupplierProfileView(_QualityView):
    def get(self, request, pk):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        supplier = suppliers_for_user(request.user).filter(pk=pk).first()
        if supplier is None:
            raise Http404
        history = supplier_history(request.user, supplier)
        points = performance_points(history)
        latest = points[-1] if points else None
        can_view_reports = can_read_documents(request.user)
        return Response({
            "supplier": {**_info(supplier), "network_id": supplier.id},
            "overview": supplier_overview(request.user, supplier),
            "current": latest,
            "history": [
                {
                    "id": e.id, "reference": e.reference,
                    "purchase_order": e.purchase_order.reference if e.purchase_order else None,
                    # Approved / closed: the reference date. Anything else: its creation date, the period is shown apart.
                    "date": reference_date(e) if e.status in FINAL_STATUSES else e.created_at,
                    "period_start": e.period_start, "period_end": e.period_end,
                    "evaluation_type": e.evaluation_type, "status": e.status,
                    "score": None if e.overall_score is None else float(e.overall_score),
                    "rating": e.rating, "follows_fnc": e.follows_fnc, "fnc_reference": e.fnc_reference,
                    "has_report": hasattr(e, "report"),
                }
                for e in history
            ],
            "can_view_reports": can_view_reports,
            "performance": points,
            "sections": section_comparison(request.user, supplier),
        })
