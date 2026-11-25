"""QUALITY Q5: workflow actions, reviewer choices and history of a Supplier Evaluation.

Same isolation as the rest of Quality: the evaluation is always looked up inside the authenticated user's
company (404 otherwise). All rules live in services.supplier_evaluation_workflow; this layer only maps HTTP.
"""

from django.http import Http404
from rest_framework.response import Response

from project_management.services.supplier_evaluation_workflow import (
    TRANSITIONS,
    assign_reviewer,
    eligible_reviewers,
    is_owner,
    transition,
)
from project_management.views.quality import (
    _QualityView,
    evaluations_for_user,
    user_can_manage_evaluations,
    user_can_view_quality,
)
from project_management.views.quality_evaluations import _detail_queryset, _name, serialize_detail


class _WorkflowView(_QualityView):
    def _get(self, request, pk):
        evaluation = evaluations_for_user(request.user).filter(pk=pk).first()
        if evaluation is None:
            raise Http404
        return evaluation

    def _detail(self, request, pk):
        return Response(serialize_detail(_detail_queryset(request.user).get(pk=pk), request.user))


class EvaluationTransitionView(_WorkflowView):
    """POST .../<action> with an optional {"comment": "..."} (mandatory for a rejection)."""

    def post(self, request, pk, action):
        if action not in TRANSITIONS:
            raise Http404
        if not user_can_manage_evaluations(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        transition(action=action, evaluation=evaluation, user=request.user,
                   comment=request.data.get("comment") or "")
        return self._detail(request, pk)


class EvaluationReviewerView(_WorkflowView):
    """GET lists who can be chosen; PUT {"reviewer": <user id>} chooses (owner only)."""

    def get(self, request, pk):
        if not user_can_manage_evaluations(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        if not is_owner(evaluation, request.user):
            return Response({"results": []})
        return Response({"results": [{"id": u.id, "name": _name(u)} for u in eligible_reviewers(evaluation)]})

    def put(self, request, pk):
        if not user_can_manage_evaluations(request.user):
            return self._forbidden()
        assign_reviewer(evaluation=self._get(request, pk), user=request.user,
                        reviewer_id=request.data.get("reviewer"))
        return self._detail(request, pk)


class EvaluationHistoryView(_WorkflowView):
    def get(self, request, pk):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        events = evaluation.events.select_related("actor")
        return Response({"results": [
            {"id": e.id, "event_type": e.event_type, "actor": _name(e.actor), "comment": e.comment,
             "metadata": e.metadata, "created_at": e.created_at}
            for e in events
        ]})
