from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from project_management.models import (
    ProcessAssemblyExtractionRun,
    ProcessAttributeExtractionRun,
    Project,
)
from project_management.permissions import RequirementPermission
from project_management.serializers.process_requirements_extraction import (
    ProcessAssemblyApplySerializer,
    ProcessAssemblyExtractionCreateSerializer,
    ProcessAssemblyExtractionRunSerializer,
    ProcessAssemblyProposalSerializer,
    ProcessAttributeApplySerializer,
    ProcessAttributeExtractionCreateSerializer,
    ProcessAttributeExtractionRunSerializer,
    ProcessAttributeProposalSerializer,
)
from project_management.services.process_requirements_extraction import (
    ProcessRequirementsExtractionError,
    apply_process_assembly_run,
    apply_process_attribute_run,
    confirm_station_coverage,
    create_process_assembly_run,
    create_process_attribute_run,
    dispatch_process_assembly_run,
    dispatch_process_attribute_run,
    retry_process_assembly_run,
)
from project_management.views.product_assembly_extraction import (
    ProductAssemblyProjectManagerPermission,
)


def _project_access_filter(user):
    if user.is_freelancer:
        return Q(project__user=user) | Q(project__team_members__team_member=user)
    return (
        Q(project__company_profile=user.company)
        | Q(project__user__company=user.company)
        | Q(project__team_members__team_member=user)
    )


def _error_response(exc, conflict_codes=None):
    return Response(
        {"error": {"code": exc.code, "message": exc.message}},
        status=(
            status.HTTP_409_CONFLICT
            if exc.code in set(conflict_codes or ())
            else status.HTTP_400_BAD_REQUEST
        ),
    )


class ProcessExtractionViewSetMixin:
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.action in ["create", "partial_update", "apply", "retry", "cancel", "confirm_coverage"]:
            return [
                RequirementPermission(action="create", scope="project"),
                ProductAssemblyProjectManagerPermission(),
            ]
        return [RequirementPermission(action="view", scope="project")]

    def get_queryset(self):
        queryset = self.queryset.select_related("project")
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if not self.request.user.is_authenticated:
            return queryset.none()
        return queryset.filter(_project_access_filter(self.request.user)).distinct()

    def cancel(self, request, pk=None):
        run = self.get_object()
        if run.status not in {run.Status.QUEUED, run.Status.EXTRACTING}:
            return Response(
                {"detail": "Only queued or active extraction runs can be cancelled."},
                status=status.HTTP_409_CONFLICT,
            )
        run.status = run.Status.CANCELLED
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


class ProcessAssemblyExtractionRunViewSet(
    ProcessExtractionViewSetMixin,
    viewsets.ModelViewSet,
):
    queryset = ProcessAssemblyExtractionRun.objects.all()
    serializer_class = ProcessAssemblyExtractionRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = ProcessAssemblyExtractionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = get_object_or_404(Project, id=serializer.validated_data["project"])
        try:
            run = create_process_assembly_run(
                project,
                request.user,
                serializer.validated_data["project_language"],
                request=request,
            )
            dispatch_process_assembly_run(run.id)
            run.refresh_from_db()
        except ProcessRequirementsExtractionError as exc:
            return _error_response(
                exc,
                {
                    "extraction_already_in_progress",
                    "extraction_already_applied",
                },
            )
        return Response(
            self.get_serializer(run).data,
            status=(
                status.HTTP_202_ACCEPTED
                if run.status == run.Status.QUEUED
                else status.HTTP_201_CREATED
            ),
        )

    @action(detail=True, methods=["post"], url_path="confirm-coverage")
    def confirm_coverage(self, request, pk=None):
        try:
            run = confirm_station_coverage(self.get_object(), request.user, request.data.get("notes"))
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc)
        return Response(self.get_serializer(run).data)

    def partial_update(self, request, *args, **kwargs):
        run = self.get_object()
        if run.status != run.Status.REVIEW_READY:
            return Response(
                {"detail": "Only a review-ready extraction can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ProcessAssemblyProposalSerializer(
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
        serializer = ProcessAssemblyApplySerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        try:
            run = apply_process_assembly_run(
                self.get_object(),
                request.user,
                serializer.validated_data.get("rows"),
            )
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc, {"run_not_review_ready", "hint_only_run", "extraction_inputs_changed", "extraction_already_applied"})
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        try:
            replacement = retry_process_assembly_run(run, request.user, request=request)
            dispatch_process_assembly_run(replacement.id)
            replacement.refresh_from_db()
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc, {"extraction_already_in_progress", "run_not_retryable"})
        return Response(
            self.get_serializer(replacement).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return super().cancel(request, pk)


class ProcessAttributeExtractionRunViewSet(
    ProcessExtractionViewSetMixin,
    viewsets.ModelViewSet,
):
    queryset = ProcessAttributeExtractionRun.objects.all()
    serializer_class = ProcessAttributeExtractionRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = ProcessAttributeExtractionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = get_object_or_404(Project, id=serializer.validated_data["project"])
        try:
            run = create_process_attribute_run(
                project,
                request.user,
                serializer.validated_data["project_language"],
                request=request,
            )
            dispatch_process_attribute_run(run.id)
            run.refresh_from_db()
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc, {"attribute_extraction_already_in_progress"})
        return Response(
            self.get_serializer(run).data,
            status=(
                status.HTTP_202_ACCEPTED
                if run.status == run.Status.QUEUED
                else status.HTTP_201_CREATED
            ),
        )

    def partial_update(self, request, *args, **kwargs):
        run = self.get_object()
        if run.status != run.Status.REVIEW_READY:
            return Response(
                {"detail": "Only a review-ready extraction can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ProcessAttributeProposalSerializer(
            data=request.data.get("attributes"),
            many=True,
        )
        serializer.is_valid(raise_exception=True)
        run.result = {**run.result, "attributes": serializer.validated_data}
        run.modified_by = request.user
        run.save(update_fields=["result", "modified_by", "updated_at"])
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        serializer = ProcessAttributeApplySerializer(data=request.data or {})
        serializer.is_valid(raise_exception=True)
        try:
            run = apply_process_attribute_run(
                self.get_object(),
                request.user,
                serializer.validated_data.get("attributes"),
            )
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc, {"run_not_review_ready", "hint_only_run", "extraction_inputs_changed"})
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        if run.status not in {run.Status.FAILED, run.Status.STALE}:
            return Response(
                {"detail": "Only failed or stale extraction runs can be retried."},
                status=status.HTTP_409_CONFLICT,
            )
        try:
            replacement = create_process_attribute_run(
                run.project,
                request.user,
                run.project_language,
                request=request,
            )
            dispatch_process_attribute_run(replacement.id)
            replacement.refresh_from_db()
        except ProcessRequirementsExtractionError as exc:
            return _error_response(exc, {"attribute_extraction_already_in_progress"})
        return Response(
            self.get_serializer(replacement).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        return super().cancel(request, pk)
