from datetime import date
from django.db.models import Q, Sum
from datetime import timedelta, date
from rest_framework.exceptions import PermissionDenied
from rest_framework import serializers

from human_resource_management.constants import START_YEAR, END_YEAR
from human_resource_management.models import StaffObservation
from models.models import User, Staff, StaffConfiguration
from project_management.models.project import Project
from human_resource_management.utils import calculate_leave_days
from ecpmpapp.utils import require_any_permission_multiple_modules

from datetime import date
from django.db.models import Sum, Q
from ecpmpapp.constants import LeaveType, StatusType, UserType

class StaffSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)
    user_id = serializers.CharField(source="user.id", read_only=True)
    mobile = serializers.CharField(source="user.mobile_phone", read_only=True)
    dob = serializers.DateField(source="user.dob", read_only=True)
    employment_status = serializers.SerializerMethodField()
    leave_taken = serializers.SerializerMethodField()
    leave_left = serializers.SerializerMethodField()
    work_from_home_taken = serializers.SerializerMethodField()
    work_from_home_left = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    project_allocation = serializers.SerializerMethodField()
    productivity_percentage = serializers.SerializerMethodField()
    total_allocated_hours = serializers.SerializerMethodField()
    total_spent_hours = serializers.SerializerMethodField()

    class Meta:
        model = Staff
        fields = [
            "name",
            "user_id",
            "mobile",
            "id",
            "dob",
            "employment_status",
            "annual_leaves_allowed",
            "wfh_leaves_allowed",
            "leave_taken",
            "leave_left",
            "work_from_home_taken",
            "work_from_home_left",
            "salary",
            "productivity_percentage",
            "hourly_rate",
            "location",
            "weekly_hours",
            "project_allocation",
            "user",
            "total_allocated_hours",
            "total_spent_hours",
        ]

    def get_employment_status(self, obj):
        return "Freelancer" if obj.user.is_freelancer else "Company User"

    def get_leave_taken(self, obj):
        # Exclude leave requests that are of type "sick"
        leave_requests = obj.leave_requests.filter(
            start_date__year=START_YEAR,
            end_date__year=END_YEAR,
            status=StatusType.APPROVED.value,
            is_working_from_home=False
        ).exclude(type__in=[LeaveType.SICK.value, LeaveType.UNPAID.value])
        total_leave_days = 0
        for leave in leave_requests:
            leave_days = calculate_leave_days(leave.start_date, leave.end_date)
            total_leave_days += leave_days
        return total_leave_days

    def get_leave_left(self, obj):
        leave_taken = self.get_leave_taken(obj)
        return obj.annual_leaves_allowed - leave_taken if obj.annual_leaves_allowed is not None else None
      
    def get_work_from_home_taken(self, obj):
        """
        Calculate total work-from-home days taken.
        This filters the leave_requests for approved requests of type "home_office"
        within the given year range.
        """
        wfh_requests = obj.leave_requests.filter(
            start_date__year=START_YEAR,
            end_date__year=END_YEAR,
            status=StatusType.APPROVED.value,
            is_working_from_home=True
        )
        total_wfh_days = 0
        for req in wfh_requests:
            days = calculate_leave_days(req.start_date, req.end_date)
            total_wfh_days += days
        return total_wfh_days

    def get_work_from_home_left(self, obj):
        wfh_taken = self.get_work_from_home_taken(obj)
        return obj.wfh_leaves_allowed - wfh_taken if obj.wfh_leaves_allowed is not None else None

    def get_location(self, obj):
        today = date.today()
        leave_today = obj.leave_requests.filter(start_date__lte=today, end_date__gte=today, status=StatusType.APPROVED.value).first()
        if leave_today:
            if leave_today.is_working_from_home:
                return "Remote"
            else:
                return "On Leave"
        else:
            return "Onsite"
        
    def get_project_allocation(self, obj):
        projects = Project.objects.filter(
            Q(user=obj.user) | Q(team_members__team_member=obj.user)
        ).values_list(flat=True)
        return projects                          

    def get_productivity_percentage(self, obj):
        # Get user creation date
        user_creation_date = obj.user.created_at.date()
        today = date.today()
        # Calculate total working days
        total_days = (today - user_creation_date).days
        total_weeks = total_days / 7
        # Take staff weekly hours default to 40
        total_working_hours = total_weeks * float(obj.weekly_hours or 40)
        # Get billed hours
        if obj.user.is_freelancer:
            total_billed_hours = (
                obj.user.time_cards.filter(status=StatusType.APPROVED.value)
                .aggregate(total_hours=Sum('hours'))
                .get('total_hours') or 0
            )
        else:
            total_billed_hours = (
                obj.user.created_time_cards.filter(status=StatusType.APPROVED.value)
                .aggregate(total_hours=Sum('hours'))
                .get('total_hours') or 0
            )
        if total_working_hours > 0:
            productivity_percentage = round((total_billed_hours / total_working_hours) * 100, 2)
            return productivity_percentage
        return 0

    def get_total_allocated_hours(self, obj):
        action_items = obj.user.multi_assigned_action_items.all()
        total_allocated_hours = action_items.aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
        return total_allocated_hours

    def get_total_spent_hours(self, obj):
        user_time_cards = obj.user.created_time_cards.filter(status=StatusType.APPROVED.value)
        total_spent_hours = user_time_cards.aggregate(total_hours=Sum('hours'))['total_hours'] or 0
        return total_spent_hours

    def to_representation(self, instance):
        """Limit field visibility based on role."""
        data = super().to_representation(instance)
        user = self.context.get("request").user
        data["observation_count"] = instance.staff_observations.count()
        
        # Check if user has staff management permissions (treated as admin)
        has_staff_management_permissions = require_any_permission_multiple_modules(
            user, 
            {'staff_management': ['view', 'full_access', 'update']}, 
            'company', 
            {}
        )
        
        # Admin, Supervisor, or the user viewing their own profile gets full visibility
        if has_staff_management_permissions or user == instance.user:
            return data
        # Regular Users get limited fields
        limited_fields = [
            "name",
            "user_id",
            "id",
            "dob",
            "employment_status",
            "leave_taken",
            "leave_left",
            "work_from_home_taken",
            "work_from_home_left",
            "location",
            "project_allocation",
            "observation_count"
        ]
        return {key: data[key] for key in limited_fields}

class StaffObservationSerializer(serializers.ModelSerializer):
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = StaffObservation
        fields = "__all__"
        read_only_fields = ["company_profile", "created_by", "created_at", "updated_at"]

    def create(self, validated_data):
        request_user = self.context['request'].user
        
        # Check if user has staff observation permissions
        has_staff_observation_permissions = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['create', 'full_access']}, 
            'company', 
            {}
        )
        
        if not has_staff_observation_permissions:
            raise PermissionDenied("You do not have permission to create a staff observation.")
        
        validated_data["created_by"] = request_user
        validated_data["company_profile"] = request_user.company  # Assuming user has a company attribute
        
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request_user = self.context['request'].user
        
        # Check if user has staff observation permissions
        has_staff_observation_full_access = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['full_access']}, 
            'company', 
            {}
        )
        
        has_staff_observation_update = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['update']}, 
            'company', 
            {}
        )
        
        has_staff_observation_update_created = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['update_created']}, 
            'company', 
            {}
        )
        
        # Users with full access can update any observation in their company
        if has_staff_observation_full_access or  has_staff_observation_update and instance.company_profile == request_user.company:
            return super().update(instance, validated_data)

        
        # Users with update_created permissions can only update their own observations
        elif has_staff_observation_update_created and instance.created_by == request_user:
            return super().update(instance, validated_data)
        
        raise PermissionDenied("You do not have permission to update this staff observation.")


class StaffConfigurationSerializer(serializers.ModelSerializer):
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = StaffConfiguration
        fields = "__all__"
