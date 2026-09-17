from django.shortcuts import get_object_or_404
from django.conf import settings
from django.http import Http404
from rest_framework import mixins, serializers, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from django.utils import timezone
from rest_framework.response import Response

from project_management.models import CatalogRecommendationRun
from project_management.pagination import CustomPageNumberPagination
from project_management.project_access import accessible_projects
from project_management.services.catalog_recommendation import (
    create_catalog_run,
    dispatch_catalog_run,
    require_catalog_access,
    confirm_catalog_run,
)


class CatalogRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = CatalogRecommendationRun
        fields = [
            "id",
            "project",
            "status",
            "result",
            "selected_ids",
            "error_code",
            "error_message",
            "created_at",
            "applied_at",
        ]
        read_only_fields = fields


class CatalogCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    project_language = serializers.ChoiceField(choices=["en", "de", "fr"], default="en")


class CatalogConfirmSerializer(serializers.Serializer):
    selected_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1, max_length=100
    )
    acknowledged = serializers.BooleanField()

    def validate_acknowledged(self, value):
        if not value:
            raise serializers.ValidationError(
                "Review the suggestions and acknowledge their limitations first."
            )
        return value


class CatalogRecommendationRunViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    permission_classes = [IsAuthenticated]
    serializer_class = CatalogRunSerializer
    pagination_class = CustomPageNumberPagination

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED:
            raise Http404

    def get_queryset(self):
        project = get_object_or_404(
            accessible_projects(self.request.user),
            pk=self.request.query_params.get("project"),
        )
        require_catalog_access(self.request, project)
        # Check lightweight source IDs before paginating; do not load every
        # historical document snapshot to show a single page of runs.
        from project_management.services.product_assembly_extraction import (
            eligible_customer_demand_documents,
        )

        visible = set(
            eligible_customer_demand_documents(
                project, request=self.request
            ).values_list("id", flat=True)
        )
        ids = [
            pk
            for pk, document_ids in CatalogRecommendationRun.objects.filter(
                project=project
            ).values_list("pk", "input_snapshot__document_ids")
            if set(document_ids or []).issubset(visible)
        ]
        return CatalogRecommendationRun.objects.filter(pk__in=ids).select_related(
            "project"
        )

    def get_object(self):
        run = get_object_or_404(
            CatalogRecommendationRun.objects.filter(
                project__in=accessible_projects(self.request.user)
            ).select_related("project"),
            pk=self.kwargs["pk"],
        )
        require_catalog_access(
            self.request, run.project, write=self.request.method == "POST", run=run
        )
        return run

    def create(self, request):
        data = CatalogCreateSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        project = get_object_or_404(
            accessible_projects(request.user), pk=data.validated_data["project"]
        )
        run = create_catalog_run(
            project, request.user, request, data.validated_data["project_language"]
        )
        dispatch_catalog_run(run.pk)
        run.refresh_from_db()
        return Response(
            self.get_serializer(run).data,
            status=202 if run.status in {"queued", "running"} else 201,
        )

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        data = CatalogConfirmSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        run = confirm_catalog_run(
            self.get_object(), request, data.validated_data["selected_ids"]
        )
        return Response(
            self.get_serializer(run).data, status=409 if run.status == "stale" else 200
        )

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        run = self.get_object()
        # Conditional update ensures a finishing worker cannot revive a cancelled review.
        changed = CatalogRecommendationRun.objects.filter(
            pk=run.pk,
            status__in=["queued", "running", "review_ready", "failed", "stale"],
        ).update(
            status="cancelled", completed_at=timezone.now(), updated_at=timezone.now()
        )
        if not changed:
            return Response(
                {"detail": "This proposal can no longer be cancelled."}, status=409
            )
        run.refresh_from_db()
        return Response(self.get_serializer(run).data)
