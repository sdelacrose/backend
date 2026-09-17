from datetime import datetime
import logging
from django.contrib.auth.models import AnonymousUser
from rest_framework import serializers

from rest_framework import viewsets, filters
from rest_framework.decorators import action
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db import transaction
from django.db.models import OuterRef, Prefetch, Subquery
from django.core.exceptions import PermissionDenied
from companyprofileapis.models import Opportunity
from project_management.models import (
    ActionItem,
    ActionItemDependency,
    ExpenseCard,
    Milestone,
    Project,
    ProjectPhase,
    TimeCard,
)
from project_management.filters import ActionItemFilter
from project_management.action_item_dependencies import (
    filter_dependent_candidates_by_update_permission,
    filter_dependency_candidates_by_visibility,
    filter_predecessor_candidates_by_visibility,
    get_dependent_candidate_queryset,
    get_predecessor_candidate_queryset,
    search_dependent_candidates,
    search_predecessor_candidates,
    serialize_dependent_candidate,
    serialize_predecessor_candidate,
)
from project_management.action_item_conversion import (
    MakeSubActionSerializer,
    convert_action_item_to_sub_action,
    filter_parent_candidates_by_visibility,
    get_action_item_parent_candidate_queryset,
    search_parent_candidates,
    serialize_parent_candidate,
)
from project_management.serializers import (
    ActionItemBoardSerializer,
    ActionItemCalendarSerializer,
    ActionItemListSerializer,
    ActionItemSerializer,
    ActionItemSubActionSerializer,
)
from project_management.services.action_item_queries import (
    ActionItemFilterOptionsService,
    ActionItemFilterService,
    ActionItemOrderingService,
    ActionItemQueryService,
    ActionItemVisibilityService,
)
from project_management.services.action_item_gantt import (
    ActionItemGanttPayloadService,
)
from project_management.services.action_item_bulk_update import (
    ActionItemBulkUpdateService,
    BulkActionItemUpdateRequestSerializer,
)
from ecpmpapp.constants import StatusType
from ecpmpapp.ordering import TableOrderingFilter
from project_management.permissions import ActionItemPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from django.db.models import Count, Sum, F, FloatField, IntegerField
from django.db.models.functions import Coalesce, NullIf
from django.db.models import Value
from rest_framework.response import Response
from rest_framework import status

logger = logging.getLogger(__name__)


def _predecessor_dependencies_prefetch(lookup):
    return Prefetch(
        lookup,
        queryset=ActionItemDependency.objects.filter(
            dependency_type=(
                ActionItemDependency.DependencyType.FINISH_TO_START
            )
        )
        .select_related(
            "predecessor",
            "predecessor__milestone",
            "predecessor__parent_action_item",
            "predecessor__parent_action_item__milestone",
        )
        .order_by("id"),
        to_attr="prefetched_predecessor_dependencies",
    )


def _dependent_dependencies_prefetch(lookup):
    return Prefetch(
        lookup,
        queryset=ActionItemDependency.objects.filter(
            dependency_type=(
                ActionItemDependency.DependencyType.FINISH_TO_START
            )
        )
        .select_related(
            "dependent",
            "dependent__milestone",
            "dependent__parent_action_item",
            "dependent__parent_action_item__milestone",
        )
        .order_by("id"),
        to_attr="prefetched_dependent_dependencies",
    )


class ActionItemViewSet(viewsets.ModelViewSet):
    queryset = ActionItem.objects.all()
    serializer_class = ActionItemSerializer
    filter_backends = [filters.SearchFilter, TableOrderingFilter, DjangoFilterBackend]
    filterset_class = ActionItemFilter
    search_fields = [
        "id",
        "name",
        "status",
        "milestone__name",
        "milestone__id",
        "assignees__first_name",
        "assignees__last_name",
        "allocated_budget",
    ]

    ordering_fields = [
        "id",
        "name",
        "status",
        "milestone__id",
        "allocated_time",
        "start_date",
        "target_date",
        "forecast_date",
        "closed_at",
        "num_documents",
        "num_sub_action_items",
        "assignees_count",
        "booked_hour",
        "booked_cost",
        "priority_level",
        "allocated_budget"
    ]
    ordering_field_aliases = {
        "allocated_time": "allocated_time_sort",
        "allocated_budget": "allocated_budget_sort",
    }
    ordering_text_fields = {"name", "status", "priority_level"}
    ordering_natural_fields = {"name"}
    ordering = ["-id"]  # or whatever default you want

    def get_serializer_class(self):
        if self.action == "list":
            return ActionItemListSerializer
        if self.action == "sub_action_items":
            return ActionItemSubActionSerializer
        if self.action in {"board", "time"}:
            return ActionItemBoardSerializer
        if self.action == "calendar":
            return ActionItemCalendarSerializer
        return ActionItemSerializer

    @staticmethod
    def _with_ordering_aliases(queryset):
        return queryset.alias(
            allocated_time_sort=NullIf(F("allocated_time"), Value(0)),
            allocated_budget_sort=NullIf(F("allocated_budget"), Value(0.0)),
        )

    def is_company_action_item(self, request):
        if request.data.get('company_profile'):
            return True
        elif request.data.get('project'):
            return False
        elif request.data.get('parent_action_item'):
            try:
                parent_action_item = ActionItem.objects.get(id=request.data.get('parent_action_item'))
                return parent_action_item.milestone is None
            except ActionItem.DoesNotExist:
                return False
        return False

    def get_permissions(self):
        if self.action in {
            'list',
            'filter_options',
            'board',
            'time',
            'calendar',
            'gantt',
            'sub_action_items',
        }:
            if self.request.query_params.get('project'):
                return [ActionItemPermission('view', 'project')]
            elif self.request.query_params.get('company_profile'):
                return [ActionItemPermission('view', 'company')]
            return [ActionItemPermission('view', 'company')]
        elif self.action in [
            'retrieve',
            'predecessor_candidates',
            'dependent_candidates',
            'parent_candidates',
        ]:
            # Use a default permission and let has_object_permission handle the logic
            return [ActionItemPermission('view', 'company')]
        elif self.action == 'create':
            if self.is_company_action_item(self.request):
                return [ActionItemPermission('create', 'company')]
            else:
                return [ActionItemPermission('create', 'project')]
        elif self.action in [
            'update',
            'partial_update',
            'make_sub_action',
            'bulk_update',
        ]:
            # Use a default permission and let has_object_permission handle the logic
            return [ActionItemPermission('update', 'company')]
        elif self.action == 'destroy':
            # Use a default permission and let has_object_permission handle the logic
            return [ActionItemPermission('delete', 'company')]
        return super().get_permissions()
    
    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        context["visible_action_item_ids"] = getattr(
            self, "_visible_action_item_ids", set()
        )
        context["limit_sub_action_items_to_visible"] = getattr(
            self, "_limit_sub_action_items_to_visible", False
        )
        context["include_predecessors"] = True
        context["include_dependents"] = self.action in (
            "retrieve",
            "update",
            "partial_update",
        )
        context["serialize_sub_action_items"] = self.action not in (
            "create",
            "retrieve",
            "update",
            "partial_update",
        )
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return self._with_ordering_aliases(ActionItem.objects.none())

        visibility = ActionItemVisibilityService(
            self.request,
            permission_checker=require_any_permission_multiple_modules,
        ).resolve()
        self._visible_action_item_ids = set(visibility.directly_visible_ids)
        self._limit_sub_action_items_to_visible = (
            visibility.limit_sub_action_items_to_visible
        )
        queryset = self._with_ordering_aliases(visibility.queryset)

        if self.action in {"list", "filter_options", "board", "time", "calendar", "gantt"}:
            return queryset

        approved_time_card_hours = (
            TimeCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("hours"))
            .values("total")[:1]
        )
        approved_expense_card_total = (
            ExpenseCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("total"))
            .values("total")[:1]
        )

        queryset = queryset.annotate(
            num_documents=Count('documents', distinct=True),
            num_sub_action_items=Count('sub_action_items', distinct=True),
            assignees_count=Count('assignees', distinct=True),
            booked_hour=Coalesce(
                Subquery(approved_time_card_hours, output_field=FloatField()),
                Value(0.0),
                output_field=FloatField(),
            ),
            booked_cost=Coalesce(
                Subquery(approved_expense_card_total, output_field=FloatField()),
                Value(0.0),
                output_field=FloatField(),
            ),
        )

        return (
            queryset.select_related(
                "milestone",
                "milestone__project",
                "company_profile",
                "freelancer",
                "assignee",
                "created_by",
                "closed_by",
                "parent_action_item",
                "parent_action_item__milestone",
                "parent_action_item__company_profile",
                "parent_action_item__freelancer",
            )
            .prefetch_related(
                "assignees",
                "documents",
                "requirements",
                "opportunities",
                "sub_action_items",
                "sub_action_items__assignees",
                "sub_action_items__documents",
                "sub_action_items__requirements",
                "sub_action_items__opportunities",
                _predecessor_dependencies_prefetch(
                    "incoming_dependencies"
                ),
                _predecessor_dependencies_prefetch(
                    "sub_action_items__incoming_dependencies"
                ),
            )
            .order_by('-id')
        )

    def get_company_id_freelancer_or_project_id_from_obj(self, obj):
        project = None
        company_profile = None
        freelancer = None
        if obj.milestone:
            try:
                project = obj.milestone.project
            except:
                project = None
        elif obj.company_profile:
            company_profile = obj.company_profile
        elif obj.freelancer:
            freelancer = obj.freelancer
        return project, company_profile, freelancer

    def get_object(self):
        pk = self.kwargs.get('pk')
        if not pk:
            from django.http import Http404
            raise Http404

        dependency_prefetches = [
            _predecessor_dependencies_prefetch("incoming_dependencies"),
        ]
        if self.action in {"retrieve", "update", "partial_update"}:
            dependency_prefetches.append(
                _dependent_dependencies_prefetch(
                    "outgoing_dependencies"
                )
            )

        child_count = (
            ActionItem.objects.filter(parent_action_item_id=OuterRef("pk"))
            .values("parent_action_item_id")
            .annotate(total=Count("id"))
            .values("total")[:1]
        )
        approved_time_card_hours = (
            TimeCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("hours"))
            .values("total")[:1]
        )
        approved_expense_card_total = (
            ExpenseCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("total"))
            .values("total")[:1]
        )

        detail_queryset = (
            ActionItem.objects.annotate(
                num_sub_action_items=Coalesce(
                    Subquery(child_count, output_field=IntegerField()),
                    Value(0),
                ),
                booked_hour=Coalesce(
                    Subquery(
                        approved_time_card_hours,
                        output_field=FloatField(),
                    ),
                    Value(0.0),
                    output_field=FloatField(),
                ),
                booked_cost=Coalesce(
                    Subquery(
                        approved_expense_card_total,
                        output_field=FloatField(),
                    ),
                    Value(0.0),
                    output_field=FloatField(),
                ),
            )
            .select_related(
                "milestone",
                "milestone__project",
                "milestone__project__configurations",
                "company_profile",
                "company_profile__global_config",
                "freelancer",
                "assignee",
                "created_by",
                "closed_by",
                "parent_action_item",
                "parent_action_item__milestone",
                "parent_action_item__milestone__project",
                "parent_action_item__milestone__project__configurations",
                "parent_action_item__company_profile",
                "parent_action_item__freelancer",
            )
            .prefetch_related(
                "assignees",
                "documents",
                "linked_folders",
                "requirements",
                Prefetch(
                    "opportunities",
                    queryset=Opportunity.objects.select_related(
                        "customer_user",
                        "customer_company",
                    ),
                ),
                *dependency_prefetches,
            )
        )

        obj = get_object_or_404(
            detail_queryset,
            pk=pk,
        )
        self.check_object_permissions(self.request, obj)
        if obj.parent_action_item:
            project, company_profile, freelancer = self.get_company_id_freelancer_or_project_id_from_obj(obj.parent_action_item)
        else:
            project, company_profile, freelancer = self.get_company_id_freelancer_or_project_id_from_obj(obj)

        if company_profile and not (company_profile == self.request.user.company):
            raise PermissionDenied("You do not have permission to access this ActionItem's company profile.")
        if freelancer and not (freelancer == self.request.user):
            raise PermissionDenied("You do not have permission to access this ActionItem's freelancer.")

        return obj

    def perform_update(self, serializer):
        user = self.request.user
        requested_status = self.request.data.get('status')

        with transaction.atomic():
            instance = ActionItem.objects.select_for_update().get(
                pk=serializer.instance.pk
            )
            self.check_object_permissions(self.request, instance)
            serializer.instance = instance

            if instance.status == "closed":
                raise serializers.ValidationError(
                    "A closed action item cannot be updated."
                )

            if requested_status == "closed":
                updated_instance = serializer.save(
                    closed_at=datetime.now().date(),
                    closed_by=user,
                )
            else:
                updated_instance = serializer.save()

        if requested_status is not None:
            logger.info(
                "ActionItem status update: id=%s user_id=%s requested=%s final=%s",
                instance.id,
                getattr(user, "id", None),
                requested_status,
                updated_instance.status,
            )

    @action(detail=False, methods=["patch"], url_path="bulk-update")
    def bulk_update(self, request):
        request_serializer = BulkActionItemUpdateRequestSerializer(
            data=request.data
        )
        request_serializer.is_valid(raise_exception=True)
        result = ActionItemBulkUpdateService(
            request=request,
            serializer_context=self.get_serializer_context(),
        ).execute(
            action_item_ids=request_serializer.validated_data["ids"],
            changes=request_serializer.validated_data["changes"],
        )
        return Response(result, status=status.HTTP_200_OK)

    @action(
        detail=True,
        methods=["get"],
        url_path="predecessor-candidates",
    )
    def predecessor_candidates(self, request, pk=None):
        dependent = self.get_object()
        update_permission = ActionItemPermission(
            action="update",
        )
        if not update_permission.can_update_object(
            request.user,
            dependent,
        ):
            raise PermissionDenied(
                "You do not have permission to update this Action Item."
            )

        queryset = get_predecessor_candidate_queryset(dependent)
        queryset = filter_predecessor_candidates_by_visibility(
            queryset,
            request.user,
            dependent,
        )
        queryset = search_predecessor_candidates(
            queryset,
            request.query_params.get("search"),
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                [
                    serialize_predecessor_candidate(candidate)
                    for candidate in page
                ]
            )
        return Response(
            [
                serialize_predecessor_candidate(candidate)
                for candidate in queryset
            ]
        )

    @action(
        detail=True,
        methods=["get"],
        url_path="dependent-candidates",
    )
    def dependent_candidates(self, request, pk=None):
        predecessor = self.get_object()
        update_permission = ActionItemPermission(
            action="update",
        )
        if not update_permission.can_update_object(
            request.user,
            predecessor,
        ):
            raise PermissionDenied(
                "You do not have permission to update this Action Item."
            )

        queryset = get_dependent_candidate_queryset(predecessor)
        queryset = filter_dependency_candidates_by_visibility(
            queryset,
            request.user,
            predecessor,
        )
        queryset = filter_dependent_candidates_by_update_permission(
            queryset,
            request.user,
            predecessor,
        )
        queryset = search_dependent_candidates(
            queryset,
            request.query_params.get("search"),
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                [
                    serialize_dependent_candidate(candidate)
                    for candidate in page
                ]
            )
        return Response(
            [
                serialize_dependent_candidate(candidate)
                for candidate in queryset
            ]
        )

    @action(
        detail=True,
        methods=["get"],
        url_path="parent-candidates",
    )
    def parent_candidates(self, request, pk=None):
        action_item = self.get_object()
        update_permission = ActionItemPermission(action="update")
        if not update_permission.can_update_object(request.user, action_item):
            raise PermissionDenied(
                "You do not have permission to update this Action Item."
            )

        queryset = get_action_item_parent_candidate_queryset(action_item)
        queryset = filter_parent_candidates_by_visibility(
            queryset,
            request.user,
            action_item,
        )
        queryset = search_parent_candidates(
            queryset,
            request.query_params.get("search"),
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(
                [serialize_parent_candidate(candidate) for candidate in page]
            )
        return Response(
            [serialize_parent_candidate(candidate) for candidate in queryset],
            status=status.HTTP_200_OK,
        )

    @action(
        detail=True,
        methods=["post"],
        url_path="make-sub-action",
    )
    def make_sub_action(self, request, pk=None):
        action_item = self.get_object()
        input_serializer = MakeSubActionSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        data = input_serializer.validated_data

        converted_action_item = convert_action_item_to_sub_action(
            action_item_id=action_item.id,
            parent_action_item_id=data["parent_action_item_id"],
            user=request.user,
            start_date=data.get("start_date"),
            start_date_provided="start_date" in data,
            target_date=data.get("target_date"),
            target_date_provided="target_date" in data,
            confirm_dependency_removal=data[
                "confirm_dependency_removal"
            ],
        )
        output_serializer = self.get_serializer(converted_action_item)
        return Response(output_serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="sub-action-items")
    def sub_action_items(self, request, pk=None):
        parent = self.get_object()
        queryset = parent.sub_action_items.all()

        visibility = ActionItemVisibilityService(
            request,
            permission_checker=require_any_permission_multiple_modules,
        )
        if visibility.should_limit_sub_action_items():
            queryset = visibility.directly_visible_queryset(queryset)

        queryset = ActionItemQueryService.with_board_projection(
            queryset
        ).order_by("id")
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(
            page if page is not None else queryset,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter('target_date_after', openapi.IN_QUERY, description="Filter by start date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('target_date_before', openapi.IN_QUERY, description="Filter by end date", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('forecast_date_after', openapi.IN_QUERY, description="Filter by forecast date start", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE),
            openapi.Parameter('forecast_date_before', openapi.IN_QUERY, description="Filter by forecast date end", type=openapi.TYPE_STRING, format=openapi.FORMAT_DATE)
        ]
    )
    def list(self, request, *args, **kwargs):
        visible = self.get_queryset()
        filters_service = ActionItemFilterService(request.query_params)
        parents = filters_service.filter_tree(
            visible.filter(parent_action_item__isnull=True)
        )
        children = filters_service.filter_children(
            visible.filter(parent_action_item__isnull=False)
        )
        queryset = ActionItemQueryService.with_list_projection(
            parents,
            children=children,
        )
        queryset = ActionItemOrderingService.apply(
            queryset,
            scope="project" if request.query_params.get("project") else "company",
            ordering=request.query_params.get("ordering"),
        )

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        data = serializer.data
        return Response(data)

    @action(detail=False, methods=["get"], url_path="filter-options")
    def filter_options(self, request):
        return Response(
            ActionItemFilterOptionsService.build(self.get_queryset(), request)
        )

    def _paginated_flat_response(self, request, queryset, *, default_ordering):
        queryset = ActionItemQueryService.with_board_projection(queryset)
        queryset = ActionItemOrderingService.apply(
            queryset,
            scope="project" if request.query_params.get("project") else "company",
            ordering=request.query_params.get("ordering") or default_ordering,
        )
        page = self.paginate_queryset(queryset)
        serializer = self.get_serializer(
            page if page is not None else queryset,
            many=True,
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="board")
    def board(self, request):
        queryset = ActionItemFilterService(request.query_params).filter_flat(
            self.get_queryset()
        )
        return self._paginated_flat_response(
            request,
            queryset,
            default_ordering="id",
        )

    @action(detail=False, methods=["get"], url_path="time")
    def time(self, request):
        filters_service = ActionItemFilterService(request.query_params)
        queryset = filters_service.filter_flat(self.get_queryset())
        queryset = filters_service.filter_time_bucket(queryset)
        return self._paginated_flat_response(
            request,
            queryset,
            default_ordering="target_date",
        )

    @action(detail=False, methods=["get"], url_path="calendar")
    def calendar(self, request):
        filters_service = ActionItemFilterService(request.query_params)
        queryset = filters_service.filter_flat(self.get_queryset())
        queryset = filters_service.filter_calendar_range(queryset)
        queryset = ActionItemQueryService.with_calendar_projection(queryset)
        queryset = queryset.order_by("start_date", "target_date", "id")
        return Response(self.get_serializer(queryset, many=True).data)

    @action(detail=False, methods=["get"], url_path="gantt")
    def gantt(self, request):
        filters_service = ActionItemFilterService(request.query_params)
        visible = self.get_queryset()
        parents = filters_service.filter_tree(
            visible.filter(parent_action_item__isnull=True)
        )
        children = filters_service.filter_children(
            visible.filter(parent_action_item__isnull=False)
        )
        gantt_data = ActionItemGanttPayloadService.build(parents, children)

        project_id = request.query_params.get("project")
        project = None
        phases = []
        project_milestones = []
        if project_id:
            project_scope = ActionItemVisibilityService(
                request,
                permission_checker=require_any_permission_multiple_modules,
            )
            project_instance = get_object_or_404(
                Project.objects.filter(
                    solution__in=project_scope.allowed_products(),
                ),
                pk=project_id,
            )
            project = {
                "id": project_instance.id,
                "project_name": project_instance.project_name,
                "project_start_date": project_instance.project_start_date,
                "project_end_date": project_instance.project_end_date,
            }
            phases = list(
                Milestone.objects.filter(project=project_instance)
                .order_by("sort_order", "id")
                .values(
                    "id",
                    "sort_order",
                    "name",
                    "description",
                    "colour",
                    "phase_title",
                    "start_date",
                    "due_date",
                    "status",
                    "allocated_budget",
                    "allocated_time",
                    "reminder_setting",
                    "project_id",
                )
            )
            project_milestones = list(
                ProjectPhase.objects.filter(project=project_instance)
                .order_by("date", "id")
                .values("id", "name", "date", "status", "amount")
            )

        payload = {
            "project": project,
            "phases": phases,
            "project_milestones": project_milestones,
            **gantt_data,
        }
        return Response(payload)
