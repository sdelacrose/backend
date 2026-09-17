from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime

from drf_yasg import openapi
from drf_yasg.utils import swagger_auto_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ecpmpapp.constants import UserType
from ecpmpapp.utils import check_for_freelancer_or_company_owner, require_any_permission
from models.models import CompanyConfig
from models.rooms_utils import effective_rooms_list
from project_management.models import ActionItem, Milestone, Project, ProjectPhase
from project_management.serializers import (
    ActionItemSerializer,
    MilestoneSerializer,
    ProjectPhaseSerializer,
)
from project_management.utils import get_product_name_from_request
from userapis.models import CalendarEvent
from userapis.microsoft_outlook import is_outlook_token_valid, sync_outlook_events
from userapis.permissions import CalendarEventPermission
from userapis.serializers import CalendarEventSerializer
from userapis.views.calendar_room_warning import CalendarEventRoomWarningMixin


def _is_truthy_param(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class ProjectCalendarViewSet(CalendarEventRoomWarningMixin, viewsets.ModelViewSet):
    """
    API for managing project calendar events and aggregate project calendar data.
    """

    queryset = CalendarEvent.objects.filter(project__isnull=False)
    serializer_class = CalendarEventSerializer

    def get_permissions(self):
        if self.action in ["list", "retrieve"]:
            return [CalendarEventPermission("view", "project")]
        if self.action == "rooms":
            return [CalendarEventPermission("view", "project")]
        if self.action == "create":
            return [CalendarEventPermission("create", "project")]
        if self.action in ["update", "partial_update"]:
            return [CalendarEventPermission("update", "project")]
        if self.action == "destroy":
            return [CalendarEventPermission("delete", "project")]
        return super().get_permissions()

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["allow_project_scope"] = True
        return context

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return CalendarEvent.objects.none()

        queryset = (
            CalendarEvent.objects.filter(project__isnull=False)
            .prefetch_related("assignees", "documents", "object_assessments")
            .select_related("project", "company_profile", "freelancer", "created_by", "user")
            .order_by("start_date", "id")
        )

        permission = CalendarEventPermission("view", "project")
        project = self._get_project()

        if self.action == "list":
            if not project:
                return CalendarEvent.objects.none()
            queryset = permission.filter_queryset(queryset, user, {"project": project})

            if not _is_truthy_param(self.request.query_params.get("all")):
                start_date = parse_datetime(self.request.query_params.get("start_date"))
                end_date = parse_datetime(self.request.query_params.get("end_date"))
                if start_date and end_date:
                    queryset = queryset.filter(
                        start_date__lte=end_date,
                        end_date__gte=start_date,
                    )
            return queryset

        return queryset

    def get_object(self):
        obj = get_object_or_404(self.get_queryset(), pk=self.kwargs["pk"])
        self.check_object_permissions(self.request, obj)
        return obj

    def create(self, request, *args, **kwargs):
        project = self._get_project(from_body=True)
        if not project:
            return Response(
                {"project": ["Project is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._create_calendar_event_response(serializer)

    def partial_update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        return self._update_calendar_event_response(serializer)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=False)
        serializer.is_valid(raise_exception=True)
        return self._update_calendar_event_response(serializer)

    def _get_project(self, from_body=False):
        project_value = None
        if from_body:
            project_value = self.request.data.get("project")
        else:
            project_value = self.request.query_params.get("project")

        if not project_value:
            return None

        try:
            return Project.objects.get(id=project_value)
        except (Project.DoesNotExist, TypeError, ValueError):
            return None

    @action(detail=False, methods=["get"], url_path="rooms")
    def rooms(self, request):
        user = request.user

        if isinstance(user, AnonymousUser):
            return Response(
                {"error": "Authentication required"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        project = self._get_project()
        if not project:
            return Response(
                {"error": "project parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        product_name = get_product_name_from_request(request)
        config = CompanyConfig.objects.filter(
            company_profile=project.company_profile,
            solution=product_name,
        ).first()
        rooms = effective_rooms_list(config.rooms if config else [])

        return Response(
            {
                "project": project.id,
                "company_profile": project.company_profile_id,
                "company_name": (
                    project.company_profile.company_name
                    if project.company_profile_id and project.company_profile
                    else None
                ),
                "rooms": rooms,
            }
        )

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "project",
                openapi.IN_QUERY,
                description="Project ID to filter events",
                type=openapi.TYPE_INTEGER,
                required=True,
            ),
            openapi.Parameter(
                "start_date",
                openapi.IN_QUERY,
                description="Start date for filtering events (ISO 8601 format: YYYY-MM-DDThh:mm:ss)",
                type=openapi.TYPE_STRING,
                required=True,
            ),
            openapi.Parameter(
                "end_date",
                openapi.IN_QUERY,
                description="End date for filtering events (ISO 8601 format: YYYY-MM-DDThh:mm:ss)",
                type=openapi.TYPE_STRING,
                required=True,
            ),
            openapi.Parameter(
                "status",
                openapi.IN_QUERY,
                description="Optional status filter for phases, milestones, and action items. Prefix with '-' to exclude (e.g. '-closed').",
                type=openapi.TYPE_STRING,
                required=False,
            ),
        ]
    )
    @action(detail=False, methods=["get"], url_path="events")
    def events(self, request):
        """
        Get project calendar aggregate events with filtering options.
        """
        user = request.user

        if isinstance(user, AnonymousUser):
            return Response(
                {"error": "Authentication required"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        project_id = request.query_params.get("project")
        start_date = parse_datetime(request.query_params.get("start_date"))
        end_date = parse_datetime(request.query_params.get("end_date"))
        status_param = request.query_params.get("status")

        if not project_id:
            return Response(
                {"error": "project parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if start_date is None or end_date is None:
            return Response(
                {
                    "error": (
                        "start_date and end_date are required and must be in ISO format "
                        "(YYYY-MM-DDThh:mm:ss)"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            return Response(
                {"error": "Project not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        project_filter = Q(project=project)

        phase_permission = require_any_permission(
            user,
            "milestone_management",
            ["view", "full_access"],
            "project",
            {"project": project.id},
        )
        if check_for_freelancer_or_company_owner(user, project):
            projects_company_permission = require_any_permission(
                user,
                "projects",
                ["view_all_access", "full_access"],
                "company",
                request.query_params,
            )
        else:
            projects_company_permission = False

        if not phase_permission and not projects_company_permission:
            project_phases = ProjectPhase.objects.none()
        else:
            project_phases = ProjectPhase.objects.filter(
                project_filter & Q(date__lte=end_date) & Q(date__gte=start_date)
            )

        milestone_permission = require_any_permission(
            user,
            "phases_management",
            ["view", "full_access"],
            "project",
            {"project": project.id},
        )

        if not milestone_permission and not projects_company_permission:
            milestones = Milestone.objects.none()
        else:
            milestones = Milestone.objects.filter(
                project_filter & Q(start_date__lte=end_date) & Q(due_date__gte=start_date)
            ).distinct()

        action_item_permission = require_any_permission(
            user,
            "action_item",
            ["view", "full_access"],
            "project",
            {"project": project.id},
        )

        if not action_item_permission and not projects_company_permission:
            action_items = ActionItem.objects.none()
        else:
            action_items = (
                ActionItem.objects.filter(
                    Q(milestone__in=milestones)
                    | Q(parent_action_item__milestone__in=milestones)
                )
                .filter(Q(start_date__lte=end_date) & Q(target_date__gte=start_date))
            )
            is_admin = (
                not user.is_freelancer
                and user.groups.filter(name=UserType.ADMIN.value).exists()
            )
            if not is_admin:
                action_items = action_items.filter(Q(assignees=user)).distinct()

        calendar_permission = CalendarEventPermission("view", "project")
        calendar_events = calendar_permission.filter_queryset(
            CalendarEvent.objects.filter(project=project)
            .prefetch_related("assignees", "documents", "object_assessments")
            .select_related("project", "company_profile", "freelancer", "created_by", "user")
            .order_by("start_date", "id"),
            user,
            {"project": project},
        ).filter(
            start_date__lte=end_date,
            end_date__gte=start_date,
        )

        if status_param:
            is_negative = status_param.startswith("-")
            status_value = status_param[1:] if is_negative else status_param
            status_value = status_value.strip()

            if status_value:
                status_filter = {"status__iexact": status_value}
                if project_phases is not None:
                    project_phases = (
                        project_phases.exclude(**status_filter)
                        if is_negative
                        else project_phases.filter(**status_filter)
                    )
                if milestones is not None:
                    milestones = (
                        milestones.exclude(**status_filter)
                        if is_negative
                        else milestones.filter(**status_filter)
                    )
                if action_items is not None:
                    action_items = (
                        action_items.exclude(**status_filter)
                        if is_negative
                        else action_items.filter(**status_filter)
                    )

        return Response(
            {
                "calendar_events": CalendarEventSerializer(
                    calendar_events,
                    many=True,
                    context=self.get_serializer_context(),
                ).data,
                "milestones": MilestoneSerializer(
                    milestones,
                    many=True,
                    context={"request": request},
                ).data,
                "action_items": ActionItemSerializer(action_items, many=True).data,
                "project_phases": ProjectPhaseSerializer(
                    project_phases,
                    many=True,
                ).data,
            }
        )

    @action(detail=False, methods=["post"], url_path="sync-calendar")
    def sync_calendar(self, request):
        user = request.user
        project = self._get_project(from_body=True) or self._get_project()

        if not project:
            return Response(
                {"project": ["Project is required."]},
                status=status.HTTP_400_BAD_REQUEST,
            )

        permission = CalendarEventPermission("create", "project")
        context = {"project": project}
        if not (
            permission._check_create_permission(user, context)
            or permission._check_view_permission(user, None, context)
        ):
            return Response(
                {"error": "You do not have permission to sync this project calendar."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not is_outlook_token_valid(user):
            return Response(
                {
                    "error": (
                        "Invalid or expired Outlook token. Please reconnect your Outlook account."
                    )
                },
                status=status.HTTP_401_UNAUTHORIZED,
            )

        access_token = user.outlook_credentials.outlook_access_token
        sync_result = sync_outlook_events(user, access_token, project=project)

        if not sync_result.get("success"):
            return Response(sync_result.get("error"), status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "message": sync_result.get("message"),
                "synced_events_count": sync_result.get("synced_events_count", 0),
            },
            status=status.HTTP_200_OK,
        )
