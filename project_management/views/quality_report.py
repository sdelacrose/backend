"""QUALITY Q6: generate / look up / download the PDF report of a Supplier Evaluation.

Isolation: the evaluation is always looked up inside the user's company (404 otherwise). Rights: Quality
(`quality.view` to read, `quality.evaluation_manage` to generate) AND Documents (`documents.view` to read the file,
`documents.create` to file a new report). The file is streamed through this authenticated endpoint, never exposed
by a public URL.
"""

import logging

from django.http import FileResponse, Http404
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from ecpmpapp.permissions import BaseModulePermission
from project_management.services.supplier_evaluation_report import (
    assert_reportable,
    generate_report,
    report_filename,
)
from project_management.views.quality import (
    _QualityView,
    evaluations_for_user,
    user_can_manage_evaluations,
    user_can_view_quality,
)

logger = logging.getLogger(__name__)


def _has_documents_right(user, actions):
    checker = BaseModulePermission("documents", "view", "company")
    return any(checker._check_permission(user, "documents", action, "company", {}) for action in actions)


def can_read_documents(user):
    return _has_documents_right(user, ("view", "full_access"))


def can_create_documents(user):
    return _has_documents_right(user, ("create", "full_access"))


def report_payload(evaluation, user):
    """Summary used by the detail page: what exists and what this user may do with it."""
    from project_management.views.quality_evaluations import _name  # lazy: that module imports this one

    link = getattr(evaluation, "report", None)
    revision = None
    if link is not None:
        revision = link.document.revisions.order_by("-created_at", "-id").first()
    reportable = evaluation.status in ("APPROVED", "CLOSED")
    can_generate = reportable and user_can_manage_evaluations(user) and can_create_documents(user)
    can_read = link is not None and user_can_view_quality(user) and can_read_documents(user)
    return {
        "exists": link is not None,
        "document_id": link.document_id if link else None,
        "file_name": link.document.file_name if link else None,
        "revision_count": link.document.revisions.count() if link else 0,
        "last_generated_at": revision.created_at if revision else None,
        "last_generated_by": _name(revision.created_by) if revision else None,
        "can_generate": can_generate,
        "can_view": can_read,
    }


class _ReportView(_QualityView):
    def _get(self, request, pk):
        evaluation = evaluations_for_user(request.user).select_related("report__document").filter(pk=pk).first()
        if evaluation is None:
            raise Http404
        return evaluation


class EvaluationReportView(_ReportView):
    def get(self, request, pk):
        if not user_can_view_quality(request.user):
            return self._forbidden()
        return Response(report_payload(self._get(request, pk), request.user))

    def post(self, request, pk):
        """Generate the report (first time) or add a revision (next times). Never touches the workflow."""
        if not user_can_manage_evaluations(request.user) or not can_create_documents(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        assert_reportable(evaluation)
        try:
            generate_report(
                evaluation=evaluation, user=request.user, language=request.data.get("language") or "en",
                timezone_name=request.data.get("timezone"),
            )
        except APIException:
            raise
        except Exception:
            # PDF or storage failure: everything was rolled back, the evaluation is untouched, a retry is safe.
            logger.exception("Quality report generation failed for evaluation %s", pk)
            return Response({"detail": "The report could not be generated. Nothing was saved, please try again."}, status=503)
        evaluation = self._get(request, pk)
        return Response(report_payload(evaluation, request.user), status=201)


class EvaluationReportDownloadView(_ReportView):
    def get(self, request, pk):
        if not user_can_view_quality(request.user) or not can_read_documents(request.user):
            return self._forbidden()
        evaluation = self._get(request, pk)
        link = getattr(evaluation, "report", None)
        revision = link and link.document.revisions.order_by("-created_at", "-id").first()
        if revision is None or not revision.file:
            raise Http404
        inline = request.query_params.get("inline") == "1"
        response = FileResponse(
            revision.file.open("rb"), content_type="application/pdf",
            as_attachment=not inline, filename=report_filename(evaluation),
        )
        response["Cache-Control"] = "private, no-store"
        return response
