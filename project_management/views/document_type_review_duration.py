from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from project_management.models import Project
from project_management.permissions import ECPMPConfigPermission
from project_management.services.document_type_review_duration import (
    DURATION_FIELD_NAMES,
    MAX_BULK_REVIEW_DURATION_ROWS,
    bulk_upsert_review_durations,
    list_effective_review_durations,
    resolve_review_durations,
    upsert_review_duration,
)

# L3H-1: the narrowest existing permission for a comparable Document Control configuration
# action -- the exact same `ecpmp_configurations` permission (and the exact same direct
# _check_view_permission/_check_update_permission calling convention) already used by
# views.supplier_surveillance_template (L3C) -- no new permission (L3H-1 section 14).


def _resolve_project(request):
    project_id = request.query_params.get("project") or request.data.get("project")
    if not project_id:
        raise ValidationError({"project": "A project is required."})
    project = Project.objects.filter(pk=project_id).first()
    if project is None:
        raise Http404
    return project


def _assert_permission(request, project, *, write):
    permission = ECPMPConfigPermission(
        action="update" if write else "view", scope="project",
    )
    context = {"project": project}
    allowed = (
        permission._check_update_permission(request.user, None, context)
        if write
        else permission._check_view_permission(request.user, None, context)
    )
    if not allowed:
        raise PermissionDenied(
            "You do not have permission to manage Document Review Durations."
        )


def _error_payload(error):
    if hasattr(error, "message_dict"):
        return error.message_dict
    return {"detail": error.messages}


class DocumentReviewDurationViewSet(viewsets.GenericViewSet):
    """L3H-1: Review Durations configuration, Project + Document Type scoped (checkpoint
    section 6/7). CONFIGURATION ONLY -- no due date, no snapshot, no overdue computation (those
    remain L3H-2/L3H-3/future lots)."""

    filter_backends = []

    def list(self, request, *args, **kwargs):
        project = _resolve_project(request)
        _assert_permission(request, project, write=False)
        return Response({"review_durations": list_effective_review_durations(project)})

    def upsert(self, request, *args, **kwargs):
        project = _resolve_project(request)
        _assert_permission(request, project, write=True)
        document_type = request.data.get("document_type")
        if not document_type:
            return Response(
                {"document_type": "A Document Type is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        values = {
            field_name: request.data.get(field_name)
            for field_name in DURATION_FIELD_NAMES
            if field_name in request.data
        }
        try:
            row = upsert_review_duration(
                project=project, document_type=document_type, values=values,
            )
        except DjangoValidationError as error:
            return Response(_error_payload(error), status=status.HTTP_400_BAD_REQUEST)
        return Response(resolve_review_durations(project=project, document_type=row.document_type))

    def bulk_upsert(self, request, *args, **kwargs):
        """FINISH-B section 7: one Save action for every dirty row -- narrow, transactional,
        the exact same per-field validation as `upsert` (section 22), never a second
        configuration model/semantics."""
        project = _resolve_project(request)
        _assert_permission(request, project, write=True)
        rows = request.data.get("rows")
        if not isinstance(rows, list) or not rows:
            return Response(
                {"rows": "A non-empty list of rows is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(rows) > MAX_BULK_REVIEW_DURATION_ROWS:
            return Response(
                {"rows": f"At most {MAX_BULK_REVIEW_DURATION_ROWS} rows per request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        for row in rows:
            if not isinstance(row, dict) or not row.get("document_type"):
                return Response(
                    {"rows": "Each row requires a document_type."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        result = bulk_upsert_review_durations(project=project, rows=rows)
        return Response(result)
