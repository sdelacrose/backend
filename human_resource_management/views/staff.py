from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Sum, F, ExpressionWrapper, FloatField, IntegerField, Case, When
from datetime import date
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import MethodNotAllowed
from django.http import HttpResponse

from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.dateparse import parse_date
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
import openpyxl
from openpyxl.styles import Font
import csv

from human_resource_management.serializers import StaffSerializer, StaffObservationSerializer, StaffConfigurationSerializer
from human_resource_management.models import StaffObservation
from human_resource_management.utils import calculate_leave_days
from models.models import Staff, StaffConfiguration
from project_management.models import ActionItem, Milestone
from project_management.models import Project
from human_resource_management.constants import EXCEL_MIME_TYPE
from human_resource_management.permissions import StaffManagementPermission, StaffObservationPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.constants import LeaveType, StatusType, UserType
from ecpmpapp.ordering import TableOrderingFilter
from django.db.models.functions import Concat
from django.db.models import Value, CharField

class StaffViewSet(viewsets.ModelViewSet):
    serializer_class = StaffSerializer
    filter_backends = [DjangoFilterBackend, TableOrderingFilter]
    ordering_fields = [
        'name',
        'employment_status',
        'weekly_hours',
        'salary',
        'hourly_rate'
    ]
    ordering_text_fields = {"name", "employment_status"}
    ordering_natural_fields = {"name"}
   
    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [StaffManagementPermission(action='view', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [StaffManagementPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [StaffManagementPermission(action='delete', scope='company')]
        return super().get_permissions()

    def get_object(self):
        obj = get_object_or_404(Staff, pk=self.kwargs['pk'])
        
        # Allow users to view their own staff profile even without staff management permissions
        if obj.user == self.request.user:
            return obj
        
        # For other staff profiles, check staff management permissions
        permission = StaffManagementPermission(action='view', scope='company')
        if not permission.has_permission(self.request, self):
            raise PermissionDenied("You do not have permission to access this staff profile.")
        return obj

    def get_queryset(self):
        """Filter staff based on user permissions and annotate calculated fields for ordering."""
        request_user = self.request.user
        if isinstance(request_user, AnonymousUser):
            return Staff.objects.none()

        has_staff_management_full_access = require_any_permission_multiple_modules(
            request_user, 
            {'staff_management': ['full_access']}, 
            'company', 
            self.request.query_params
        )

        has_staff_management_view = require_any_permission_multiple_modules(
            request_user, 
            {'staff_management': ['view']}, 
            'company', 
            self.request.query_params
        )

        has_staff_management_update = require_any_permission_multiple_modules(
            request_user, 
            {'staff_management': ['update', 'default_management_update']}, 
            'company', 
            self.request.query_params
        )

        staff_profiles = Staff.objects.filter(user__is_deleted=False)
        
        # Users with full access or view permissions see all staff in their company
        if has_staff_management_full_access or has_staff_management_view:
            if request_user.is_freelancer:
                # Freelancers see only their own staff profile
                queryset = staff_profiles.filter(user=request_user)
            else:
                queryset = staff_profiles.filter(user__company=request_user.company)
        # Users with update permissions see staff they can manage (treated as admin)
        elif has_staff_management_update:
            if request_user.is_freelancer:
                # Freelancers can only manage their own staff profile
                queryset = staff_profiles.filter(user=request_user)
            else:
                queryset = staff_profiles.filter(user__company=request_user.company)
        else:
            queryset = staff_profiles.filter(user=request_user)

        queryset = queryset.annotate(
            name=Concat('user__first_name', Value(' '), 'user__last_name', output_field=CharField()),
            employment_status=Case(
                When(user__user_type='freelancer', then=Value('Freelancer')),
                default=Value('Company User'),
                output_field=CharField()
            )
        )

        return queryset

    # Disallow staff creation
    def create(self, request, *args, **kwargs):
        """
        Disable the ability to create staff profile.
        """
        return Response({"error": "Staff creation is not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)
    
    @swagger_auto_schema(
    manual_parameters=[
        openapi.Parameter(
            name="start_date",
            in_=openapi.IN_QUERY,
            type=openapi.TYPE_STRING,
            format=openapi.FORMAT_DATE,
            description="Filter records starting from this date (format: YYYY-MM-DD).",
            required=False,
            example="2024-01-01"
        ),
        openapi.Parameter(
            name="end_date",
            in_=openapi.IN_QUERY,
            type=openapi.TYPE_STRING,
            format=openapi.FORMAT_DATE,
            description="Filter records up to this date (format: YYYY-MM-DD).",
            required=False,
            example="2024-01-31"
        ),
    ]
    )
    @action(detail=False, methods=['get'], url_path='export_staff')
    def export_staff_to_csv(self, request):
        """
        Export all staff profiles to CSV.
        """
        start_date = request.GET.get('start_date')
        end_date = request.GET.get('end_date')
        start_date = parse_date(start_date) if start_date else None
        end_date = parse_date(end_date) if end_date else None
        today = date.today()
        if start_date and start_date > today:
            raise ValidationError("Start date cannot be in the future.")
        if end_date and end_date > today:
            raise ValidationError("End date cannot be in the future.")

        request_user = self.request.user
        
        # Check if user has staff management permissions
        has_staff_management_permissions = require_any_permission_multiple_modules(
            request_user, 
            {'staff_management': ['view', 'full_access']}, 
            'company', 
            self.request.query_params
        )

        staff_profiles = Staff.objects.filter(user__is_deleted=False)
        
        if has_staff_management_permissions:
            staff_profiles = staff_profiles.filter(user__company=request_user.company)
        else:
            staff_profiles = staff_profiles.filter(user=request_user)
        
        header = [
            'Staff Name', 'Total Projects', 'Total Assigned Action Items',
            'Total Assigned Sub Action Items', 'Total Allocated Hours',
            'Total Spent Hours', 'Total Allocated Expense', 'Total Spent Expense',
            'Productivity Percentage', 'Total Leaves Taken', 'Total Work From Home Taken',
            'Remaining Leaves', 'Remaining Work From Home', 'Weekly Working Hours', 
            'Salary Amount'
        ]
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "Staff Profiles"
        bold_font = Font(bold=True)

        for col_num, column_title in enumerate(header, start=1):
            cell = sheet.cell(row=1, column=col_num, value=column_title)
            cell.font = bold_font

        row = 2
        for staff in staff_profiles:
            user = staff.user
            projects = user.user_projects.all()
            action_items = user.multi_assigned_action_items.all()
            sub_action_items = ActionItem.objects.filter(
                parent_action_item__in=action_items,
                assignees=user,
            ).distinct()
            user_time_cards = user.created_time_cards.filter(status=StatusType.APPROVED.value)
            user_expense_cards = user.created_expense_cards.filter(status=StatusType.APPROVED.value)
            user_leaves = user.staff.leave_requests.all()
            all_user_leaves = user.staff.leave_requests.all()

            if start_date and end_date:
                projects = projects.filter(project_start_date__lte=end_date, project_end_date__gte=start_date)
                action_items = action_items.filter(start_date__lte=end_date, target_date__gte=start_date)
                sub_action_items = sub_action_items.filter(start_date__lte=end_date, target_date__gte=start_date)
                user_time_cards = user_time_cards.filter(created_at__range=[start_date, end_date])
                user_expense_cards = user_expense_cards.filter(created_at__range=[start_date, end_date])
                user_leaves = user_leaves.filter(start_date__lte=end_date, end_date__gte=start_date)

            # Total allocated hours (action items only)
            total_allocated_hours = action_items.filter(parent_action_item__isnull=True).aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
            # Total spent hours (sum of time card hours)
            total_spent_hours = user_time_cards.aggregate(total_hours=Sum('hours'))['total_hours'] or 0
            # Productivity Percentage (total allocated hours / total spent hours * 100)
            productivity_percentage = 0 if total_spent_hours == 0 else round((total_allocated_hours / total_spent_hours) * 100, 2)
            # Total allocated and spent expense
            total_allocated_expense = action_items.filter(parent_action_item__isnull=True).aggregate(total_expense=Sum('allocated_budget'))['total_expense'] or 0
            total_spent_expense = user_expense_cards.aggregate(total_expense=Sum('total'))['total_expense'] or 0

            # Check all approved non-WFH leave requests, and ignore those with leave type "sick"
            regular_leaves = user_leaves.filter(
                status=StatusType.APPROVED.value,
                is_working_from_home=False
            ).exclude(type__in=[LeaveType.SICK.value, LeaveType.UNPAID.value])
            regular_leaves_2 = all_user_leaves.filter(
                status=StatusType.APPROVED.value,
                is_working_from_home=False
            ).exclude(type__in=[LeaveType.SICK.value, LeaveType.UNPAID.value])
            total_leaves_taken = sum(
                calculate_leave_days(leave.start_date, leave.end_date)
                for leave in regular_leaves
            )
            total_leaves_taken_2 = sum(
                calculate_leave_days(leave.start_date, leave.end_date)
                for leave in regular_leaves_2
            )
            if start_date and end_date:
                remaining_leaves = getattr(staff, 'annual_leaves_allowed', 0) - total_leaves_taken_2
            else:
                remaining_leaves = getattr(staff, 'annual_leaves_allowed', 0) - total_leaves_taken
            # Total Work From Home Taken and Remaining Work From Home
            wfh_leaves = user_leaves.filter(
                status=StatusType.APPROVED.value,
                is_working_from_home=True
            )
            wfh_leaves_2 = all_user_leaves.filter(
                status=StatusType.APPROVED.value,
                is_working_from_home=True
            )
            total_work_from_home_taken = sum(
                calculate_leave_days(leave.start_date, leave.end_date)
                for leave in wfh_leaves
            )
            total_work_from_home_taken_2 = sum(
                calculate_leave_days(leave.start_date, leave.end_date)
                for leave in wfh_leaves_2
            )
            if start_date and end_date:
                remaining_work_from_home = getattr(staff, 'wfh_leaves_allowed', 0) - total_work_from_home_taken_2
            else:
                remaining_work_from_home = getattr(staff, 'wfh_leaves_allowed', 0) - total_work_from_home_taken
            weekly_working_hours = getattr(staff, 'weekly_hours', 0)
            salary_amount = getattr(staff, 'salary', 0)

            row_data = [
                user.full_name,
                projects.count(),
                action_items.count(),
                sub_action_items.count(),
                total_allocated_hours,
                total_spent_hours,
                total_allocated_expense,
                total_spent_expense,
                productivity_percentage,
                total_leaves_taken,
                total_work_from_home_taken,
                remaining_leaves,
                remaining_work_from_home,
                weekly_working_hours,
                salary_amount,
            ]
            for col_num, cell_value in enumerate(row_data, start=1):
                sheet.cell(row=row, column=col_num, value=cell_value)
            row += 1

        for col_num, column_title in enumerate(header, start=1):
            sheet.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = 20

        response = HttpResponse(content_type=EXCEL_MIME_TYPE)
        response['Content-Disposition'] = 'attachment; filename="staff_profile_stats.xlsx"'
        workbook.save(response)
        return response
    @action(detail=False, methods=['get'], url_path='export_staff_project_kpis')
    def export_staff_project_kpis(self, request):
        """
        Export staff project related kpis to file
        """
        project_id = request.query_params.get('project_id')
        if not project_id:
            return Response({'error': 'project_id is required query param'}, status=400)
        project = None
        request_user = request.user
        try:
            project = get_object_or_404(Project, pk=project_id)
            
            # Check if user has staff management permissions (treated as admin)
            has_staff_management_permissions = require_any_permission_multiple_modules(
                request_user, 
                {'staff_management': ['view', 'full_access']}, 
                'company', 
                self.request.query_params
            )
            
            if not has_staff_management_permissions:
                raise PermissionDenied("You do not have permission to export staff project KPIs.")
                
            project_team_members = project.team_members.all()
            project_milestones = Milestone.objects.filter(project=project)
            project_action_items = ActionItem.objects.filter(Q(milestone__project=project) | Q(parent_action_item__milestone__project=project))
            staff_profiles = Staff.objects.filter(user__in=project_team_members.values_list('team_member', flat=True))
            header = [
                        'Staff Name', 'Total Assigned Action Items',
                        'Total Assigned Sub Action Items', 'Total Allocated Hours',
                        'Total Spent Hours', 'Total Allocated Expense', 'Total Spent Expense',
                        'Productivity Percentage'
            ]
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "Staff Project KPIs"
            bold_font = Font(bold=True)

            for col_num, column_title in enumerate(header, start=1):
                cell = sheet.cell(row=1, column=col_num, value=column_title)
                cell.font = bold_font

            row = 2
            for staff in staff_profiles:
                user = staff.user
                action_items = project_action_items.filter(assignees=user).distinct()
                sub_action_items = action_items.filter(parent_action_item__isnull=False)
                user_time_cards = user.created_time_cards.filter(
                    Q(milestone__in=project_milestones) | Q(action_item__in=project_action_items),
                    status=StatusType.APPROVED.value
                    ).distinct()
                user_expense_cards = user.created_expense_cards.filter(
                    Q(milestone__in=project_milestones) | Q(action_item__in=project_action_items),
                    status=StatusType.APPROVED.value
                    ).distinct()


                # Total allocated hours (action items only)
                total_allocated_hours = action_items.filter(parent_action_item__isnull=True).aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
                # Total spent hours (sum of time card hours)
                total_spent_hours = user_time_cards.aggregate(total_hours=Sum('hours'))['total_hours'] or 0
                # Productivity Percentage (total allocated hours / total spent hours * 100)
                productivity_percentage = 0 if total_spent_hours == 0 else round((total_allocated_hours / total_spent_hours) * 100, 2)
                # Total allocated and spent expense
                total_allocated_expense = action_items.filter(parent_action_item__isnull=True).aggregate(total_expense=Sum('allocated_budget'))['total_expense'] or 0
                total_spent_expense = user_expense_cards.aggregate(total_expense=Sum('total'))['total_expense'] or 0


                row_data = [
                    user.full_name,
                    action_items.count(),
                    sub_action_items.count(),
                    total_allocated_hours,
                    total_spent_hours,
                    total_allocated_expense,
                    total_spent_expense,
                    productivity_percentage
                ]
                for col_num, cell_value in enumerate(row_data, start=1):
                    sheet.cell(row=row, column=col_num, value=cell_value)
                row += 1

            for col_num, column_title in enumerate(header, start=1):
                sheet.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = 20

            response = HttpResponse(content_type=EXCEL_MIME_TYPE)
            response['Content-Disposition'] = 'attachment; filename="staff_project_stats.xlsx"'
            workbook.save(response)
            return response
                    
        except Project.DoesNotExist:
            return Response({'error': 'Invalid Project ID'}, status=404)
        
    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Deleting a staff profile is not allowed.")
    
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data

        ordering = request.query_params.get("ordering")
        if ordering:
            reverse = ordering.startswith("-")
            sort_by = ordering.lstrip("-")

            # Computed fields (must match serializer output keys)
            computed_fields = {
                "work_from_home_taken",
                "work_from_home_left",
                "leave_left",
                "leave_taken",
                "productivity_percentage",
                "location",
                "total_spent_hours",
                "total_allocated_hours",
            }
            # UI shows ∞ for company owner on these columns; treat as +∞ for sort.
            infinite_leave_fields = {
                "leave_taken",
                "leave_left",
                "work_from_home_taken",
                "work_from_home_left",
            }
            company = getattr(request.user, "company", None)
            company_owner_id = getattr(company, "admin_user_id", None) if company else None

            def sort_key(row):
                user_id = row.get("user")
                if (
                    sort_by in infinite_leave_fields
                    and company_owner_id is not None
                    and user_id is not None
                    and int(user_id) == int(company_owner_id)
                ):
                    # DESC (reverse=True) → ∞ first; ASC → ∞ last
                    return float("inf")

                value = row.get(sort_by)
                if value is None:
                    return 0
                if isinstance(value, (int, float)):
                    return value
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return value

            if sort_by in computed_fields:
                # Values rendered as "-" stay last for both directions. Sorting
                # a single API page in the frontend would break global ordering.
                available = [
                    row for row in data if row.get(sort_by) not in (None, "")
                ]
                missing = [
                    row for row in data if row.get(sort_by) in (None, "")
                ]
                data = sorted(available, key=sort_key, reverse=reverse) + missing

        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)
        
        

class StaffObservationViewSet(viewsets.ModelViewSet):
    queryset = StaffObservation.objects.all()
    serializer_class = StaffObservationSerializer
    filter_backends = [
        DjangoFilterBackend,
        TableOrderingFilter,
        filters.SearchFilter,
    ]
    ordering_fields = [
        'id',
        'company_profile',
        'title',
        'description',
        'staff',
        'staff__user__first_name',
        'staff__user__last_name',
        'staff_full_name',
        'created_by',
        'created_at',
        'updated_at',
    ]
    ordering_text_fields = {
        "title",
        "description",
        "staff_full_name",
    }
    ordering_natural_fields = {"title", "description", "staff_full_name"}
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [StaffObservationPermission(action='view', scope='company')]
        elif self.action == 'create':
            return [StaffObservationPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [StaffObservationPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [StaffObservationPermission(action='delete', scope='company')]
        return super().get_permissions()

    def get_queryset(self):
        request_user = self.request.user
        if isinstance(request_user, AnonymousUser):
            return StaffObservation.objects.none()

        has_staff_observation_full_access = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['full_access']}, 
            'company', 
            self.request.query_params
        )

        has_staff_observation_view = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['view']}, 
            'company', 
            self.request.query_params
        )

        has_staff_observation_view_created = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['view_created']}, 
            'company', 
            self.request.query_params
        )

        # Users with full access or view permissions see all staff observations in their company
        if has_staff_observation_full_access or has_staff_observation_view:
            queryset = StaffObservation.objects.filter(company_profile=request_user.company)
        # Users with view_created permissions see only their own observations
        elif has_staff_observation_view_created:
            queryset = StaffObservation.objects.filter(created_by=request_user)
        # Fallback to original logic for backward compatibility
        else:
            return StaffObservation.objects.none()

        queryset = queryset.annotate(
            staff_full_name=Concat(
                F('staff__user__first_name'),
                Value(' '),
                F('staff__user__last_name'),
                output_field=CharField()
            )
        )
        return queryset

    def get_object(self):
        obj = get_object_or_404(StaffObservation, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj


class StaffConfigurationViewSet(viewsets.ModelViewSet):
    serializer_class = StaffConfigurationSerializer
    queryset = StaffConfiguration.objects.all()
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter, filters.SearchFilter]
    ordering_fields = [
        'id',
        'company_profile',
        'freelancer',
        'number_of_hours',
        'number_of_leaves',
        'number_of_wfh_leaves',
        'hourly_rate',
        'base_salary',
        'created_at',
        'updated_at',
    ]
    ordering = ['-id']

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return StaffConfiguration.objects.none()

        if user.is_freelancer:
            return StaffConfiguration.objects.filter(freelancer=user).order_by('-id')
        
        # Check if user has staff management permissions (treated as admin)
        has_staff_management_permissions = require_any_permission_multiple_modules(
            user, 
            {'staff_management': ['view', 'full_access']}, 
            'company', 
            self.request.query_params
        )
        
        if has_staff_management_permissions:
            return StaffConfiguration.objects.filter(company_profile=user.company).order_by('-id')
        
        return StaffConfiguration.objects.none()

    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed("POST", detail="Creating a staff configuration is not allowed.")

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed("DELETE", detail="Deleting a staff configuration is not allowed.")

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        user = request.user

        # Check if user has staff management permissions (treated as admin)
        has_staff_management_permissions = require_any_permission_multiple_modules(
            user, 
            {'staff_management': ['full_access', 'default_management_update']}, 
            'company', 
            self.request.query_params
        )

        # Company configuration - only users with staff management permissions can update
        if instance.company_profile:
            if not has_staff_management_permissions or instance.company_profile != user.company:
                raise PermissionDenied("You do not have permission to update this configuration.")

        # Freelancer can only update their own configuration
        if instance.freelancer:
            if instance.freelancer != user:
                raise PermissionDenied("You can only update your own configuration.")
                
        return super().update(request, *args, **kwargs)

    def get_object(self):
        obj = get_object_or_404(StaffConfiguration, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj
