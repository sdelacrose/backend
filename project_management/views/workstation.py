from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Prefetch, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.models import (
    CompanySolutionSpaceItem,
    Project,
    ProjectWorkstation,
    ProjectWorkstationNode,
    Requirement,
)
from project_management.permissions import ProjectSolutionSpacePermission, RequirementPermission
from project_management.services.workstation_compliance import compliance
from project_management.serializers.workstation import (
    ProjectWorkstationCreateSerializer,
    ProjectWorkstationSerializer,
    WorkstationRevisionCommitRequestSerializer,
    WorkstationRevisionPreviewRequestSerializer,
    WorkstationRevisionSerializer,
    WorkbenchEditSerializer,
)
from project_management.services.workstation_composition import (
    create_project_workstation,
    create_blank_workstation,
)
from project_management.services.workstation_revision import (
    WorkstationRevisionConflict,
    commit_workstation_revision,
    preview_workstation_revision,
)
from project_management.services.workstation_workbench import workbench_edit


class ProjectWorkstationViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ProjectWorkstationSerializer
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "source_process_assembly__assembly_name"]
    ordering_fields = ["id", "name", "status", "created_at", "updated_at"]
    ordering = ["source_requirement__sort_order", "source_requirement_id", "name", "id"]

    @action(detail=True, methods=["get", "post"])
    def compliance(self, request, pk=None):
        workstation = self.get_object()
        if not RequirementPermission()._check_view_permission_project(request.user, workstation, {"project": workstation.project}):
            self.permission_denied(request)
        payload, code = compliance(workstation, request.user, None if request.method == "GET" else request.data, occurrence=request.query_params.get("occurrence"))
        return Response(payload, status=code)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED:
            raise Http404

    def get_permissions(self):
        if self.action == "create":
            action_name = "create"
        elif self.action in {"preview_revision", "commit_revision", "workbench"} or (self.action == "compliance" and self.request.method == "POST"):
            action_name = "update"
        else:
            action_name = "view"
        return [ProjectSolutionSpacePermission(action=action_name, scope="project")]

    @staticmethod
    def _project_access_filter(user, *, include_company=False):
        query = Q(project__user=user) | Q(project__team_members__team_member=user)
        if include_company and not user.is_freelancer and user.company_id:
            query |= Q(project__company_profile_id=user.company_id)
        return query

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser) or not user.is_authenticated:
            return ProjectWorkstation.objects.none()

        project_id = self.request.query_params.get("project")
        if not project_id and self.action == "create":
            project_id = self.request.data.get("project")
        if not project_id and self.kwargs.get("pk"):
            project_id = (
                ProjectWorkstation.objects.filter(pk=self.kwargs["pk"])
                .values_list("project_id", flat=True)
                .first()
            )
        permission_params = {"project": project_id} if project_id else self.request.query_params
        has_company_access = bool(
            not user.is_freelancer
            and require_any_permission_multiple_modules(
                user,
                {"projects": ["full_access", "view_all_access"]},
                "company",
                permission_params,
            )
        )
        has_module_access = require_any_permission_multiple_modules(
            user,
            {"project_assembly_catalog": ["view", "full_access"]},
            "project",
            permission_params,
        )
        if not has_company_access and not has_module_access:
            return ProjectWorkstation.objects.none()

        active_nodes = ProjectWorkstationNode.objects.select_related(
            "revision",
            "source_company_item",
            "source_project_item",
            "supersedes",
        ).order_by("level", "sort_order", "id")
        queryset = (
            ProjectWorkstation.objects.filter(
                self._project_access_filter(user, include_company=has_company_access),
            )
            .select_related("project", "source_process_assembly", "created_by", "modified_by")
            .prefetch_related(Prefetch("nodes", queryset=active_nodes, to_attr="active_nodes"))
            .distinct()
        )
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        source_process_assembly_id = self.request.query_params.get(
            "source_process_assembly",
        )
        if source_process_assembly_id:
            queryset = queryset.filter(
                source_process_assembly_id=source_process_assembly_id,
            )
        status_value = self.request.query_params.get("status")
        if status_value in {
            ProjectWorkstation.STATUS_ACTIVE,
            ProjectWorkstation.STATUS_ARCHIVED,
        }:
            queryset = queryset.filter(status=status_value)
        return queryset

    def create(self, request, *args, **kwargs):
        request_serializer = ProjectWorkstationCreateSerializer(data=request.data)
        request_serializer.is_valid(raise_exception=True)
        project_id = request_serializer.validated_data["project"]

        project_query = Project.objects.filter(id=project_id)
        if request.user.is_freelancer:
            project_query = project_query.filter(
                Q(user=request.user) | Q(team_members__team_member=request.user),
            )
        elif request.user.company_id:
            project_query = project_query.filter(
                Q(user=request.user)
                | Q(team_members__team_member=request.user)
                | Q(company_profile_id=request.user.company_id),
            )
        else:
            project_query = project_query.none()
        project = get_object_or_404(project_query.distinct())
        source = get_object_or_404(
            CompanySolutionSpaceItem.objects.filter(
                id=request_serializer.validated_data["source_process_assembly"],
                company_profile_id=project.company_profile_id,
                assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            ),
        ) if request_serializer.validated_data.get("source_process_assembly") else None
        requirement = None
        if request_serializer.validated_data.get("source_requirement"):
            requirement = get_object_or_404(
                Requirement.objects.filter(project=project),
                pk=request_serializer.validated_data["source_requirement"],
            )
        try:
            creator = create_project_workstation if source else create_blank_workstation
            result = creator(
                project=project,
                source=source,
                actor=request.user,
                idempotency_key=request_serializer.validated_data["idempotency_key"],
                name=request_serializer.validated_data["name"],
                source_requirement=requirement,
            )
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)

        workstation = self.get_queryset().get(id=result.workstation.id)
        response_serializer = self.get_serializer(workstation)
        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"])
    def revisions(self, request, pk=None):
        workstation = self.get_object()
        queryset = workstation.revisions.select_related("snapshot", "created_by").prefetch_related(
            "nodes",
        )
        page = self.paginate_queryset(queryset)
        serializer = WorkstationRevisionSerializer(
            page if page is not None else queryset,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=True, methods=["post"])
    def workbench(self, request, pk=None):
        source = self.get_object()
        serializer = WorkbenchEditSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        for target in payload["targets"]:
            station = get_object_or_404(self.get_queryset(), pk=target["id"], project_id=source.project_id)
            self.check_object_permissions(request, station)
        try:
            results = workbench_edit(source=source, payload=payload, actor=request.user)
        except WorkstationRevisionConflict as exc:
            return Response({"detail": str(exc), "latest_revision": exc.latest_revision}, status=409)
        except DjangoValidationError as exc:
            raise ValidationError(exc.message_dict if hasattr(exc, "message_dict") else exc.messages)
        return Response({"committed": payload["commit"], "workstations": results})

    @staticmethod
    def _preview_payload(preview):
        return {
            "expected_revision": preview.expected_revision,
            "resulting_revision": preview.resulting_revision,
            "source_hash": preview.source_hash,
            "content_hash": preview.content_hash,
            "summary": preview.summary,
            "nodes": preview.nodes,
            "before_nodes": preview.before_nodes,
        }

    @action(detail=True, methods=["post"], url_path="preview-revision")
    def preview_revision(self, request, pk=None):
        workstation = self.get_object()
        serializer = WorkstationRevisionPreviewRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            preview = preview_workstation_revision(
                workstation=workstation,
                operations=serializer.validated_data["operations"],
                expected_revision=serializer.validated_data["expected_revision"],
            )
        except WorkstationRevisionConflict as exc:
            return Response(
                {
                    "detail": str(exc),
                    "latest_revision": exc.latest_revision,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
            )
        return Response(self._preview_payload(preview))

    @action(detail=True, methods=["post"], url_path="commit-revision")
    def commit_revision(self, request, pk=None):
        workstation = self.get_object()
        serializer = WorkstationRevisionCommitRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = commit_workstation_revision(
                workstation=workstation,
                operations=serializer.validated_data["operations"],
                expected_revision=serializer.validated_data["expected_revision"],
                actor=request.user,
                reason=serializer.validated_data.get("reason", ""),
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except WorkstationRevisionConflict as exc:
            return Response(
                {
                    "detail": str(exc),
                    "latest_revision": exc.latest_revision,
                },
                status=status.HTTP_409_CONFLICT,
            )
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
            )

        workstation = self.get_queryset().get(pk=workstation.pk)
        return Response(
            {
                "revision": WorkstationRevisionSerializer(result.revision).data,
                "preview": self._preview_payload(result.preview),
                "workstation": self.get_serializer(workstation).data,
            },
            status=(
                status.HTTP_201_CREATED
                if result.created
                else status.HTTP_200_OK
            ),
        )
