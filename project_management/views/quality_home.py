"""QUALITY UX-01: the counters of the Quality home page (real counts only, company scope, read-only).

Kept apart from `quality/dashboard`, whose payload is already consumed as is."""

from django.db.models import Count
from rest_framework.response import Response

from project_management.models import SupplierEvaluation
from project_management.services.supplier_evaluation import evaluable_orders, orders_to_evaluate
from project_management.services.supplier_quality import supplier_ids
from project_management.views.quality import _QualityView, evaluations_for_user, user_can_view_quality

S = SupplierEvaluation.Status


class QualityHomeView(_QualityView):
    def get(self, request):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        user = request.user
        evaluations = evaluations_for_user(user)
        by_status = {row["status"]: row["n"] for row in evaluations.values("status").annotate(n=Count("id"))}
        count = lambda *statuses: sum(by_status.get(status, 0) for status in statuses)  # noqa: E731
        return Response({
            "orders": {
                "total": evaluable_orders(user).count(),
                "not_evaluated": orders_to_evaluate(user).filter(evaluations_count=0).count(),
            },
            "reviews_waiting": evaluations.filter(reviewer=user, status__in=[S.SUBMITTED, S.UNDER_REVIEW]).count(),
            "suppliers": len(supplier_ids(user)),
            "evaluations": {
                "to_start": count(S.DRAFT),
                "in_progress": count(S.IN_PROGRESS, S.REJECTED),
                "under_review": count(S.SUBMITTED, S.UNDER_REVIEW),
                "approved": count(S.APPROVED),
                "closed": count(S.CLOSED),
            },
        })
