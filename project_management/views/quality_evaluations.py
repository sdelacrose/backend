"""QUALITY Q3: orders to evaluate, evaluation creation / detail, questionnaire answers.

Every lookup is restricted to the authenticated user's company; identifiers sent by the client are only
ever matched inside that scope (another company's order, template or evaluation is a 404 / a validation
error, never readable). Reading needs `quality.view`; creating and answering need
`quality.evaluation_manage` (or `quality.full_access`).
"""

from datetime import date

from django.http import Http404
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from project_management.models import SupplierEvaluation
from project_management.services.supplier_evaluation_scoring import FINAL_STATUSES, evaluation_scoring
from project_management.services.supplier_evaluation import (
    EDITABLE_STATUSES,
    create_supplier_evaluation,
    evaluation_progress,
    orders_to_evaluate,
    save_responses,
)
from project_management.services.supplier_evaluation_workflow import (
    allowed_actions,
    is_owner,
    submission_check,
)
from project_management.views.quality import SupplierEvaluationListView
from project_management.views.quality import (
    MAX_PAGE_SIZE,
    _positive_int,
    _QualityView,
    evaluations_for_user,
    user_can_manage_evaluations,
    user_can_view_quality,
)


def _name(user):
    return None if not user else ((user.full_name or "").strip() or user.email)


def _date(value, field):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Enter a valid date (YYYY-MM-DD)."})


def _float(value):
    return None if value is None else float(value)


def scoring_payload(evaluation):
    """Everything the interface shows about results; all numbers come from the scoring service. Approved and
    closed evaluations show their STORED result; open ones show the live provisional result (identical to the
    stored one after any save)."""
    result = evaluation_scoring(evaluation)
    final = evaluation.status in FINAL_STATUSES
    overall = evaluation.overall_score if final else result["overall_score"]
    rating = evaluation.rating if final else result["rating"]
    return {
        "overall_score": _float(overall),
        "rating": rating,
        "provisional": not final,
        "sections": [{**section, "score": _float(section["score"])} for section in result["sections"]],
        "sections_weighted": result["sections_weighted"],
        "strengths": result["strengths"],
        "improvements": result["improvements"],
        "critical_points": result["critical_points"],
        "required_remaining": result["required_remaining"],
    }


def serialize_detail(evaluation, user):
    order = evaluation.purchase_order
    project = evaluation.project
    return {
        "id": evaluation.id,
        "reference": evaluation.reference,
        "status": evaluation.status,
        "evaluation_type": evaluation.evaluation_type,
        "supplier": {"id": evaluation.supplier_id, "name": evaluation.supplier.company_name},
        "purchase_order": None if not order else {
            "id": order.id, "reference": order.reference, "po_date": order.po_date, "status": order.status,
        },
        "project": None if not project else {"id": project.id, "name": project.project_name},
        "owner": _name(evaluation.owner),
        "period_start": evaluation.period_start,
        "period_end": evaluation.period_end,
        "follows_fnc": evaluation.follows_fnc,
        "fnc_reference": evaluation.fnc_reference,
        "template_version": evaluation.template_version,
        "snapshot": evaluation.template_snapshot,
        "responses": {
            str(r.criterion_id): {
                "score": r.score, "not_applicable": r.not_applicable, "comment": r.comment,
            }
            for r in evaluation.responses.all()
        },
        "progress": evaluation_progress(evaluation),
        "scoring": scoring_payload(evaluation),
        "editable": evaluation.status in EDITABLE_STATUSES and is_owner(evaluation, user)
        and user_can_manage_evaluations(user),
        "reviewer": None if not evaluation.reviewer else {
            "id": evaluation.reviewer_id, "name": _name(evaluation.reviewer)},
        "is_owner": is_owner(evaluation, user),
        "allowed_actions": allowed_actions(evaluation, user),
        "submission": submission_check(evaluation),
        "workflow": {
            "submitted_at": evaluation.submitted_at,
            "submitted_by": _name(evaluation.submitted_by),
            "reviewed_at": evaluation.reviewed_at,
            "reviewed_by": _name(evaluation.reviewed_by),
            "rejected_at": evaluation.rejected_at,
            "rejected_by": _name(evaluation.rejected_by),
            "rejection_reason": evaluation.rejection_reason,
            "approved_at": evaluation.approved_at,
            "approved_by": _name(evaluation.approved_by),
            "closed_at": evaluation.closed_at,
            "closed_by": _name(evaluation.closed_by),
        },
        "created_at": evaluation.created_at,
        "updated_at": evaluation.updated_at,
    }


def _detail_queryset(user):
    return evaluations_for_user(user).select_related(
        "supplier", "purchase_order", "project", "owner", "reviewer", "submitted_by", "reviewed_by",
        "rejected_by", "approved_by", "closed_by",
    ).prefetch_related("responses")


class OrdersToEvaluateView(_QualityView):
    def get(self, request):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        params = request.query_params
        queryset = orders_to_evaluate(request.user, params.get("search"))
        page_size = _positive_int(params.get("page_size"), 25, MAX_PAGE_SIZE)
        page = _positive_int(params.get("page"), 1)
        start = (page - 1) * page_size
        return Response({
            "count": queryset.count(),
            "page": page,
            "page_size": page_size,
            "results": [
                {
                    "id": order.id,
                    "reference": order.reference,
                    "supplier": {"id": order.receiving_company_id,
                                 "name": order.receiving_company.company_name},
                    "po_date": order.po_date,
                    "status": order.status,
                    "project": None if not order.project else
                    {"id": order.project_id, "name": order.project.project_name},
                    "evaluations_count": order.evaluations_count,
                }
                for order in queryset[start:start + page_size]
            ],
        })


class SupplierEvaluationCreateView(_QualityView):
    def post(self, request):
        if not user_can_manage_evaluations(request.user):
            return self._forbidden()
        data = request.data
        evaluation = create_supplier_evaluation(
            user=request.user,
            purchase_order_id=data.get("purchase_order"),
            template_id=data.get("template"),
            evaluation_type=data.get("evaluation_type"),
            period_start=_date(data.get("period_start"), "period_start"),
            period_end=_date(data.get("period_end"), "period_end"),
            follows_fnc=bool(data.get("follows_fnc")),
            fnc_reference=data.get("fnc_reference") or "",
        )
        evaluation = _detail_queryset(request.user).get(pk=evaluation.pk)
        return Response(serialize_detail(evaluation, request.user), status=201)


class SupplierEvaluationDetailView(_QualityView):
    def _get(self, request, pk):
        evaluation = _detail_queryset(request.user).filter(pk=pk).first()
        if evaluation is None:
            raise Http404
        return evaluation

    def get(self, request, pk):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        return Response(serialize_detail(self._get(request, pk), request.user))


class SupplierEvaluationResponsesView(SupplierEvaluationDetailView):
    def put(self, request, pk):
        if not user_can_manage_evaluations(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        save_responses(evaluation=evaluation, user=request.user, answers=request.data.get("answers"))
        return Response(serialize_detail(self._get(request, pk), request.user))


class EvaluationsView(SupplierEvaluationCreateView, SupplierEvaluationListView):
    """GET lists the company's evaluations (Q1), POST creates one from an order (Q3)."""
