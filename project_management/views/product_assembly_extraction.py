from django.db.models import Exists, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import BasePermission, SAFE_METHODS
from rest_framework.response import Response

from ecpmpapp.constants import UserType
from project_management.models import (
    ProductAssemblyExtractionRun,
    ProductAssemblyExtractionSource,
    Project,
)
from project_management.permissions import SolutionDemandPermission
from project_management.serializers.product_assembly_extraction import (
    ProductAssemblyApplySerializer,
    ProductAssemblyExtractionCreateSerializer,
    ProductAssemblyExtractionRunSerializer,
    ProductAssemblyProposalSerializer,
)
from project_management.services.product_assembly_extraction import (
    ProductAssemblyExtractionError,
    apply_extraction_run,
    create_extraction_run,
    dispatch_extraction_run,
    retry_extraction_run,
)
from project_management.services.document_access import (
    get_accessible_document_queryset,
)


class ProductAssemblyProjectManagerPermission(BasePermission):
    message = "Only the project manager can run or apply product assembly extraction."

    @staticmethod
    def _is_project_manager(user, project):
        if not user or not user.is_authenticated or not project:
            return False
        if project.user_id == user.id:
            return True
        return project.team_members.filter(
            Q(team_member=user, project_role__name__iexact="Project Owner")
            | Q(team_member=user, role__iexact=UserType.PROJECT_MANAGER.value)
        ).exists()

    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if getattr(view, "action", None) != "create":
            return True
        project = Project.objects.filter(id=request.data.get("project")).first()
        return self._is_project_manager(request.user, project)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        return self._is_project_manager(request.user, obj.project)


class ProductAssemblyExtractionRunViewSet(viewsets.ModelViewSet):
    serializer_class = ProductAssemblyExtractionRunSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.action == "create":
            return [
                SolutionDemandPermission(action="create", scope="project"),
                ProductAssemblyProjectManagerPermission(),
            ]
        if self.action in ["partial_update", "apply", "retry", "cancel"]:
            return [
                SolutionDemandPermission(action="create", scope="project"),
                ProductAssemblyProjectManagerPermission(),
            ]
        return [SolutionDemandPermission(action="view", scope="project")]

    def get_queryset(self):
        user = self.request.user
        project_id = self.request.query_params.get("project")
        queryset = ProductAssemblyExtractionRun.objects.select_related(
            "project"
        ).prefetch_related(
            "sources__document",
            "applied_rows__solution_demand",
        )
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        if not user.is_authenticated:
            return queryset.none()
        if user.is_freelancer:
            access_filter = Q(project__user=user) | Q(
                project__team_members__team_member=user
            )
        else:
            access_filter = (
                Q(project__company_profile=user.company)
                | Q(project__user__company=user.company)
                | Q(project__team_members__team_member=user)
            )
        visible_document_ids = get_accessible_document_queryset(
            self.request,
            project_id=project_id,
        ).values("id")
        inaccessible_sources = ProductAssemblyExtractionSource.objects.filter(
            run_id=OuterRef("pk"),
            document__isnull=False,
        ).exclude(document_id__in=Subquery(visible_document_ids))
        return (
            queryset.filter(access_filter)
            .annotate(has_inaccessible_source=Exists(inaccessible_sources))
            .filter(has_inaccessible_source=False)
            .distinct()
        )

    def create(self, request, *args, **kwargs):
        input_serializer = ProductAssemblyExtractionCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        project = get_object_or_404(Project, id=data["project"])
        try:
            run = create_extraction_run(
                project=project,
                user=request.user,
                document_ids=data.get("document_ids"),
                project_language=data["project_language"],
                request=request,
            )
            dispatch_extraction_run(run.id)
            run.refresh_from_db()
        except ProductAssemblyExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.code
                    in {
                        "source_documents_not_ready",
                        "extraction_already_applied",
                        "extraction_already_in_progress",
                    }
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        return Response(
            self.get_serializer(run).data,
            status=(
                status.HTTP_201_CREATED
                if run.status != ProductAssemblyExtractionRun.Status.QUEUED
                else status.HTTP_202_ACCEPTED
            ),
        )

    def partial_update(self, request, *args, **kwargs):
        run = self.get_object()
        if run.status != ProductAssemblyExtractionRun.Status.REVIEW_READY:
            return Response(
                {"detail": "Only a review-ready extraction can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ProductAssemblyProposalSerializer(
            data=request.data.get("rows"),
            many=True,
        )
        serializer.is_valid(raise_exception=True)
        run.result = {**run.result, "rows": serializer.validated_data}
        run.modified_by = request.user
        run.save(update_fields=["result", "modified_by", "updated_at"])
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        run = self.get_object()
        input_serializer = ProductAssemblyApplySerializer(data=request.data or {})
        input_serializer.is_valid(raise_exception=True)
        try:
            run = apply_extraction_run(
                run,
                request.user,
                rows=input_serializer.validated_data.get("rows"),
                request=request,
            )
        except ProductAssemblyExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        try:
            replacement = retry_extraction_run(
                run=run,
                user=request.user,
                request=request,
            )
            dispatch_extraction_run(replacement.id)
            replacement.refresh_from_db()
        except ProductAssemblyExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            self.get_serializer(replacement).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if run.status not in {
            ProductAssemblyExtractionRun.Status.QUEUED,
            ProductAssemblyExtractionRun.Status.EXTRACTING,
        }:
            return Response(
                {"detail": "Only queued or active extraction runs can be cancelled."},
                status=status.HTTP_409_CONFLICT,
            )
        run.status = ProductAssemblyExtractionRun.Status.CANCELLED
        run.modified_by = request.user
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "modified_by",
                "completed_at",
                "updated_at",
            ]
        )
        return Response(self.get_serializer(run).data)
