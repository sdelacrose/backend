from django.db.models import Exists, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from project_management.models import (
    ProductAttributeExtractionRun,
    ProductAttributeExtractionSource,
    Project,
)
from project_management.permissions import SolutionDemandPermission
from project_management.serializers.product_attribute_extraction import (
    ProductAttributeApplySerializer,
    ProductAttributeExtractionCreateSerializer,
    ProductAttributeExtractionRunSerializer,
    ProductAttributeProposalSerializer,
)
from project_management.services.document_access import get_accessible_document_queryset
from project_management.services.product_attribute_extraction import (
    ProductAttributeExtractionError,
    apply_attribute_extraction_run,
    create_attribute_extraction_run,
    dispatch_attribute_extraction_run,
    retry_attribute_extraction_run,
)
from project_management.views.product_assembly_extraction import (
    ProductAssemblyProjectManagerPermission,
)


class ProductAttributeExtractionRunViewSet(viewsets.ModelViewSet):
    serializer_class = ProductAttributeExtractionRunSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        if self.action in ["create", "partial_update", "apply", "retry", "cancel"]:
            return [
                SolutionDemandPermission(action="create", scope="project"),
                ProductAssemblyProjectManagerPermission(),
            ]
        return [SolutionDemandPermission(action="view", scope="project")]

    def get_queryset(self):
        user = self.request.user
        project_id = self.request.query_params.get("project")
        queryset = ProductAttributeExtractionRun.objects.select_related("project").prefetch_related(
            "sources__document",
            "applied_columns__applied_values",
        )
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        if not user.is_authenticated:
            return queryset.none()
        if user.is_freelancer:
            access_filter = Q(project__user=user) | Q(project__team_members__team_member=user)
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
        inaccessible_sources = ProductAttributeExtractionSource.objects.filter(
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
        input_serializer = ProductAttributeExtractionCreateSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data
        project = get_object_or_404(Project, id=data["project"])
        try:
            run = create_attribute_extraction_run(
                project=project,
                user=request.user,
                document_ids=data.get("document_ids"),
                project_language=data["project_language"],
                request=request,
            )
            dispatch_attribute_extraction_run(run.id)
            run.refresh_from_db()
        except ProductAttributeExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=(
                    status.HTTP_409_CONFLICT
                    if exc.code == "attribute_extraction_already_in_progress"
                    else status.HTTP_400_BAD_REQUEST
                ),
            )
        return Response(
            self.get_serializer(run).data,
            status=(
                status.HTTP_201_CREATED
                if run.status != ProductAttributeExtractionRun.Status.QUEUED
                else status.HTTP_202_ACCEPTED
            ),
        )

    def partial_update(self, request, *args, **kwargs):
        run = self.get_object()
        if run.hint_only:
            return Response(
                {"detail": "Suggestion-only extractions cannot be edited or applied."},
                status=status.HTTP_409_CONFLICT,
            )
        if run.status != ProductAttributeExtractionRun.Status.REVIEW_READY:
            return Response(
                {"detail": "Only a review-ready extraction can be edited."},
                status=status.HTTP_409_CONFLICT,
            )
        serializer = ProductAttributeProposalSerializer(
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
        run = self.get_object()
        input_serializer = ProductAttributeApplySerializer(data=request.data or {})
        input_serializer.is_valid(raise_exception=True)
        try:
            run = apply_attribute_extraction_run(
                run,
                request.user,
                attributes=input_serializer.validated_data.get("attributes"),
                request=request,
            )
        except ProductAttributeExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(self.get_serializer(run).data)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        run = self.get_object()
        try:
            replacement = retry_attribute_extraction_run(
                run=run,
                user=request.user,
                request=request,
            )
            dispatch_attribute_extraction_run(replacement.id)
            replacement.refresh_from_db()
        except ProductAttributeExtractionError as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(self.get_serializer(replacement).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if run.status not in {
            ProductAttributeExtractionRun.Status.QUEUED,
            ProductAttributeExtractionRun.Status.EXTRACTING,
        }:
            return Response(
                {"detail": "Only queued or active extraction runs can be cancelled."},
                status=status.HTTP_409_CONFLICT,
            )
        run.status = ProductAttributeExtractionRun.Status.CANCELLED
        run.modified_by = request.user
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "modified_by", "completed_at", "updated_at"])
        return Response(self.get_serializer(run).data)
