"""DEMO-01: intervention requests of a project and their reviewed CSV import.

    GET  intervention-requests?project=<id>[&search=&ordering=&page=&page_size=]
    GET  intervention-requests/<id>
    POST intervention-requests/import/preview     multipart: project, file, [source]
    GET  intervention-requests/import/<uuid>      batch with its classified rows
    POST intervention-requests/import/<uuid>/confirm

Rights reuse the Action Item module at project scope (the import creates Action Items):
  * read    action_item view / view_all / full_access on the project;
  * import  action_item create / full_access on the project.
Every query goes through `accessible_projects`, so a project (or request) of another company or a
project the user cannot reach is a plain 404. There is no DELETE and no project change in this
first scope: a request that owns actions is PROTECTed anyway.
"""
from django.db.models import Q
from django.http import Http404
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import (
    ActionItem,
    InterventionImportBatch,
    InterventionImportRow,
    InterventionRequest,
)
from project_management.pagination import CustomPageNumberPagination
from project_management.permissions import ActionItemPermission, check_company_project_permission
from project_management.project_access import accessible_projects
from project_management.services.intervention_import import (
    ACTION_TEMPLATES,
    BatchNotConfirmable,
    InvalidAssignee,
    ImportFileError,
    confirm_import,
    preview_import,
)


def _project_or_404(user, project_id):
    try:
        return accessible_projects(user).get(pk=int(project_id))
    except (TypeError, ValueError, accessible_projects(user).model.DoesNotExist):
        raise Http404


def can_view_interventions(user, project):
    perm = ActionItemPermission("view", "project")
    ctx = {"project": project}
    return bool(
        check_company_project_permission(perm, user, "full_access", ctx)
        or check_company_project_permission(perm, user, "view_all_access", ctx)
        or perm._check_permission(user, "action_item", "full_access", "project", ctx)
        or perm._check_permission(user, "action_item", "view", "project", ctx)
    )


def can_import_interventions(user, project):
    return bool(ActionItemPermission("create", "project")._check_create_permission(user, {"project": project}))


class InterventionActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ActionItem
        fields = ("id", "name", "status", "start_date", "target_date", "intervention_action_key")


class InterventionRequestSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source="project.project_name", read_only=True)
    actions_count = serializers.SerializerMethodField()
    actions_closed_count = serializers.SerializerMethodField()
    progress_status = serializers.SerializerMethodField()

    @staticmethod
    def _statuses(obj):
        # .all() uses the prefetch done by the views: no query per row.
        return [(a.status or "").strip().lower() for a in obj.action_items.all()]

    def get_actions_count(self, obj):
        return len(self._statuses(obj))

    def get_actions_closed_count(self, obj):
        return sum(1 for s in self._statuses(obj) if s == "closed")

    def get_progress_status(self, obj):
        # Derived from the generated actions, never stored: not_started (all still open),
        # completed (all closed), in_progress otherwise.
        statuses = self._statuses(obj)
        if not statuses or all(s == "open" for s in statuses):
            return "not_started"
        if all(s == "closed" for s in statuses):
            return "completed"
        return "in_progress"

    class Meta:
        model = InterventionRequest
        fields = (
            "id", "project", "project_name", "company_profile", "reference", "title", "received_date",
            "expected_end_date", "budget", "currency", "description", "source", "external_reference",
            "source_modified_at", "created_at", "actions_count", "actions_closed_count", "progress_status",
        )
        read_only_fields = fields


class InterventionRequestDetailSerializer(InterventionRequestSerializer):
    actions = InterventionActionSerializer(source="action_items", many=True, read_only=True)

    class Meta(InterventionRequestSerializer.Meta):
        fields = InterventionRequestSerializer.Meta.fields + ("actions",)
        read_only_fields = fields


class InterventionImportRowSerializer(serializers.ModelSerializer):
    existing_reference = serializers.CharField(source="existing_request.reference", read_only=True, default=None)

    class Meta:
        model = InterventionImportRow
        fields = ("line_number", "external_reference", "data", "outcome", "errors", "existing_reference")


def _batch_payload(batch, include_rows=True):
    payload = {
        "id": str(batch.id),
        "project": batch.project_id,
        "status": batch.status,
        "source": batch.source,
        "file_name": batch.file_name,
        "summary": batch.summary,
        "result": batch.result,
        "action_templates": [name for _, name in ACTION_TEMPLATES],
        "created_at": batch.created_at,
        "confirmed_at": batch.confirmed_at,
    }
    if include_rows:
        payload["rows"] = InterventionImportRowSerializer(batch.rows.select_related("existing_request"), many=True).data
    return payload


class InterventionRequestListView(APIView):
    permission_classes = [IsAuthenticated]
    ORDERING = {"reference", "title", "received_date", "expected_end_date", "created_at", "id"}

    def get(self, request):
        project = _project_or_404(request.user, request.query_params.get("project"))
        if not can_view_interventions(request.user, project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        queryset = InterventionRequest.objects.filter(project=project).select_related("project").prefetch_related("action_items")
        search = (request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(reference__icontains=search) | Q(title__icontains=search) | Q(external_reference__icontains=search)
            )
        ordering = request.query_params.get("ordering") or "-id"
        if ordering.lstrip("-") not in self.ORDERING:
            ordering = "-id"
        queryset = queryset.order_by(ordering, "-id")
        paginator = CustomPageNumberPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        data = InterventionRequestSerializer(page, many=True).data
        response = paginator.get_paginated_response(data)
        response.data["can_import"] = can_import_interventions(request.user, project)
        return response


class InterventionRequestDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        queryset = InterventionRequest.objects.filter(project__in=accessible_projects(request.user))
        try:
            intervention = queryset.select_related("project").prefetch_related("action_items").get(pk=pk)
        except InterventionRequest.DoesNotExist:
            raise Http404
        if not can_view_interventions(request.user, intervention.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        return Response(InterventionRequestDetailSerializer(intervention).data)


class InterventionImportPreviewView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        project = _project_or_404(request.user, request.data.get("project"))
        if not can_import_interventions(request.user, project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"code": "missing_file", "detail": "A CSV file is required."}, status=400)
        raw = upload.read(2 * 1024 * 1024 + 1)
        try:
            batch = preview_import(
                project=project, user=request.user, file_name=upload.name, raw=raw,
                source=request.data.get("source") or "csv",
            )
        except ImportFileError as exc:
            return Response({"code": exc.code, "detail": exc.detail}, status=status.HTTP_400_BAD_REQUEST)
        return Response(_batch_payload(batch), status=status.HTTP_201_CREATED)


def _batch_for_user(user, batch_id):
    try:
        return InterventionImportBatch.objects.select_related("project").get(
            pk=batch_id, project__in=accessible_projects(user)
        )
    except (InterventionImportBatch.DoesNotExist, ValueError, TypeError):
        raise Http404


class InterventionImportBatchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, batch_id):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        return Response(_batch_payload(batch))


class InterventionImportConfirmView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, batch_id):
        batch = _batch_for_user(request.user, batch_id)
        if not can_import_interventions(request.user, batch.project):
            return Response({"detail": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
        try:
            batch, applied_now = confirm_import(
                batch_id=batch.id, user=request.user, assignee_id=request.data.get("assignee")
            )
        except InvalidAssignee as exc:
            return Response({"code": "invalid_assignee", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except BatchNotConfirmable as exc:
            return Response({"code": "batch_not_confirmable", "detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        payload = _batch_payload(batch)
        payload["applied_now"] = applied_now
        return Response(payload, status=status.HTTP_200_OK)
