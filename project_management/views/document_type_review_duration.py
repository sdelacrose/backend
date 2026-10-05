from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from project_management.models import Project
from project_management.permissions import ECPMPConfigPermission
from project_management.services.document_type_review_duration import (
    DURATION_FIELD_NAMES,
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
