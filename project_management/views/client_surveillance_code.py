"""GED-WF.4.1: configuration of the client surveillance codes of a project.

Reading follows the project documents access (or the project configuration view right); writing needs
the project configuration right (ECPMPConfigPermission, as the Document Review Durations). There is no
delete endpoint: the four categories are fixed, "reset" only brings a category back to its defaults.
"""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from project_management.models import Project
from project_management.permissions import DocumentPermission, ECPMPConfigPermission
from project_management.services.client_surveillance_code import (
    LABEL_FIELDS, list_effective_codes, reset_category, save_codes,
)


def _resolve_project(request):
    project_id = request.query_params.get("project") or request.data.get("project")
    if not project_id:
        raise ValidationError({"project": "A project is required."})
    try:
        project = Project.objects.filter(pk=int(project_id)).first()
    except (TypeError, ValueError):
        raise ValidationError({"project": "A valid project is required."})
    if project is None:
        raise Http404
    return project


def _can_write(user, project):
    return ECPMPConfigPermission(action="update", scope="project")._check_update_permission(
        user, None, {"project": project},
    )


def _can_read(user, project):
    context = {"project": project}
    config = ECPMPConfigPermission(action="view", scope="project")
    documents = DocumentPermission(action="view", scope="project")
    return bool(
        config._check_view_permission(user, None, context)
        or documents._check_permission(user, "documents", "view", "project", context)
        or documents._check_permission(user, "documents", "full_access", "project", context)
    )


def _error(error):
    return Response(
        error.message_dict if hasattr(error, "message_dict") else {"detail": error.messages},
        status=status.HTTP_400_BAD_REQUEST,
    )


class ClientSurveillanceCodeViewSet(viewsets.GenericViewSet):
    filter_backends = []

    def list(self, request, *args, **kwargs):
        project = _resolve_project(request)
        if not _can_read(request.user, project):
            raise PermissionDenied("You do not have access to this project's documents.")
        return Response({"codes": list_effective_codes(project)})

    def save(self, request, *args, **kwargs):
        """PUT: {project, codes: [{category, code, label_en, label_fr, label_de}, ...]}"""
        project = _resolve_project(request)
        if not _can_write(request.user, project):
            raise PermissionDenied("You do not have permission to configure the client surveillance codes.")
        changes = request.data.get("codes")
        if not isinstance(changes, list) or not changes:
            raise ValidationError({"codes": "A list of codes is required."})
        try:
            return Response({"codes": save_codes(project, [self._pick(c) for c in changes], request.user)})
        except DjangoValidationError as error:
            return _error(error)

    def update_one(self, request, category=None, *args, **kwargs):
        """PATCH one category: {project, code, label_en, label_fr, label_de}"""
        project = _resolve_project(request)
        if not _can_write(request.user, project):
            raise PermissionDenied("You do not have permission to configure the client surveillance codes.")
        change = {**self._pick(request.data), "category": category}
        try:
            return Response({"codes": save_codes(project, [change], request.user)})
        except DjangoValidationError as error:
            return _error(error)

    def reset(self, request, category=None, *args, **kwargs):
        project = _resolve_project(request)
        if not _can_write(request.user, project):
            raise PermissionDenied("You do not have permission to configure the client surveillance codes.")
        try:
            return Response({"codes": reset_category(project, category, request.user)})
        except DjangoValidationError as error:
            return _error(error)

    @staticmethod
    def _pick(data):
        # Only the visible code and the labels are ever read: the category of a row, the rules and the
        # consequences cannot be sent, whatever the payload contains.
        if not hasattr(data, "get"):
            raise ValidationError({"codes": "Invalid entry."})
        return {"category": data.get("category"), "code": data.get("code"), **{f: data.get(f) for f in LABEL_FIELDS}}
