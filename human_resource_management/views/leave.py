from rest_framework import viewsets, filters, status
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
from django_filters import rest_framework as drf_filters
from rest_framework.response import Response
from rest_framework.decorators import action
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.utils.dateparse import parse_date
from project_management.filters import CharInFilter, NumberInFilter
from human_resource_management.serializers.leave import LeaveRequestSerializer
from human_resource_management.models import LeaveRequest
from models.models import Staff
from ecpmpapp.constants import StatusType
from human_resource_management.permissions import LeaveRequestPermission

from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter

from django.db.models.functions import Concat
from django.db.models import Value, CharField, F

#TODO: Automatically adds sick leave entries to the office calendar.
#TODO: Approved leave is displayed on the office calendar with color-coded status.
#TODO: Declined leave is removed from the calendar

class LeaveRequestFilter(django_filters.FilterSet):
    status = CharInFilter(field_name='status', lookup_expr='in')
    type = CharInFilter(field_name='type', lookup_expr='in')
    employee = NumberInFilter(field_name='employee', lookup_expr='in')
    employee_user = NumberInFilter(field_name='employee__user', lookup_expr='in')
    start_date = drf_filters.DateFromToRangeFilter(field_name='start_date', label='Start Date Range')
    end_date = drf_filters.DateFromToRangeFilter(field_name='end_date', label='End Date Range')
    date_from = drf_filters.DateFilter(field_name='start_date', lookup_expr='gte')
    date_to = drf_filters.DateFilter(field_name='end_date', lookup_expr='lte')

    class Meta:
        model = LeaveRequest
        fields = ["status", "type", "employee", "employee_user", "start_date", "end_date", "date_from", "date_to"]

class LeaveRequestViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveRequestSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, TableOrderingFilter]
    filterset_class = LeaveRequestFilter
    ordering_fields = [
        "id",
        "employee",
        "employee__user__email",
        "employee__user__first_name",
        "employee__user__last_name",
        "employee_full_name",
        "type",
        "start_date",
        "end_date",
        "is_working_from_home",
        "status",
        "reason",
        "created_at",
    ]
    ordering = ["-id"]
    ordering_text_fields = {"employee_full_name", "type", "status", "reason"}
    ordering_natural_fields = {"employee_full_name", "reason"}

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'calendar']:
            return [LeaveRequestPermission(action='view', scope='company')]
        elif self.action == 'create':
            return [LeaveRequestPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [LeaveRequestPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [LeaveRequestPermission(action='delete', scope='company')]
        return super().get_permissions()

    def get_queryset(self):
        """
        Filter leave requests based on user permissions.
        The permission class handles the permission checks, this method handles the data filtering.
        """
        
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return LeaveRequest.objects.none()

        is_review_queue = str(
            self.request.query_params.get('review_queue', '')
        ).lower() in {'true', '1'}

        if is_review_queue:
            has_leave_request_review = require_any_permission_multiple_modules(
                user,
                {'leave_request': ['review']},
                'company',
                self.request.query_params,
            )
            if (
                not has_leave_request_review
                or user.is_freelancer
                or not user.company_id
            ):
                return LeaveRequest.objects.none()

            queryset = LeaveRequest.objects.filter(
                employee__user__company_id=user.company_id,
            )
            return self._annotate_employee_name(queryset.distinct())

        # Check permissions using the same logic as the permission class
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

        has_leave_request_review = require_any_permission_multiple_modules(
            user,
            {'leave_request': ['review']},
            'company',
            self.request.query_params,
        )

        query_filters = Q()
        # Build queryset based on permissions
        if (
            has_leave_request_full_access
            or has_leave_request_view
            or has_leave_request_review
        ):
            # Users with full_access or view permissions see all leave requests
            if user.is_freelancer:
                # For freelancers, we need to get their staff profile
                try:
                    staff_profile = user.staff
                    query_filters = Q(employee=staff_profile)
                except Staff.DoesNotExist:
                    return LeaveRequest.objects.none()
            else:
                query_filters = Q(employee__user__company=user.company)
        elif has_leave_request_view_created:
            # Users with view_created permissions see only their own leave requests
            staff_profile = user.staff
            query_filters = Q(employee=staff_profile)
        else:
            # No permissions, return empty queryset
            return LeaveRequest.objects.none()

        queryset = LeaveRequest.objects.filter(query_filters).distinct()

        return self._annotate_employee_name(queryset)

    @staticmethod
    def _annotate_employee_name(queryset):
        return queryset.annotate(
            employee_full_name=Concat(
                F('employee__user__first_name'),
                Value(' '),
                F('employee__user__last_name'),
                output_field=CharField()
            )
        )

    @action(detail=False, methods=["get"], url_path="calendar")
    def calendar(self, request):
        range_start_value = request.query_params.get("range_start")
        range_end_value = request.query_params.get("range_end")
        range_start = parse_date(range_start_value or "")
        range_end = parse_date(range_end_value or "")
        if not range_start or not range_end:
            return Response(
                {"range": "Valid range_start and range_end dates are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if range_end <= range_start or (range_end - range_start).days > 62:
            return Response(
                {"range": "The date range must be positive and at most 62 days."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = (
            self.get_queryset()
            .exclude(status__iexact="rejected")
            .filter(start_date__lt=range_end, end_date__gte=range_start)
            .select_related("employee__user")
            .order_by("start_date", "id")
        )
        return Response(
            [
                {
                    "id": leave.id,
                    "type": leave.type,
                    "status": leave.status,
                    "start_date": leave.start_date,
                    "end_date": leave.end_date,
                    "employee": {
                        "id": leave.employee_id,
                        "name": leave.employee.user.full_name,
                    },
                }
                for leave in queryset
            ]
        )

    def get_object(self):
        obj = get_object_or_404(LeaveRequest, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != StatusType.PENDING.value:
            raise PermissionDenied("Only pending leave requests can be deleted.")
        if instance.employee != request.user.staff:
            raise PermissionDenied("You are not allowed to delete this leave request.")
        instance.delete()
        return Response({"detail": "Leave request deleted successfully."}, status=status.HTTP_204_NO_CONTENT)
