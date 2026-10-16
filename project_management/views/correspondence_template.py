"""MAIL-1.5-B: the company's library of e-mail templates for outgoing correspondences.

    GET   correspondence-templates               active templates the user may apply (?language=FR|EN|DE,
                                                 ?project=<id> to check the right at project scope);
                                                 ?include_inactive=true is for managers only
    POST  correspondence-templates               create (manager)
    GET   correspondence-templates/<id>          detail (manager; others: an active template only)
    PATCH correspondence-templates/<id>          edit, deactivate, reactivate (manager)
    POST  correspondence-templates/<id>/apply    render a template for a context: READ ONLY

Two distinct rights, both read from the role of the requester in their own company:
  * documents.correspondence_template_manage (company scope; documents.full_access overrides, the
    established Documents convention) -- create / edit / deactivate / see inactive ones;
  * documents.correspondence_create (project scope when a project is given, else company scope) --
    list and apply the ACTIVE templates. Creating correspondences never gives the right to change
    the library.

There is no DELETE: a template is deactivated. Every query is filtered by the requester's company, so
a template id of another company is a plain 404. Applying a template creates nothing, changes no
draft, no workflow, no send and no proof: the frontend copies the rendered text into the form.
"""
from django.http import Http404
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from project_management.models import CorrespondenceTemplate, Project
from project_management.permissions import CorrespondencePermission
from project_management.serializers.correspondence_template import (
    ApplyCorrespondenceTemplateSerializer,
    CorrespondenceTemplateSerializer,
)
from project_management.services.correspondence_template import (
    TemplateContextError,
    apply_template,
    resolve_context,
)
from project_management.views.correspondence import correspondence_queryset_for_user

MANAGE_ACTION = "correspondence_template_manage"


def _company_id(user):
    return getattr(user, "company_id", None)


def can_manage_templates(user):
    if not _company_id(user):
        return False
    return CorrespondencePermission()._check_action_or_full_access(user, MANAGE_ACTION, "company", {})


def can_use_templates(user, project=None):
    """The right to create an OUT correspondence where the template would be applied."""
    if not _company_id(user):
        return False
    scope, context = ("project", {"project": project}) if project is not None else ("company", {})
    return CorrespondencePermission()._check_action_or_full_access(
        user, "correspondence_create", scope, context,
    )


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes")


class CorrespondenceTemplateViewSet(viewsets.GenericViewSet):
    filter_backends = []
    serializer_class = CorrespondenceTemplateSerializer

    def _templates(self, request):
        return CorrespondenceTemplate.objects.filter(
            company_profile_id=_company_id(request.user)
        ).select_related("created_by")

    def _require_company_user(self, request):
        if not _company_id(request.user):
            raise PermissionDenied("Templates are available to company users only.")

    def _project_for_scope(self, request, raw):
        """The project of the requester's own company, or None. An unusable id is a clean 400."""
        if raw in (None, ""):
            return None
        try:
            project_id = int(raw)
        except (TypeError, ValueError):
            raise ValidationError({"project": "Not found."})
        project = Project.objects.filter(pk=project_id, company_profile_id=_company_id(request.user)).first()
        if project is None:
            raise ValidationError({"project": "Not found."})
        return project

    def list(self, request):
        self._require_company_user(request)
        manager = can_manage_templates(request.user)
        queryset = self._templates(request)
        if _truthy(request.query_params.get("include_inactive")):
            if not manager:
                raise PermissionDenied()
        else:
            project = self._project_for_scope(request, request.query_params.get("project"))
            if not manager and not can_use_templates(request.user, project):
                raise PermissionDenied()
            queryset = queryset.filter(is_active=True)
        language = (request.query_params.get("language") or "").strip().upper()
        if language:
            if language not in CorrespondenceTemplate.Language.values:
                raise ValidationError({"language": "Choose FR, EN or DE."})
            queryset = queryset.filter(language=language)
        return Response(CorrespondenceTemplateSerializer(queryset, many=True).data)

    def create(self, request):
        self._require_company_user(request)
        if not can_manage_templates(request.user):
            raise PermissionDenied()
        serializer = CorrespondenceTemplateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        return Response(CorrespondenceTemplateSerializer(template).data, status=status.HTTP_201_CREATED)

    def _get(self, request, pk):
        template = self._templates(request).filter(pk=pk).first()
        if template is None:
            raise Http404
        return template

    def retrieve(self, request, pk=None):
        self._require_company_user(request)
        template = self._get(request, pk)
        if not can_manage_templates(request.user):
            # An inactive template does not exist for someone who cannot manage the library.
            if not template.is_active:
                raise Http404
            if not can_use_templates(request.user):
                raise PermissionDenied()
        return Response(CorrespondenceTemplateSerializer(template).data)

    def partial_update(self, request, pk=None):
        self._require_company_user(request)
        if not can_manage_templates(request.user):
            raise PermissionDenied()
        template = self._get(request, pk)
        serializer = CorrespondenceTemplateSerializer(
            template, data=request.data, partial=True, context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        template = serializer.save()
        return Response(CorrespondenceTemplateSerializer(template).data)

    def apply(self, request, pk=None):
        """READ ONLY: nothing is created or changed (no correspondence, draft, workflow, send, proof)."""
        self._require_company_user(request)
        template = self._get(request, pk)
        context_serializer = ApplyCorrespondenceTemplateSerializer(data=request.data)
        context_serializer.is_valid(raise_exception=True)
        data = context_serializer.validated_data
        project = self._project_for_scope(request, data.get("project"))
        if not can_use_templates(request.user, project):
            raise PermissionDenied()
        if not template.is_active:
            raise ValidationError({"template": "This template is not active."})
        try:
            values, ids = resolve_context(
                request.user,
                project_id=project.id if project else None,
                recipient_contact_id=data.get("recipient_contact"),
                recipient_company_id=data.get("recipient_company"),
                parent_correspondence_id=data.get("parent_correspondence"),
                visible_correspondences=correspondence_queryset_for_user(request.user),
            )
        except TemplateContextError as error:
            raise ValidationError(error.errors)
        result = apply_template(template, values)
        result.update({"template": template.id, "language": template.language, "context": ids})
        return Response(result)
