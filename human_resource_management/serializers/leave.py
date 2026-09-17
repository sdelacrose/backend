from datetime import datetime, timedelta
from rest_framework import serializers
from django.db import transaction
from human_resource_management.models import LeaveRequest
from .staff import StaffSerializer
from human_resource_management.constants import START_YEAR, END_YEAR
from human_resource_management.utils import (
    calculate_leave_days,
    is_weekend,
)
from project_management.serializers.time_expense import get_valid_choices
from ecpmpapp.constants import StatusType, LeaveType
from ecpmpapp.utils import require_any_permission_multiple_modules
from models.models import UserRole

class LeaveRequestSerializer(serializers.ModelSerializer):
    employee = StaffSerializer(read_only=True)

    class Meta:
        model = LeaveRequest
        fields = ["id", "type", "start_date", "end_date", "status", "employee", "reason",
                  "is_working_from_home"]
        extra_kwargs = {
            'type': {'required': False, 'allow_null': True}
        }

    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user
        instance = self.instance

        # Determine employee from the request or instance.
        employee = user.staff if not instance else instance.employee

        # Retrieve the is_working_from_home flag.
        is_working_from_home = attrs.get('is_working_from_home', instance.is_working_from_home if instance else False)

        # Retrieve leave type if provided (it is now optional).
        leave_type = attrs.get('type', instance.type if instance else None)

        # Validate leave type only if provided.
        if not is_working_from_home and leave_type is not None:
            request = self.context.get('request')
            request_user = request.user
            if request_user.is_freelancer:
                valid_choices = get_valid_choices("leave_types", request_user.id, request, is_freelancer=True)
            else:
                valid_choices = get_valid_choices("leave_types", request_user.company.id, request, is_company=True)
            if valid_choices and leave_type not in valid_choices:
                raise serializers.ValidationError(
                    {"type": f"Invalid leave type. Allowed choices are: {', '.join(valid_choices)}"}
                )

        # Fetch approved leave requests for the current year.
        leave_requests = employee.leave_requests.filter(
            start_date__year=START_YEAR,
            end_date__year=END_YEAR,
            status=StatusType.APPROVED.value,
        )

        if is_working_from_home:
            annual_leaves_allowed = employee.wfh_leaves_allowed
            leave_requests = leave_requests.filter(is_working_from_home=True)
        else:
            annual_leaves_allowed = employee.annual_leaves_allowed
            leave_requests = leave_requests.filter(is_working_from_home=False)
            # Exclude approved sick leave requests from the quota calculation.
            leave_requests = leave_requests.exclude(type__in=[LeaveType.SICK.value, LeaveType.UNPAID.value])

        total_leaves_taken = sum(calculate_leave_days(leave.start_date, leave.end_date) for leave in leave_requests)
        leave_balance = annual_leaves_allowed - total_leaves_taken if annual_leaves_allowed is not None else None

        start_date = attrs.get('start_date', instance.start_date if instance else None)
        end_date = attrs.get('end_date', instance.end_date if instance else None)

        if is_weekend(start_date):
            raise serializers.ValidationError("Start date cannot be on a weekend")
        if is_weekend(end_date):
            raise serializers.ValidationError("End date cannot be on a weekend")

        if not start_date or not end_date:
            raise serializers.ValidationError("Start date and End date are required.")

        if start_date > end_date:
            raise serializers.ValidationError("Start date cannot be after end date.")

        today = datetime.today().date()
        if not instance or (instance and (start_date != instance.start_date or end_date != instance.end_date)):
            if start_date < today:
                raise serializers.ValidationError("Start date cannot be in the past.")
            if end_date < today:
                raise serializers.ValidationError("End date cannot be in the past.")

        # Only check leave balance if the request is not a sick leave, 
        # the user is not a company admin, and not a freelancer (unlimited paid leaves).
        if not employee.user.is_company_admin and not employee.user.is_freelancer and leave_type not in [LeaveType.SICK.value, LeaveType.UNPAID.value]:
            leave_days_requested = calculate_leave_days(start_date, end_date)
            if leave_balance is None or leave_days_requested > leave_balance:
                raise serializers.ValidationError("Employee does not have enough leave balance.")

        overlap_requests = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status__in=[StatusType.PENDING.value, StatusType.APPROVED.value]
        )

        if instance:
            overlap_requests = overlap_requests.exclude(id=instance.id)

        if overlap_requests.exists():
            raise serializers.ValidationError("Employee has already requested leave for these dates.")

        return attrs

    def create(self, validated_data):
        """Create a leave request without assigning a legacy supervisor."""
        with transaction.atomic():
            request = self.context.get('request')
            request_user = request.user
            employee = request_user.staff
            
            # Check if user has create_without_approval permission
            has_create_without_approval_permissions = require_any_permission_multiple_modules(
                request_user, 
                {'leave_request': ['create_without_approval', 'full_access']}, 
                'company', 
                request.query_params
            )
            
            # Auto-approve request if user is a freelancer OR has create_without_approval permission
            if request_user.is_freelancer or has_create_without_approval_permissions:
                validated_data["status"] = StatusType.APPROVED.value

            leave_request = LeaveRequest.objects.create(
                employee=employee,
                **validated_data
            )

        return leave_request

    def update(self, instance, validated_data):
        """Update leave request with proper email notifications."""
        with transaction.atomic():
            request = self.context.get('request')
            request_user = request.user
            employee = instance.employee

            # Check if request has already been reviewed
            if instance.status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
                raise serializers.ValidationError("Request has already been reviewed.")

            # Handle status updates with email notifications
            if "status" in validated_data:
                new_status = validated_data["status"]

                if new_status not in {
                    StatusType.APPROVED.value,
                    StatusType.REJECTED.value,
                }:
                    raise serializers.ValidationError(
                        "Leave status can only be approved or rejected during review."
                    )

                if employee.user == request_user:
                    raise serializers.ValidationError(
                        "You cannot approve or reject your own leave request."
                    )

                if (
                    request_user.is_freelancer
                    or not request_user.company_id
                    or employee.user.company_id != request_user.company_id
                ):
                    raise serializers.ValidationError(
                        "You cannot review leave requests outside your company."
                    )

                has_leave_review_permissions = require_any_permission_multiple_modules(
                    request_user,
                    {'leave_request': ['review']},
                    'company',
                    request.query_params,
                )

                if not has_leave_review_permissions:
                    raise serializers.ValidationError(
                        "You need leave review permission to approve or reject "
                        "leave requests."
                    )

            return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["created_by_solution_role_details"] = None 
        user_solution_role = UserRole.objects.filter(user=instance.employee.user, role__type="solution", role__scope="company").first()
        if user_solution_role:
            representation["created_by_solution_role_details"] = {
                "id": user_solution_role.id,
                "name": user_solution_role.role.name,
                "description": user_solution_role.role.description,
                "is_default": user_solution_role.role.is_default,
                "is_active": user_solution_role.role.is_active,
            }
        return representation
