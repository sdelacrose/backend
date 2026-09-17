from datetime import datetime
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION

from rest_framework import viewsets
from django.contrib.auth.models import AnonymousUser
from rest_framework import serializers
from rest_framework.response import Response

from rest_framework import viewsets, filters
from rest_framework.decorators import action
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import Q
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from project_management.models import (
    Milestone,
    Project,
)
from project_management.filters import MilestoneFilter
from project_management.serializers import (
    MilestoneDetailSerializer,
    MilestoneListSerializer,
    MilestoneOptionSerializer,
    MilestoneSerializer,
)
from project_management.services.milestone_queries import MilestoneQueryService
from project_management.utils import get_product_name_from_request, has_project_permission

from ecpmpapp.constants import UserType
from project_management.permissions import PhasePermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from django.db.models import Sum, Q, Count
from ecpmpapp.constants import StatusType
from ecpmpapp.ordering import TableOrderingFilter

class MilestoneViewSet(viewsets.ModelViewSet):
    queryset = Milestone.objects.all()
    serializer_class = MilestoneSerializer
    filterset_class = MilestoneFilter
    filter_backends = [DjangoFilterBackend, TableOrderingFilter]
    ordering_fields = [
        "id",
        "sort_order",
        "colour",
        "name",
        "phase_title",     
        "start_date",
        "due_date",         
        "allocated_budget",
        "booked_cost",
        "status",
        "num_documents",
        "num_action_items"
        ]
    ordering = ["sort_order", "id"]
    ordering_text_fields = {"name", "phase_title", "status"}
    ordering_natural_fields = {"name", "phase_title"}

    def get_serializer_class(self):
        if self.action in {"list", "move_up", "move_down"}:
            return MilestoneListSerializer
        if self.action == "retrieve":
            return MilestoneDetailSerializer
        if self.action == "timeline":
            return MilestoneOptionSerializer
        return MilestoneSerializer

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'budget_sum', 'timeline']:
            return [PhasePermission(action='view', scope='project')]
        elif self.action == 'create':
            return [PhasePermission(action='create', scope='project')]
        elif self.action in ['update', 'partial_update', 'move_up', 'move_down']:
            return [PhasePermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [PhasePermission(action='delete', scope='project')]
        return super().get_permissions()
    
    def get_serializer(self, *args, **kwargs):
        serializer = super(MilestoneViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_queryset(self):
        user = self.request.user
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]
        if isinstance(user, AnonymousUser):
            return Milestone.objects.none()
        
        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            self.request.query_params
        )

        has_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'phases_management': ['view', 'full_access']}, 
            'project', 
            self.request.query_params
        )
        query_filters = Q()
        if not user.is_freelancer and has_projects_company_permissions:
            query_filters = (
                Q(project__isnull=False, project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
            )
        elif has_project_permissions:
            query_filters = Q(project__team_members__team_member=user, project__solution__in=products)
        else:
            return Milestone.objects.none()
        queryset = Milestone.objects.filter(query_filters).distinct()
        if self.action in {"list", "retrieve", "move_up", "move_down"}:
            return MilestoneQueryService.with_read_projection(
                queryset,
                detail=self.action == "retrieve",
            )
        return queryset

    def get_object(self):
        queryset = self.get_queryset() if self.action == "retrieve" else Milestone.objects.all()
        obj = get_object_or_404(queryset, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def perform_update(self, serializer):
        instance = serializer.instance
        user = self.request.user
        status = self.request.data.get('status')

        if instance.status == "closed":
            raise serializers.ValidationError("A closed milestone cannot be updated.")

        if status == "closed" and instance.status != "closed":
            serializer.save(closed_at=datetime.now().date(), closed_by=user)
        else:
            serializer.save()

    @action(detail=False, methods=["get"], url_path="sum")
    def budget_sum(self, request):
        queryset = self.filter_queryset(self.get_queryset())
        total_budget = queryset.aggregate(total=Sum("allocated_budget"))["total"] or 0
        return Response({"total_budget": total_budget})

    @action(detail=False, methods=["get"], url_path="timeline")
    def timeline(self, request):
        queryset = self.filter_queryset(self.get_queryset()).order_by(
            "sort_order", "id"
        )
        return Response(self.get_serializer(queryset, many=True).data)

    @action(detail=True, methods=["post"], url_path="move-up")
    def move_up(self, request, pk=None):
        return self._move(direction=-1)

    @action(detail=True, methods=["post"], url_path="move-down")
    def move_down(self, request, pk=None):
        return self._move(direction=1)

    @transaction.atomic
    def _move(self, direction):
        requested_phase = self.get_object()
        Project.objects.select_for_update().only("id").get(
            pk=requested_phase.project_id
        )
        phases = list(
            Milestone.objects.select_for_update()
            .filter(project_id=requested_phase.project_id)
            .order_by("sort_order", "id")
        )
        current_index = next(
            index
            for index, phase in enumerate(phases)
            if phase.id == requested_phase.id
        )
        target_index = current_index + direction

        if 0 <= target_index < len(phases):
            phases[current_index], phases[target_index] = (
                phases[target_index],
                phases[current_index],
            )

        updated_at = timezone.now()
        changed_phases = []
        for sort_order, phase in enumerate(phases, start=1):
            if phase.sort_order == sort_order:
                continue
            phase.sort_order = sort_order
            phase.updated_at = updated_at
            changed_phases.append(phase)

        if changed_phases:
            Milestone.objects.bulk_update(
                changed_phases,
                ["sort_order", "updated_at"],
            )

        moved_phase = MilestoneQueryService.with_read_projection(
            Milestone.objects.filter(pk=requested_phase.id)
        ).get()
        return Response(self.get_serializer(moved_phase).data)

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter('due_date_after', openapi.IN_QUERY, description="Filter by start date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('due_date_before', openapi.IN_QUERY, description="Filter by end date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE)
        ]
    )
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)
