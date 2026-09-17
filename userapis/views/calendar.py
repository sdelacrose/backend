from django.db.models import Q
from django.contrib.auth.models import AnonymousUser
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime


from rest_framework import viewsets, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from userapis.models import CalendarEvent
from userapis.serializers import CalendarEventSerializer
from project_management.models import Milestone, ActionItem
from human_resource_management.models import LeaveRequest
from human_resource_management.serializers import LeaveRequestSerializer
from ecpmpapp.utils import reschedule_event
from ecpmpapp.constants import UserType
from userapis.permissions import CalendarEventPermission
from userapis.views.calendar_room_warning import CalendarEventRoomWarningMixin
from ecpmpapp.utils import require_any_permission_multiple_modules


def _is_truthy_param(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class CalendarEventViewSet(CalendarEventRoomWarningMixin, viewsets.ModelViewSet):
    """
    API for managing calendar events.
    """
    queryset = CalendarEvent.objects.all()
    serializer_class = CalendarEventSerializer

    
    def get_permissions(self):
        if self.action == 'list':
            return [CalendarEventPermission('view', 'company')]
        elif self.action == 'retrieve':
            return [CalendarEventPermission('view', 'company')]
        elif self.action == 'create':
            return [CalendarEventPermission('create', 'company')]
        elif self.action in ['update', 'partial_update']:
            return [CalendarEventPermission('update', 'company')]
        elif self.action == 'destroy':
            return [CalendarEventPermission('delete', 'company')]
        return super().get_permissions()
    
    def get_object(self):
        obj = get_object_or_404(CalendarEvent, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj

    def get_queryset(self):
        """Filter calendar events based on user permissions"""
        request_user = self.request.user
        if isinstance(request_user, AnonymousUser):
            return CalendarEvent.objects.none()
        permission = CalendarEventPermission('view', 'company')
        queryset = permission.filter_queryset(
            CalendarEvent.objects.all(),
            request_user,
            {},
        )

        if _is_truthy_param(self.request.query_params.get("personal")):
            if request_user.is_freelancer:
                queryset = queryset.filter(freelancer=request_user)
            else:
                queryset = queryset.filter(
                    Q(created_by=request_user) | Q(assignees__in=[request_user]) | Q(user=request_user)
                ).distinct()

        return queryset

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "all",
                openapi.IN_QUERY,
                description="When true, return all accessible calendar events without requiring a date range. Intended for picker/dropdown flows.",
                type=openapi.TYPE_BOOLEAN,
                required=False,
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
        ]
    )
    def list(self, request, *args, **kwargs):
        from project_management.serializers import MilestoneSerializer, ActionItemSerializer

        user = request.user
        personal_only = _is_truthy_param(request.query_params.get("personal"))
        fetch_all_events = _is_truthy_param(request.query_params.get("all"))

        if fetch_all_events:
            calendar_events = self.get_queryset().order_by("start_date")
            return Response({
                "calendar_events": self.get_serializer(calendar_events, many=True).data,
                "action_items": [],
                "milestones": [],
                "leave_requests": [],
            })

        # Get start_date and end_date from query params
        start_date = parse_datetime(request.query_params.get("start_date"))
        end_date = parse_datetime(request.query_params.get("end_date"))

        if start_date is None or end_date is None:
            return Response({"error": "start_date and end_date are required and must be in ISO format (YYYY-MM-DDThh:mm:ss)"}, 
                          status=status.HTTP_400_BAD_REQUEST)

        # Fetch Calendar Events
        calendar_events = self.get_queryset().filter(start_date__lte=end_date, end_date__gte=start_date).order_by('start_date')

        # Check Action Item permissions
        has_action_item_full_access = require_any_permission_multiple_modules(
            user, 
            {'action_item': ['full_access']}, 
            'company', 
            self.request.query_params
        )
        has_action_item_view = require_any_permission_multiple_modules(
            user, 
            {'action_item': ['view']}, 
            'company', 
            self.request.query_params
        )
        has_action_item_view_assigned_created = require_any_permission_multiple_modules(
            user, 
            {'action_item': ['view_assigned_created']}, 
            'company', 
            self.request.query_params
        )
        
        print(f'[DEBUG] Calendar: Action Item permissions - full_access: {has_action_item_full_access}, view: {has_action_item_view}, view_assigned_created: {has_action_item_view_assigned_created}')

        # Fetch Action Items based on permissions
        action_items = ActionItem.objects.none()
        if has_action_item_full_access or has_action_item_view or has_action_item_view_assigned_created:
            # Base filter for date range
            date_filter = Q(start_date__lte=end_date) & Q(target_date__gte=start_date)
            
            if has_action_item_full_access or has_action_item_view:
                # Users with full_access or view permissions see all company action items
                if user.is_freelancer:
                    # Freelancers see their own action items
                    action_items = ActionItem.objects.filter(
                        date_filter & Q(assignee=user)
                    )
                else:
                    # Company users see all company action items
                    action_items = ActionItem.objects.filter(
                        date_filter & Q(company_profile=user.company)
                    )
            elif has_action_item_view_assigned_created:
                # Users with view_assigned_created permissions see only their own action items
                if user.is_freelancer:
                    action_items = ActionItem.objects.filter(
                        date_filter & Q(assignee=user)
                    )
                else:
                    action_items = ActionItem.objects.filter(
                        date_filter & Q(company_profile=user.company)
                    ).filter(
                        Q(assignee=user) | Q(created_by=user)
                    ).distinct()

            if personal_only:
                action_items = action_items.filter(
                    Q(assignee=user) | Q(created_by=user)
                ).distinct()

        # Check Milestone permissions
        has_milestone_full_access = require_any_permission_multiple_modules(
            user, 
            {'milestone_management': ['full_access']}, 
            'company', 
            self.request.query_params
        )
        has_milestone_view = require_any_permission_multiple_modules(
            user, 
            {'milestone_management': ['view']}, 
            'company', 
            self.request.query_params
        )
        
        print(f'[DEBUG] Calendar: Milestone permissions - full_access: {has_milestone_full_access}, view: {has_milestone_view}')

        # Fetch Milestones based on permissions
        milestones = Milestone.objects.none()
        if (has_milestone_full_access or has_milestone_view) and not personal_only:
            # Base filter for date range
            date_filter = Q(start_date__lte=end_date) & Q(due_date__gte=start_date)
            
            if user.is_freelancer:
                # Freelancers see milestones from projects they're part of
                milestones = Milestone.objects.filter(
                    date_filter & Q(project__team_members__team_member=user)
                ).distinct()
            else:
                # Company users see all company milestones
                milestones = Milestone.objects.filter(
                    date_filter & Q(project__user__company=user.company)
                ).distinct()

        # Check Leave Request permissions
        has_leave_request_full_access = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['full_access']}, 
            'company', 
            self.request.query_params
        )
        has_leave_request_view = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['view']}, 
            'company', 
            self.request.query_params
        )
        has_leave_request_view_created = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['view_created']}, 
            'company', 
            self.request.query_params
        )
        
        print(f'[DEBUG] Calendar: Leave Request permissions - full_access: {has_leave_request_full_access}, view: {has_leave_request_view}, view_created: {has_leave_request_view_created}')

        # Fetch Leave Requests based on permissions
        leave_requests = LeaveRequest.objects.none()
        if has_leave_request_full_access or has_leave_request_view or has_leave_request_view_created:
            # Base filter for date range
            date_filter = Q(start_date__lte=end_date) & Q(end_date__gte=start_date)
            
            if has_leave_request_full_access or has_leave_request_view:
                if user.is_freelancer:
                    # Freelancers see their own leave requests
                    leave_requests = LeaveRequest.objects.filter(
                        date_filter & Q(employee__user=user)
                    )
                else:
                    # Company users see all company leave requests
                    leave_requests = LeaveRequest.objects.filter(
                        date_filter & Q(employee__user__company=user.company)
                    )
            elif has_leave_request_view_created:
                if user.is_freelancer:
                    # Freelancers see their own leave requests
                    leave_requests = LeaveRequest.objects.filter(
                        date_filter & Q(employee__user=user)
                    )
                else:
                    # Company users see leave requests where they are the employee
                    leave_requests = LeaveRequest.objects.filter(
                        date_filter & Q(employee__user=user)
                    )

            if personal_only:
                leave_requests = leave_requests.filter(employee__user=user)

        return Response({
            "calendar_events": self.get_serializer(calendar_events, many=True).data,
            "action_items": ActionItemSerializer(action_items, many=True).data,
            "milestones": MilestoneSerializer(milestones, many=True).data,
            "leave_requests": LeaveRequestSerializer(leave_requests, many=True, context={"request": request}).data,
        })

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._create_calendar_event_response(serializer)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        return self._update_calendar_event_response(serializer)

    def partial_update(self, request, *args, **kwargs):
        kwargs["partial"] = True
        return self.update(request, *args, **kwargs)
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        reschedule_event(instance, is_deleted=True)
        self.perform_destroy(instance)
        return Response(status=status.HTTP_204_NO_CONTENT)
