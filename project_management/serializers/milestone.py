from decimal import Decimal, InvalidOperation

from django.db.models import Sum, Q
from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import serializers


from project_management.models import Milestone, ActionItem, Project

from project_management.serializers.document import DocumentSerializer
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from project_management.serializers.action_item import ActionItemSerializer
from .time_expense import get_valid_choices
from ecpmpapp.constants import UserType
from project_management.utils import get_product_name_from_request, has_project_permission
from ecpmpapp.constants import StatusType
from ecpmpapp.utils import require_any_permission_multiple_modules

PHASE_BUDGET_EXCEEDS_PROJECT_WARNING = "phase_allocated_budget_exceeds_contract_value"
PHASE_BUDGET_EXCEEDS_PROJECT_CREATED_MESSAGE = (
    "Phase created. The phase budgets exceed the project budget. "
    "Please adjust the budgets accordingly."
)
PHASE_BUDGET_EXCEEDS_PROJECT_UPDATED_MESSAGE = (
    "Phase updated. The phase budgets exceed the project budget. "
    "Please adjust the budgets accordingly."
)


def _as_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


class MilestoneSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = Milestone
        fields = "__all__"
        read_only_fields = ("sort_order",)

    def _set_phases_budget_warning(
        self,
        project,
        new_budget,
        message,
        *,
        exclude_milestone_id=None,
    ):
        if project is None:
            return
        milestones = project.milestones.all()
        if exclude_milestone_id is not None:
            milestones = milestones.exclude(pk=exclude_milestone_id)
        existing_total = _as_decimal(
            milestones.aggregate(total=Sum("allocated_budget"))["total"]
        )
        contract_value = _as_decimal(project.contract_value)
        if existing_total + _as_decimal(new_budget) > contract_value:
            self._response_warnings = [
                {
                    "code": PHASE_BUDGET_EXCEEDS_PROJECT_WARNING,
                    "message": message,
                }
            ]

    def get_linked_folder_owner_ids(self):
        project_id = self.initial_data.get("project") if hasattr(self, "initial_data") else None
        if not project_id and self.instance:
            project_id = self.instance.project_id
        return project_id, None, None

    # TODO: Adjust as per new permission system

    # def validate_project(self, value):
    #     request_user = self.context.get('user')
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.user == request_user and value.solution in products:
    #         return value
    #     if has_project_permission("PUT", request_user, value, product_name):
    #         return value
    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")


    def validate_status(self, value):
        if self.instance:
            project_id = self.instance.project.id
        else:
            project_id = self.initial_data.get('project')
        request = self.context.get('request')
        valid_choices = get_valid_choices("status_options", project_id, request)
        valid_status = {choice.split(":")[0] for choice in valid_choices}

        if valid_status and value not in valid_status:
            raise serializers.ValidationError(f"Invalid milestone status. Allowed choices are: {', '.join(valid_status)}")

        if value == "closed":
            milestone = self.instance
            if milestone and milestone.action_items.exists():
                open_action_items = milestone.action_items.exclude(status="closed")
                if open_action_items.exists():
                    raise serializers.ValidationError("All related action items must be marked as 'closed' before closing the project phase.")

        return value

    def validate_reminder_setting(self, value):
        if self.instance:
            project_id = self.instance.project.id
        else:
            project_id = self.initial_data.get('project')
        request = self.context.get('request')
        valid_choices = get_valid_choices("reminder_settings", project_id, request)
        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid reminder setting. Allowed choices are: {', '.join(valid_choices)}")
        return value

    def validate_documents(self, documents):
        milestone_project = self.instance.project.id if self.instance else int(self.initial_data.get('project'))

        for document in documents:
            if document.folder.project.id != milestone_project:
                raise serializers.ValidationError(
                    f"Document {document.file_name} is not linked to the same project as the milestone."
                )
        return documents

    def validate(self, data):
        project_start = self.instance.project.project_start_date if self.instance else data.get('project').project_start_date
        project_end = self.instance.project.project_end_date if self.instance else data.get('project').project_end_date

        start_date = data.get('start_date', self.instance.start_date if self.instance else None)
        due_date = data.get('due_date', self.instance.due_date if self.instance else None)

        if start_date and project_start and start_date < project_start:
            raise serializers.ValidationError("Milestone start date must not be earlier than the project's start date.")
        
        if due_date:
            if start_date and start_date > due_date:
                raise serializers.ValidationError("Milestone start date must be less than or equal to the due date.")
            if project_end and due_date > project_end:
                raise serializers.ValidationError("Milestone due date must not be later than the project's end date.")

        return data

    def create(self, validated_data):
        project = validated_data.get("project")
        new_budget = validated_data.get("allocated_budget")
        self._set_phases_budget_warning(
            project,
            new_budget,
            PHASE_BUDGET_EXCEEDS_PROJECT_CREATED_MESSAGE,
        )
        return super().create(validated_data)

    def update(self, instance, validated_data):
        project = validated_data.get("project", instance.project)
        new_budget = validated_data.get(
            "allocated_budget", instance.allocated_budget
        )
        self._set_phases_budget_warning(
            project,
            new_budget,
            PHASE_BUDGET_EXCEEDS_PROJECT_UPDATED_MESSAGE,
            exclude_milestone_id=instance.pk,
        )
        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["project"] = {
            "id": instance.project.id,
            "project_name": instance.project.project_name
        }
        ecpmp_config = instance.project.configurations
        status_color = "#00FF00"

        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data

        if ecpmp_config and ecpmp_config.status_options:
            status_dict = dict(option.split(":") for option in ecpmp_config.status_options)
            status_color = status_dict.get(instance.status, status_color)
        if instance.closed_by:
            representation["closed_by"] = {
                "id": instance.closed_by.id,
                "name": instance.closed_by.full_name
            }
        representation["status_color"] = status_color
        representation["booked_hour"] = instance.time_cards.filter(status=StatusType.APPROVED.value).aggregate(total_hours=Sum('hours'))['total_hours'] or 0
        representation["booked_cost"] = instance.expense_cards.filter(status=StatusType.APPROVED.value).aggregate(total_cost=Sum('total'))['total_cost'] or 0

        warnings = getattr(self, "_response_warnings", None)
        if warnings:
            representation["warnings"] = warnings

        return representation
