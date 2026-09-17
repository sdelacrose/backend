from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from project_management.models import BomGenerationRun
from project_management.permissions import BillOfMaterialPermission
from project_management.project_access import accessible_projects
from project_management.serializers.bom import (
    BomApplyRequestSerializer,
    BomConflictResolutionRequestSerializer,
    BomGenerationRequestSerializer,
    BomGenerationRunSerializer,
)
from project_management.services.workstation_bom import (
    BomPlanConflict,
    apply_bom_plan,
    create_bom_plan,
    create_queued_bom_plan,
    dispatch_bom_plan_run,
    estimate_bom_plan_node_count,
    resolve_bom_plan_conflicts,
)


class BomGenerationRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = BomGenerationRunSerializer
    filter_backends = [filters.OrderingFilter]
    ordering_fields = ["id", "created_at", "updated_at", "status"]
    ordering = ["-created_at", "-id"]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.PROCESS_ASSEMBLY_BOM_ENABLED:
            raise Http404

    def get_permissions(self):
        permission_action = (
            "create" if self.action in {"create", "apply", "resolve_conflicts"} else "view"
        )
        return [BillOfMaterialPermission(action=permission_action, scope="project")]

    def get_queryset(self):
        queryset = (
            BomGenerationRun.objects.filter(
                project__in=accessible_projects(self.request.user),
                internal_run=False,
            )
            .select_related("project", "created_by", "applied_by")
            .prefetch_related("workstation_snapshots", "operations")
            .distinct()
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        status_value = self.request.query_params.get("status")
        if status_value in dict(BomGenerationRun.STATUS_CHOICES):
            queryset = queryset.filter(status=status_value)
        return queryset

    def create(self, request, *args, **kwargs):
        serializer = BomGenerationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = get_object_or_404(
            accessible_projects(request.user),
            id=serializer.validated_data["project"],
        )
        try:
            workstation_ids = serializer.validated_data["workstations"]
            _, node_count = estimate_bom_plan_node_count(
                project=project,
                workstation_ids=workstation_ids,
            )
            if node_count > settings.PROCESS_BOM_ASYNC_NODE_THRESHOLD:
                queued_result = create_queued_bom_plan(
                    project=project,
                    workstation_ids=workstation_ids,
                    actor=request.user,
                    idempotency_key=serializer.validated_data["idempotency_key"],
                )
                run = queued_result.plan
                if queued_result.created:
                    dispatch_bom_plan_run(run.id)
            else:
                run = create_bom_plan(
                    project=project,
                    workstation_ids=workstation_ids,
                    actor=request.user,
                    idempotency_key=serializer.validated_data["idempotency_key"],
                )
        except DjangoValidationError as exc:
            raise ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else exc.messages,
            )
        run = self.get_queryset().get(pk=run.pk)
        response_status = (
            status.HTTP_202_ACCEPTED
            if run.status in {
                BomGenerationRun.STATUS_QUEUED,
                BomGenerationRun.STATUS_RUNNING,
            }
            else status.HTTP_201_CREATED
        )
        return Response(self.get_serializer(run).data, status=response_status)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        plan = self.get_object()
        serializer = BomApplyRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = apply_bom_plan(
                plan=plan,
                actor=request.user,
                expected_target_hash=serializer.validated_data["expected_target_hash"],
                confirm_detach=serializer.validated_data["confirm_detach"],
            )
        except BomPlanConflict as exc:
            message = str(exc)
            response_status = (
                status.HTTP_422_UNPROCESSABLE_ENTITY
                if "confirmation" in message.lower() or "unresolved" in message.lower()
                else status.HTTP_409_CONFLICT
            )
            return Response({"detail": message}, status=response_status)
        refreshed = self.get_queryset().get(pk=result.plan.pk)
        payload = self.get_serializer(refreshed).data
        payload["created"] = result.created
        return Response(payload)

    @action(detail=True, methods=["post"], url_path="resolve-conflicts")
    def resolve_conflicts(self, request, pk=None):
        plan = self.get_object()
        serializer = BomConflictResolutionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            resolve_bom_plan_conflicts(
                plan=plan,
                resolutions=serializer.validated_data["resolutions"],
            )
        except BomPlanConflict as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        refreshed = self.get_queryset().get(pk=plan.pk)
        return Response(self.get_serializer(refreshed).data)
