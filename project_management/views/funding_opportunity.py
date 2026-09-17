from django.conf import settings
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from project_management.models import ProjectFunding, ProjectFundingMatch
from project_management.pagination import CustomPageNumberPagination
from project_management.permissions import FundingPermission
from project_management.project_access import accessible_projects
from project_management.serializers.funding_opportunity import (
    FundingOpportunityMatchSerializer,
    FundingOpportunityMatchStatusSerializer,
)


class FundingOpportunityConvertPermission(FundingPermission):
    """Apply the existing funding-create grant to explicit match conversion."""

    def __init__(self):
        super().__init__(action="create", scope="project")

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        return self._check_create_permission(request.user, {"project": obj.project})


class FundingOpportunityMatchViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = FundingOpportunityMatchSerializer
    pagination_class = CustomPageNumberPagination

    def get_permissions(self):
        funding_permission = (
            FundingOpportunityConvertPermission()
            if self.action == "convert"
            else FundingPermission(
                action="update" if self.action == "partial_update" else "view",
                scope="project",
            )
        )
        return [IsAuthenticated(), funding_permission]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.FUNDING_OPPORTUNITY_MATCHING_ENABLED:
            raise Http404

    def get_queryset(self):
        queryset = (
            ProjectFundingMatch.objects.filter(
                project__in=accessible_projects(self.request.user),
                relevance_score__gte=getattr(settings, "FUNDING_MATCH_THRESHOLD", 60),
            )
            .select_related(
                "project",
                "revision__opportunity__source",
                "converted_funding",
            )
            .distinct()
        )
        project_id = self.request.query_params.get("project")
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        status_value = self.request.query_params.get("status")
        if status_value:
            queryset = queryset.filter(status=status_value)
        return queryset

    def partial_update(self, request, *args, **kwargs):
        match = self.get_object()
        serializer = FundingOpportunityMatchStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        match.status = serializer.validated_data["status"]
        match.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(match).data)

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def convert(self, request, pk=None):
        match = self.get_object()
        if match.converted_funding_id:
            return Response(self.get_serializer(match).data)
        funding = ProjectFunding.objects.create(
            project=match.project,
            name=match.revision.title[:255],
            created_by=request.user,
        )
        match.converted_funding = funding
        match.status = ProjectFundingMatch.STATUS_CONVERTED
        match.save(update_fields=["converted_funding", "status", "updated_at"])
        return Response(self.get_serializer(match).data, status=status.HTTP_201_CREATED)
