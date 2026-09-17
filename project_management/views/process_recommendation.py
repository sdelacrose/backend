from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from project_management.models import ProcessAssemblyRecommendationRun
from project_management.permissions import RequirementPermission
from project_management.project_access import accessible_projects
from project_management.serializers.process_recommendation import (
    ProcessAssemblyRecommendationCreateSerializer,
    ProcessAssemblyRecommendationRunSerializer,
    ProcessAssemblyRecommendationSelectSerializer,
)
from project_management.services.process_recommendation import (
    create_process_recommendation_run,
    override_process_recommendation,
)
from project_management.views.product_assembly_extraction import (
    ProductAssemblyProjectManagerPermission,
)


class ProcessAssemblyRecommendationRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ProcessAssemblyRecommendationRunSerializer

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED:
            raise Http404

    def get_permissions(self):
        if self.action in {"create", "select"}:
            return [
                RequirementPermission(action="create", scope="project"),
                ProductAssemblyProjectManagerPermission(),
            ]
        return [RequirementPermission(action="view", scope="project")]

    def get_queryset(self):
        queryset = ProcessAssemblyRecommendationRun.objects.filter(
            project__in=accessible_projects(self.request.user),
        ).select_related(
            "project",
            "selected_candidate",
            "created_workstation",
            "created_by",
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        return queryset.distinct()

    def create(self, request, *args, **kwargs):
        serializer = ProcessAssemblyRecommendationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = get_object_or_404(
            accessible_projects(request.user),
            id=serializer.validated_data["project"],
        )
        run = create_process_recommendation_run(project, request.user)
        run = self.get_queryset().get(pk=run.pk)
        return Response(self.get_serializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def select(self, request, pk=None):
        serializer = ProcessAssemblyRecommendationSelectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            run = override_process_recommendation(
                run=self.get_object(),
                candidate_id=serializer.validated_data["candidate"],
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
            )
        return Response(self.get_serializer(run).data)
