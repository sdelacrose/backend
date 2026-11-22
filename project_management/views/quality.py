"""QUALITY Q1: access check, evaluation list and dashboard counters.

Read-only. Tenant isolation: every query is restricted to the authenticated user's own company
(`SupplierEvaluation.company`), never to a company id sent by the client. Authorisation reuses the
existing role/permission catalogue (`quality.view` / `quality.full_access`, company scope).
"""

from django.db.models import Count, Q
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ecpmpapp.permissions import BaseModulePermission
from project_management.models import SupplierEvaluation

QUALITY_MODULE = "quality"
MAX_PAGE_SIZE = 200


def _has_quality_right(user, actions):
    if not user or not getattr(user, "company_id", None):
        return False
    checker = BaseModulePermission(QUALITY_MODULE, "view", "company")
    return any(
        checker._check_permission(user, QUALITY_MODULE, action, "company", {})
        for action in actions
    )


def user_can_view_quality(user):
    return _has_quality_right(user, ("view", "full_access"))


def user_can_manage_templates(user):
    return _has_quality_right(user, ("template_manage", "full_access"))


def evaluations_for_user(user):
    return SupplierEvaluation.objects.filter(company_id=user.company_id)


class _QualityView(APIView):
    permission_classes = [IsAuthenticated]

    def _forbidden(self):
        return Response({"detail": "You do not have permission to access Quality."}, status=403)


class QualityAccessView(_QualityView):
    def get(self, request):
        return Response({
            "can_view": user_can_view_quality(request.user),
            "can_manage_templates": user_can_manage_templates(request.user),
        })


class QualityDashboardView(_QualityView):
    def get(self, request):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        S = SupplierEvaluation.Status
        counts = evaluations_for_user(request.user).aggregate(
            total=Count("id"),
            in_progress=Count("id", filter=Q(status__in=[S.DRAFT, S.IN_PROGRESS])),
            pending_review=Count("id", filter=Q(status=S.PENDING_REVIEW)),
            approved_or_closed=Count("id", filter=Q(status__in=[S.APPROVED, S.CLOSED])),
        )
        return Response(counts)


def _serialize(evaluation):
    supplier = evaluation.supplier
    order = evaluation.purchase_order
    project = evaluation.project
    return {
        "id": evaluation.id,
        "reference": evaluation.reference,
        "status": evaluation.status,
        "evaluation_type": evaluation.evaluation_type,
        "supplier": {"id": supplier.id, "name": supplier.company_name},
        "purchase_order": {"id": order.id, "reference": order.reference} if order else None,
        "project": {"id": project.id, "name": project.project_name} if project else None,
        "fnc_count": evaluation.fnc_count,
        "created_at": evaluation.created_at,
        "updated_at": evaluation.updated_at,
        "closed_at": evaluation.closed_at,
    }


def _positive_int(value, default, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number < 1:
        return default
    return min(number, maximum) if maximum else number


class SupplierEvaluationListView(_QualityView):
    def get(self, request):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        params = request.query_params
        queryset = evaluations_for_user(request.user).select_related(
            "supplier", "purchase_order", "project"
        )
        status = params.get("status")
        if status in SupplierEvaluation.Status.values:
            queryset = queryset.filter(status=status)
        evaluation_type = params.get("evaluation_type")
        if evaluation_type in SupplierEvaluation.EvaluationType.values:
            queryset = queryset.filter(evaluation_type=evaluation_type)
        search = (params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search)
                | Q(supplier__company_name__icontains=search)
                | Q(purchase_order__reference__icontains=search)
            )
        page_size = _positive_int(params.get("page_size"), 25, MAX_PAGE_SIZE)
        page = _positive_int(params.get("page"), 1)
        total = queryset.count()
        start = (page - 1) * page_size
        rows = queryset[start:start + page_size]
        return Response({
            "count": total,
            "page": page,
            "page_size": page_size,
            "results": [_serialize(row) for row in rows],
        })
