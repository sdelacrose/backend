from rest_framework import viewsets, filters, status
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.db import transaction, IntegrityError
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
from human_resource_management.services.leave_balance import (
    current_year_bounds,
    get_employee_leave_summary,
)
from models.models import Staff, StaffConfiguration, User
from project_management.models import ActionItem, Milestone
from project_management.models import Project
from human_resource_management.constants import EXCEL_MIME_TYPE
from human_resource_management.permissions import (
    StaffManagementPermission,
    StaffObservationPermission,
    _same_company,
    can_export_staff_compensation,
    can_provision_staff,
    has_cumulative_leave_review_permission,
    has_staff_create_permission,
)
from human_resource_management.services.staff_provisioning import provision_staff_for_user
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.constants import StatusType, UserType
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

        # L00-A1: StaffManagementPermission.has_permission only checks the
        # REQUESTER's own roles -- it never inspects `obj`, so without this
        # guard any user holding staff_management.view/update in their own
        # company could retrieve/update ANY Staff row in ANY OTHER company
        # by guessing its pk. Reuses the same company-scoping check already
        # applied to the compensation fields (permissions.py::_same_company).
        if not _same_company(self.request.user, obj):
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

    @action(detail=False, methods=['get'], url_path='eligible-for-provisioning')
    def eligible_for_provisioning(self, request):
        """L01-B1: users already attached to the requester's OWN company who
        do not yet have a Staff dossier -- the only population this action
        (and POST .../provision/ below) may ever act on. Never a
        cross-company listing, never includes any compensation field."""
        if not has_staff_create_permission(request.user):
            raise PermissionDenied("You do not have permission to create a staff dossier.")
        company_id = getattr(request.user, "company_id", None)
        if not company_id:
            return Response([])
        users = User.objects.filter(
            company_id=company_id, staff__isnull=True, is_deleted=False, is_active=True,
        ).order_by("first_name", "last_name")
        return Response([{"id": u.id, "name": u.full_name, "email": u.email} for u in users])

    @action(detail=False, methods=['post'], url_path='provision')
    def provision(self, request):
        """L01-B1: the one real-time Staff-creation path -- a targeted
        action, never a reopening of create() above (still 405 for any
        other caller). Accepts ONLY `user` in the body; any other field is
        rejected outright -- compensation, roles, licenses and company
        assignment are never set through this action (those remain the
        domain of StaffCompensationHistory/UserRoleViewSet/the external
        admin-server, each with its own, separate controls)."""
        extra_fields = set(request.data.keys()) - {"user"}
        if extra_fields:
            raise ValidationError({"non_field_errors": [
                f"Unsupported field(s): {', '.join(sorted(extra_fields))}. "
                "Only 'user' is accepted here -- compensation, roles, "
                "licenses and company assignment are never set through "
                "this action."
            ]})
        user_id = request.data.get("user")
        if not user_id:
            raise ValidationError({"user": "This field is required."})
        target_user = get_object_or_404(User, pk=user_id)

        if not can_provision_staff(request.user, target_user):
            # L01-A §4: a deliberately generic refusal -- never confirms
            # whether the target belongs to another company, already has a
            # dossier, or doesn't exist at all.
            raise PermissionDenied("This user is not eligible for a staff dossier to be created here.")

        if hasattr(target_user, "staff"):
            # L01-B1 idempotence: a retried/duplicate call is a SUCCESS, not
            # an error -- never a second dossier, never a reset of the one
            # that already exists.
            return Response(
                StaffSerializer(target_user.staff, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        try:
            with transaction.atomic():
                staff = provision_staff_for_user(target_user)
                staff.save()
        except IntegrityError:
            # Lost a race against a concurrent call provisioning the SAME
            # user -- the other call won; that is exactly as successful an
            # outcome for this one (still exactly one Staff row, never two).
            target_user.refresh_from_db()
            return Response(
                StaffSerializer(target_user.staff, context={"request": request}).data,
                status=status.HTTP_200_OK,
            )

        return Response(
            StaffSerializer(staff, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )

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

        # LOT RH 8 closure: this export already carries leave/WFH-balance
        # data (via LOT RH 2's get_employee_leave_summary, below). Holding
        # `staff_management` view/full_access gives a Company Manager
        # company-wide ROWS here, but that permission was never meant to
        # also expose everyone's leave figures -- the dedicated Leaves
        # export/recap are explicitly reserved to `leave_request.review`
        # holders (LeaveReportingPermission). Closing that bypass here
        # rather than gating this whole action, so every other Staff-export
        # column a manager is already entitled to stays exactly as before.
        has_leave_review = has_cumulative_leave_review_permission(request_user)

        # L00-A (DEC-49): export is its own action -- staff_management.view/
        # full_access (checked above for the export's company-wide ROWS)
        # never by itself implies the right to include the Salary Amount
        # COLUMN in the generated file. A mixed export simply omits this one
        # column for an unauthorized exporter -- every other column these
        # permissions already entitled them to stays exactly as before.
        can_export_compensation = can_export_staff_compensation(request_user, request_user.company)

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
        ]
        if can_export_compensation:
            header.append('Salary Amount')
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

            if start_date and end_date:
                projects = projects.filter(project_start_date__lte=end_date, project_end_date__gte=start_date)
                action_items = action_items.filter(start_date__lte=end_date, target_date__gte=start_date)
                sub_action_items = sub_action_items.filter(start_date__lte=end_date, target_date__gte=start_date)
                user_time_cards = user_time_cards.filter(created_at__range=[start_date, end_date])
                user_expense_cards = user_expense_cards.filter(created_at__range=[start_date, end_date])

            # Total allocated hours (action items only)
            total_allocated_hours = action_items.filter(parent_action_item__isnull=True).aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
            # Total spent hours (sum of time card hours)
            total_spent_hours = user_time_cards.aggregate(total_hours=Sum('hours'))['total_hours'] or 0
            # Productivity Percentage (total allocated hours / total spent hours * 100)
            productivity_percentage = 0 if total_spent_hours == 0 else round((total_allocated_hours / total_spent_hours) * 100, 2)
            # Total allocated and spent expense
            total_allocated_expense = action_items.filter(parent_action_item__isnull=True).aggregate(total_expense=Sum('allocated_budget'))['total_expense'] or 0
            total_spent_expense = user_expense_cards.aggregate(total_expense=Sum('total'))['total_expense'] or 0

            # LOT RH 2: centralized via human_resource_management.services.leave_balance --
            # "Remaining" always reflects the current-calendar-year balance (same period
            # the Staff API and leave-request submission check use: this export previously
            # computed it from ALL-TIME approved leave regardless of the date filter, which
            # disagreed with the Staff API; that inconsistency is what this centralization
            # fixes, not a newly invented rule). "Taken" reflects the requested reporting
            # window when one is given, else that same current year.
            #
            # LOT RH 8 closure: these four figures are someone else's leave
            # data -- only ever computed/shown for the requester's OWN row
            # (always allowed, same as "My Leaves") or, for any other
            # employee's row, only when the requester also holds the real
            # `leave_request.review` entitlement. A Company Manager with
            # staff_management access but no leave review right gets every
            # other column populated as before, these four blank.
            is_own_row = staff.user_id == request_user.id
            if has_leave_review or is_own_row:
                year_start, year_end = current_year_bounds()
                annual_summary = get_employee_leave_summary(staff, year_start, year_end)
                if start_date and end_date:
                    window_summary = get_employee_leave_summary(staff, start_date, end_date)
                    total_leaves_taken = int(window_summary.leave_taken)
                    total_work_from_home_taken = int(window_summary.wfh_taken)
                else:
                    total_leaves_taken = int(annual_summary.leave_taken)
                    total_work_from_home_taken = int(annual_summary.wfh_taken)
                remaining_leaves = (
                    int(annual_summary.leave_balance)
                    if annual_summary.leave_balance is not None
                    else getattr(staff, 'annual_leaves_allowed', 0)
                )
                remaining_work_from_home = (
                    int(annual_summary.wfh_balance)
                    if annual_summary.wfh_balance is not None
                    else getattr(staff, 'wfh_leaves_allowed', 0)
                )
            else:
                total_leaves_taken = ''
                total_work_from_home_taken = ''
                remaining_leaves = ''
                remaining_work_from_home = ''
            weekly_working_hours = getattr(staff, 'weekly_hours', 0)

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
            ]
            if can_export_compensation:
                row_data.append(getattr(staff, 'salary', 0))
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
            # The project must belong to the requester (their company, or their own for a freelancer):
            # another owner's project answers exactly like an unknown one, before any data is read.
            if request_user.is_freelancer:
                own_projects = Project.objects.filter(user=request_user)
            elif request_user.company_id:
                own_projects = Project.objects.filter(company_profile_id=request_user.company_id)
            else:
                own_projects = Project.objects.none()
            project = get_object_or_404(own_projects, pk=project_id)
            
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
