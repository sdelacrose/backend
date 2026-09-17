from django.db import transaction
from django.db.models import Sum, Q
from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import serializers
import logging

from models.models import User

from project_management.models import (
    ActionItem,
    ActionItemDependency,
    Milestone,
    Project,
    Requirement,
)
from project_management.action_item_dependencies import (
    filter_dependency_candidates_by_visibility,
    get_dependent_candidate_queryset,
    replace_action_item_dependents,
    replace_action_item_predecessors,
    serialize_dependent_candidate,
    serialize_predecessor_candidate,
)

from project_management.serializers import DocumentSerializer
from project_management.serializers.linked_folder import LinkedFolderSerializerMixin
from project_management.permissions import RequirementPermission
from .time_expense import get_valid_choices
from ecpmpapp.constants import StatusType, UserType
from project_management.utils import get_product_name_from_request, has_project_permission

logger = logging.getLogger(__name__)


class ActionItemSerializer(LinkedFolderSerializerMixin, serializers.ModelSerializer):
    sub_action_items = serializers.SerializerMethodField()
    num_sub_action_items = serializers.IntegerField(read_only=True)
    parent_summary = serializers.SerializerMethodField()
    predecessors = serializers.SerializerMethodField()
    dependents = serializers.SerializerMethodField()
    predecessor_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ActionItem.objects.all(),
        required=False,
        write_only=True,
    )
    dependent_ids = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=ActionItem.objects.all(),
        required=False,
        write_only=True,
    )
    assignees = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=User.objects.all(),
        required=False,
    )
    requirements = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Requirement.objects.select_related("project").all(),
        required=False,
    )

    class Meta:
        model = ActionItem
        fields = "__all__"

    def get_fields(self):
        fields = super().get_fields()
        if not self.context.get("serialize_sub_action_items", True):
            fields.pop("sub_action_items", None)
        return fields

    def get_sub_action_items(self, obj):
        """
        Recursively serialize all sub-action items for the given ActionItem.
        """
        if self.context.get('serialize_sub_action_items', True):
            sub_items = list(obj.sub_action_items.all())
            if self.context.get('limit_sub_action_items_to_visible'):
                visible_action_item_ids = set(
                    self.context.get('visible_action_item_ids', [])
                )
                sub_items = [
                    item
                    for item in sub_items
                    if item.id in visible_action_item_ids
                ]
            child_context = {
                **self.context,
                "include_dependents": False,
            }
            return ActionItemSerializer(
                sub_items,
                many=True,
                context=child_context,
            ).data
        return []

    def get_parent_summary(self, obj):
        parent = obj.parent_action_item
        if parent is None:
            return None

        milestone = parent.milestone
        return {
            "id": parent.id,
            "name": parent.name,
            "status": parent.status,
            "start_date": parent.start_date,
            "target_date": parent.target_date,
            "milestone": (
                {
                    "id": milestone.id,
                    "project": {"id": milestone.project_id},
                }
                if milestone is not None
                else None
            ),
            "company_profile": (
                {"id": parent.company_profile_id}
                if parent.company_profile_id is not None
                else None
            ),
            "freelancer": (
                {"id": parent.freelancer_id}
                if parent.freelancer_id is not None
                else None
            ),
        }

    def get_predecessors(self, obj):
        if not self.context.get("include_predecessors", False):
            return []

        dependencies = getattr(
            obj,
            "prefetched_predecessor_dependencies",
            None,
        )
        if dependencies is None:
            dependencies = (
                obj.incoming_dependencies.filter(
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
                .order_by("id")
            )

        predecessors = []
        for dependency in dependencies:
            predecessor_data = serialize_predecessor_candidate(
                dependency.predecessor
            )
            predecessor_data["dependency_type"] = (
                dependency.dependency_type
            )
            predecessors.append(predecessor_data)
        return predecessors

    def get_dependents(self, obj):
        if not self.context.get("include_dependents", False):
            return []

        dependencies = getattr(
            obj,
            "prefetched_dependent_dependencies",
            None,
        )
        if dependencies is None:
            dependencies = list(
                obj.outgoing_dependencies.filter(
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
                .order_by("id")
            )

        user = self.context.get("user")
        if user and dependencies:
            dependent_ids = [
                dependency.dependent_id for dependency in dependencies
            ]
            visible_ids = set(
                filter_dependency_candidates_by_visibility(
                    get_dependent_candidate_queryset(obj).filter(
                        id__in=dependent_ids
                    ),
                    user,
                    obj,
                ).values_list("id", flat=True)
            )
            dependencies = [
                dependency
                for dependency in dependencies
                if dependency.dependent_id in visible_ids
            ]

        dependents = []
        for dependency in dependencies:
            dependent_data = serialize_dependent_candidate(
                dependency.dependent
            )
            dependent_data["dependency_type"] = (
                dependency.dependency_type
            )
            dependents.append(dependent_data)
        return dependents

    # TODO: Adjust as per new permission system
    # def validate_milestone(self, value):
    #     if value is None:
    #         return value

    #     if self.instance:
    #         if self.instance.parent_action_item:
    #             raise serializers.ValidationError("Cannot assign a milestone to a sub-action item.")
    #         if self.instance.company_profile:
    #             raise serializers.ValidationError("An action item cannot be attached with both milestone and company profile.")
    
    #     request_user = self.context.get('user')
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.project.user == request_user and value.project.solution in products:
    #         return value

    #     if self.instance:
    #         if (self.instance.assignee and self.instance.assignee == request_user) or has_project_permission("PUT", request_user, value.project, product_name):
    #             return value
    #     else:
    #         if has_project_permission("PUT", request_user, value.project, product_name):
    #             return value
    #     raise serializers.ValidationError(f"Milestone {value} does not belong to this user")
    
    def validate_company_profile(self, value):
        if value is None:
            return value

        if self.instance:
            if self.instance.parent_action_item:
                raise serializers.ValidationError("Cannot assign a company profile to a sub-action item.")
            if self.instance.milestone:
                raise serializers.ValidationError("An action item cannot be attached with both milestone and company profile.")
    
        request_user = self.context.get('user')
        if request_user.is_freelancer or value != request_user.company:
            raise serializers.ValidationError("You do not have permission to add action items for this company.")
        
        return value
    
    def validate_freelancer(self, value):
        if value is None:
            return value
        
        if self.instance:
            if self.instance.parent_action_item:
                raise serializers.ValidationError("Cannot assign a freelancer to a sub-action item.")
            if self.instance.milestone:
                raise serializers.ValidationError("An action item cannot be attached with both milestone and freelancer.")
    
        request_user = self.context.get('user')
        if not request_user.is_freelancer or value != request_user:
            raise serializers.ValidationError("You do not have permission to add action items for other users.")
        
        return value
        
        
    def _validate_assignee_membership(self, value):
        request_user = self.context.get('user')
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()
        if project_id:
            project = Project.objects.get(id=project_id)
            if not self.instance:
                # Special case for assignee with RO permissions
                # Check if this is a sub-action item and user is assignee of parent
                parent_action_item_id = self.initial_data.get('parent_action_item')
                is_parent_assignee = False
                
                if parent_action_item_id:
                    try:
                        parent_item = ActionItem.objects.get(id=parent_action_item_id)
                        is_parent_assignee = parent_item.assignees.filter(
                            id=request_user.id
                        ).exists()
                    except ActionItem.DoesNotExist:
                        is_parent_assignee = False
                
                # TODO: Adjust as per new permission system
                # if not is_parent_assignee and not has_project_permission("PUT", request_user, project, get_product_name_from_request(self.context.get('request'))):
                #     raise serializers.ValidationError(f"You do not have permission to add action items for this project.")
            if not project.team_members.filter(team_member=value).first():
                raise serializers.ValidationError(f"Team member {value} does not belong to one of the user's projects")
        elif company_profile_id:
            if request_user.is_freelancer or \
               value.is_freelancer:
                raise serializers.ValidationError("Freelancer cannot add action items for a company.")

            if request_user.company != value.company or company_profile_id != value.company.id:
                raise serializers.ValidationError("Assignee should belong to the same company.")
        elif freelancer_id:
            if not request_user.is_freelancer or  request_user.id != freelancer_id:
                raise serializers.ValidationError("Cannot assign action item to other users.")
        else:
            raise serializers.ValidationError("Unable to get related company/project")
        return value

    def validate_assignees(self, value):
        if not value:
            if self.instance and not self.instance.assignees.exists():
                return value
            raise serializers.ValidationError("Action item assignees cannot be empty")
        seen_ids = set()
        for assignee in value:
            if assignee.id in seen_ids:
                raise serializers.ValidationError("Duplicate assignees are not allowed.")
            seen_ids.add(assignee.id)
            self._validate_assignee_membership(assignee)
        return value
    
    def get_related_milestone_freelancer_and_project_id(self):
        milestone_id = self.initial_data.get('milestone') if hasattr(self, 'initial_data') else None
        company_profile_id = self.initial_data.get('company_profile') if hasattr(self, 'initial_data') else None
        freelancer_id = self.initial_data.get('freelancer') if hasattr(self, 'initial_data') else None
        
        if self.instance:
            if not milestone_id and self.instance.milestone:
                milestone_id = self.instance.milestone.id

            if not company_profile_id and self.instance.company_profile:
                company_profile_id = self.instance.company_profile.id
            
            if not freelancer_id and self.instance.freelancer:
                freelancer_id = self.instance.freelancer.id

        return milestone_id, company_profile_id, freelancer_id
  
    def validate_parent_action_item(self, value):
        if self.instance and value != self.instance.parent_action_item:
            raise serializers.ValidationError(
                "Use the Make Sub Action operation to change an Action "
                "Item's parent."
            )

        if value:
            user = self.context.get('user')
            # Check if the parent action item is associated with a milestone
            if value.milestone:
                # Ensure sub-action items do not have their own milestone
                milestone_id, company_profile_id, freelancer_id = self.get_related_milestone_freelancer_and_project_id()
                if milestone_id or company_profile_id or freelancer_id:
                    raise serializers.ValidationError("Sub-action items cannot be attached with a milestone or a company.")

                # TODO: Adjust as per new permission system
                # Ensure the user has project-level permissions for the associated project
                # if not value.assignee == user and not has_project_permission("PUT", user, value.milestone.project, get_product_name_from_request(self.context.get('request'))):
                #     raise serializers.ValidationError("You do not have permission to add action items in this project.")
            
            # Check if the parent action item is associated with a company profile
            elif value.company_profile:
                # Ensure sub-action items do not have their own company profile
                milestone_id, company_profile_id, freelancer_id = self.get_related_milestone_freelancer_and_project_id()
                
                if milestone_id or company_profile_id or freelancer_id:
                    raise serializers.ValidationError("Sub-action items cannot be attached with a milestone or a company.")

                if user.is_freelancer or user.company != value.company_profile:
                    raise serializers.ValidationError("You do not have permission to add sub-action items for this company.")

            elif value.freelancer:
                milestone_id, company_profile_id, freelancer_id = self.get_related_milestone_freelancer_and_project_id()
                
                if milestone_id or company_profile_id or freelancer_id:
                    raise serializers.ValidationError("Sub-action items cannot be attached with a milestone or a company.")

                if not user.is_freelancer or user != value.freelancer:
                    raise serializers.ValidationError("You do not have permission to add sub-action items for this company.")

            # Ensure sub-action items cannot have further sub-action items
            if value.parent_action_item:
                raise serializers.ValidationError("A sub-action item cannot have another sub-action item as its parent.")
        
        return value

    def validate_documents(self, value):
        if not value:
            return value
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()
        if project_id:
            for document in value:
                if not document.folder.project or (document.folder.project and document.folder.project.id != project_id):
                    raise serializers.ValidationError(f"Document {document.file_name} does not belong to the same project.")
        if company_profile_id:
            for document in value:
                if not document.folder.company_profile or (document.folder.company_profile and document.folder.company_profile.id != company_profile_id):
                    raise serializers.ValidationError(f"Document {document.file_name} does not belong to the same company.")
        if freelancer_id:
            for document in value:
                if not document.folder.freelancer or (document.folder.freelancer and document.folder.freelancer.id != freelancer_id):
                    raise serializers.ValidationError(f"Document {document.file_name} does not belong to the same freelancer.")
                    
        return value

    def _can_view_project_requirements(self, project):
        user = self.context.get("user")
        if not user or not getattr(user, "is_authenticated", False):
            return False

        permission_cache = self.context.setdefault(
            "action_item_requirement_visibility",
            {},
        )
        if project.id not in permission_cache:
            permission = RequirementPermission(action="view", scope="project")
            permission_cache[project.id] = permission._check_view_permission_project(
                user,
                None,
                {"project": project},
            )
        return permission_cache[project.id]

    def validate_requirements(self, value):
        if not value and (
            self.instance is None or not self.instance.requirements.exists()
        ):
            return value

        project_id, company_profile_id, freelancer_id = (
            self.get_related_project_id_or_company_id_or_freelancer()
        )
        if not project_id or company_profile_id or freelancer_id:
            raise serializers.ValidationError(
                "Requirements can only be linked to project Action Items."
            )

        project = value[0].project if value else Project.objects.get(id=project_id)
        if not self._can_view_project_requirements(project):
            raise serializers.ValidationError(
                "You do not have permission to view linked Requirements."
            )

        for requirement in value:
            if requirement.project_id != project_id:
                raise serializers.ValidationError(
                    "Linked Requirements must belong to the same project."
                )

        return value

    def _get_requirement_sequence(self, requirement):
        sequence_cache = self.context.setdefault(
            "action_item_requirement_sequences",
            {},
        )
        if requirement.project_id not in sequence_cache:
            requirement_rows = list(
                Requirement.objects.filter(project_id=requirement.project_id)
                .order_by("parent_id", "sort_order", "id")
                .values("id", "parent_id")
            )
            parent_by_id = {
                row["id"]: row["parent_id"] for row in requirement_rows
            }
            positions_by_id = {}
            sibling_position_by_parent = {}
            for row in requirement_rows:
                parent_id = row["parent_id"]
                sibling_position_by_parent[parent_id] = (
                    sibling_position_by_parent.get(parent_id, 0) + 1
                )
                positions_by_id[row["id"]] = sibling_position_by_parent[parent_id]

            project_sequences = {}
            for row in requirement_rows:
                current_id = row["id"]
                parts = []
                visited_ids = set()
                while current_id is not None and current_id not in visited_ids:
                    visited_ids.add(current_id)
                    parts.append(str(positions_by_id.get(current_id, 1)))
                    current_id = parent_by_id.get(current_id)
                project_sequences[row["id"]] = ".".join(reversed(parts))
            sequence_cache[requirement.project_id] = project_sequences

        return sequence_cache[requirement.project_id].get(requirement.id, "1")

    def validate_opportunities(self, value):
        if not value:
            return value
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()
        project = None

        # For milestone-based action items, infer the owning company/freelancer
        # from the related project so that opportunity IDs coming from the
        # request can be validated against the same owner, similar to documents.
        if project_id:
            try:
                project = Project.objects.get(id=project_id)
            except Project.DoesNotExist:
                raise serializers.ValidationError("Unable to get related project for validating opportunities.")

        effective_company_id = company_profile_id
        effective_freelancer_id = freelancer_id

        if project:
            if getattr(project, "company_profile_id", None):
                effective_company_id = project.company_profile_id
            if getattr(project, "user", None) and getattr(project.user, "is_freelancer", False):
                effective_freelancer_id = project.user.id

        if effective_company_id:
            for opportunity in value:
                if not opportunity.company_profile or opportunity.company_profile.id != effective_company_id:
                    raise serializers.ValidationError(
                        f"Opportunity {opportunity.title} does not belong to the same company."
                    )

        if effective_freelancer_id:
            for opportunity in value:
                if not opportunity.freelancer or opportunity.freelancer.id != effective_freelancer_id:
                    raise serializers.ValidationError(
                        f"Opportunity {opportunity.title} does not belong to the same freelancer."
                    )
        return value

    def get_company_id_or_project_id_or_freelancer_from_instance(self, instance):
        project_id = None
        company_profile_id = None
        freelancer_id = None
        if instance.milestone:
            try:
                project_id = instance.milestone.project.id
            except:
                project_id = None
        elif instance.company_profile:
            company_profile_id = instance.company_profile.id
        elif instance.freelancer:
            freelancer_id = instance.freelancer.id
        return project_id, company_profile_id, freelancer_id

    def get_related_project_id_or_company_id_or_freelancer(self):
        project_id = None
        company_profile_id = None
        freelancer_id = None
        if self.instance:
            if self.instance.parent_action_item:
                project_id, company_profile_id, freelancer_id = self.get_company_id_or_project_id_or_freelancer_from_instance(self.instance.parent_action_item)
            else:
                project_id, company_profile_id, freelancer_id = self.get_company_id_or_project_id_or_freelancer_from_instance(self.instance)
            if project_id is None and company_profile_id is None and freelancer_id is None:
                raise serializers.ValidationError("Unable to get related Project/CompanyProfile/Freelancer id.")
        elif 'milestone' in self.initial_data:
            milestone_id = int(self.initial_data.get('milestone'))
            project_id = self.get_project_id_from_initial_data(milestone_id)
        elif 'company_profile' in self.initial_data:
            company_profile_id = int(self.initial_data.get('company_profile'))
        elif UserType.FREELANCER.value in self.initial_data:
            freelancer_id = int(self.initial_data.get('freelancer'))
        elif 'parent_action_item' in self.initial_data:
            parent_action_item_id = int(self.initial_data.get('parent_action_item'))
            try:
                action_item = ActionItem.objects.get(id=parent_action_item_id)
                project_id, company_profile_id, freelancer_id = self.get_company_id_or_project_id_or_freelancer_from_instance(action_item)
            except ActionItem.DoesNotExist:
                raise serializers.ValidationError("Parent action item does not exist.")
        else:
            raise serializers.ValidationError("Unable to get related Project/CompanyProfile id.")
        return project_id, company_profile_id, freelancer_id

    def get_project_id_from_initial_data(self, milestone_id):
        try:
            return Milestone.objects.get(id=milestone_id).project.id
        except Milestone.DoesNotExist:
            raise serializers.ValidationError("Unable to get related milestone. Invalid milestone ID.")

    def perform_validation_using_config(self, key, value):
        request = self.context.get('request')
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()

        if project_id:
            valid_choices = get_valid_choices(key, project_id, request)
        elif company_profile_id:
            valid_choices = get_valid_choices(key, company_profile_id, request, is_company=True)
        elif freelancer_id:
            valid_choices = get_valid_choices(key, freelancer_id, request, is_freelancer=True)


        valid_status = {choice.split(":")[0] for choice in valid_choices}

        if valid_status and value not in valid_status:
            raise serializers.ValidationError(f"Invalid {key}. Allowed choices are: {', '.join(valid_status)}")
        return value

    def _normalize_string_list(self, value, field_name):
        if value in (None, []):
            return []

        if not isinstance(value, list):
            raise serializers.ValidationError(f"{field_name} must be a list.")

        normalized = [str(item).strip() for item in value if str(item).strip()]
        if len(normalized) != len(set(normalized)):
            raise serializers.ValidationError(f"Duplicate values are not allowed in {field_name}.")
        return normalized

    def validate_priority_level(self, value):
        if value:
            return self.perform_validation_using_config("priority_levels", value)
        return None

    def validate_labels(self, value):
        normalized_labels = self._normalize_string_list(value, "labels")
        if not normalized_labels:
            return []

        request = self.context.get('request')
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()

        if project_id:
            valid_choices = get_valid_choices("labels", project_id, request)
        elif company_profile_id:
            valid_choices = get_valid_choices(
                "labels",
                company_profile_id,
                request,
                is_company=True,
            )
        elif freelancer_id:
            valid_choices = get_valid_choices(
                "labels",
                freelancer_id,
                request,
                is_freelancer=True,
            )
        else:
            valid_choices = []

        valid_labels = {str(choice).strip() for choice in valid_choices if str(choice).strip()}
        if not valid_labels:
            raise serializers.ValidationError("No labels are configured for this action item.")

        invalid_labels = [label for label in normalized_labels if label not in valid_labels]
        if invalid_labels:
            raise serializers.ValidationError(
                f"Invalid labels. Allowed choices are: {', '.join(sorted(valid_labels))}"
            )

        return normalized_labels

    def validate_status(self, value):
        if value is None:
            raise serializers.ValidationError("Action item status cannot be null")
        request = self.context.get('request')
        project_id, company_profile_id, freelancer_id = self.get_related_project_id_or_company_id_or_freelancer()

        valid_choices = []

        if project_id:
            valid_choices = get_valid_choices("status_options", project_id, request)
        elif company_profile_id:
            valid_choices = get_valid_choices(
                "status_options",
                company_profile_id,
                request,
                is_company=True,
            )
        elif freelancer_id:
            valid_choices = get_valid_choices(
                "status_options",
                freelancer_id,
                request,
                is_freelancer=True,
            )
        else:
            valid_choices = []

        valid_statuses = {}
        for choice in valid_choices:
            status_value = choice.split(":", 1)[0].strip()
            if status_value:
                valid_statuses[status_value.lower()] = status_value

        normalized_value = str(value).strip()
        normalized_key = normalized_value.lower()

        if not valid_statuses:
            if normalized_value.lower() != StatusType.OPEN.value:
                logger.warning(
                    "ActionItem status fallback (no config): item_id=%s user_id=%s project_id=%s company_id=%s freelancer_id=%s requested=%s",
                    getattr(self.instance, "id", None),
                    getattr(request.user, "id", None) if request else None,
                    project_id,
                    company_profile_id,
                    freelancer_id,
                    normalized_value,
                )
            value = StatusType.OPEN.value
        else:
            value = valid_statuses.get(normalized_key, StatusType.OPEN.value)
            if normalized_key not in valid_statuses:
                logger.warning(
                    "ActionItem status fallback (invalid): item_id=%s user_id=%s project_id=%s company_id=%s freelancer_id=%s requested=%s",
                    getattr(self.instance, "id", None),
                    getattr(request.user, "id", None) if request else None,
                    project_id,
                    company_profile_id,
                    freelancer_id,
                    normalized_value,
                )

        if value == "closed":
            action_item = self.instance
            if action_item and action_item.sub_action_items.exists():
                open_action_items = action_item.sub_action_items.exclude(status="closed")
                if open_action_items.exists():
                    raise serializers.ValidationError("All related sub action items must be marked as 'closed' before closing the parent action item.")
        return value

    def validate_reminder(self, value):
        return self.perform_validation_using_config("reminder_settings", value)

    def get_parent_start_end_date(self):
        parent_start = None
        parent_end = None
        if self.instance:
            if self.instance.parent_action_item:
                parent_start = self.instance.parent_action_item.start_date
                parent_end = self.instance.parent_action_item.target_date
            elif self.instance.milestone:
                try:
                    parent_start = self.instance.milestone.start_date
                    parent_end = self.instance.milestone.due_date
                except:
                    parent_start = None
                    parent_end = None
        else:
            milestone_id = self.initial_data.get('milestone') if hasattr(self, 'initial_data') else None
            parent_action_id = self.initial_data.get('parent_action_item') if hasattr(self, 'parent_action_item') else None
            if milestone_id:
                try:
                    milestone = Milestone.objects.get(id=int(milestone_id))
                    parent_start = milestone.start_date
                    parent_end = milestone.due_date
                except Milestone.DoesNotExist:
                    parent_start = None
                    parent_end = None
            elif parent_action_id:
                try:
                    parent_action_item = ActionItem.objects.get(id=int(parent_action_id))
                    parent_start = parent_action_item.start_date
                    parent_end = parent_action_item.target_date
                except ActionItem.DoesNotExist:
                    parent_start = None
                    parent_end = None
        return parent_start, parent_end

    # TODO: Adjust as per new permission system
    # def get_user_permission_flags(self, instance, request_user):
    #     user_is_project_manager = False
    #     user_is_project_company_admin = False
    #     user_is_project_freelancer = False
    #     user_is_project_creater = False
    #     is_admin_or_creator = False
    #     is_freelancer_action_item = False

    #     if instance.parent_action_item:
    #         project_id, company_profile_id, freelancer_id = self.get_company_id_or_project_id_or_freelancer_from_instance(instance.parent_action_item)
    #     else:
    #         project_id, company_profile_id, freelancer_id = self.get_company_id_or_project_id_or_freelancer_from_instance(instance)

    #     if project_id or company_profile_id or freelancer_id:
    #         if company_profile_id:
    #             company_profile = (instance.company_profile if instance.company_profile 
    #                              else instance.parent_action_item.company_profile)
                
    #             is_admin_or_creator = (
    #                 company_profile.admin_user == request_user
    #                 or request_user.groups.filter(name=UserType.ADMIN.value).exists()
    #                 or instance.created_by == request_user
    #             )
    #         elif project_id:
    #             project = (instance.milestone.project if instance.milestone 
    #                      else instance.parent_action_item.milestone.project)
                
    #             user_is_project_manager = project.team_members.filter(
    #                 team_member=request_user, role__iexact=UserType.PROJECT_MANAGER.value
    #             ).exists()
    #             project_company = project.user.company
    #             if project_company and request_user.company == project_company and request_user.groups.filter(name__iexact=UserType.ADMIN.value).exists():
    #                     user_is_project_company_admin = True

    #             user_is_project_freelancer = project.user.is_freelancer and project.user == request_user
    #             user_is_project_creater = project.user == request_user
    #         elif freelancer_id:
    #             is_freelancer_action_item = True

    #         return (
    #             is_admin_or_creator
    #             or (
    #                 user_is_project_manager
    #                 or user_is_project_company_admin
    #                 or user_is_project_freelancer
    #                 or user_is_project_creater
    #             )
    #             or is_freelancer_action_item
    #         )
    #     else:
    #         raise serializers.ValidationError("Unable to get related Project/CompanyProfile id.")

    def validate(self, data):
        request_user = self.context.get('request').user

        if hasattr(self, "initial_data") and "assignee" in self.initial_data:
            raise serializers.ValidationError(
                {"assignee": "Use assignees instead of assignee."}
            )

        assignees_provided = "assignees" in data

        parent_action_item = data.get("parent_action_item")
        if (
            not self.instance
            and parent_action_item
            and str(parent_action_item.status or "").strip().lower()
            == "closed"
        ):
            raise serializers.ValidationError(
                {
                    "parent_action_item": (
                        "Sub-action items cannot be created under a closed "
                        "Action Item."
                    )
                }
            )

        if assignees_provided:
            if not data.get("assignees") and not (
                self.instance and not self.instance.assignees.exists()
            ):
                raise serializers.ValidationError("At least one assignee is required.")
        elif not self.instance:
            raise serializers.ValidationError("At least one assignee is required.")

        if not self.instance and data.get("predecessor_ids"):
            raise serializers.ValidationError(
                {
                    "predecessor_ids": (
                        "Predecessors can only be linked after the "
                        "Action Item has been created."
                    )
                }
            )

        if not self.instance and data.get("dependent_ids"):
            raise serializers.ValidationError(
                {
                    "dependent_ids": (
                        "Dependents can only be linked after the "
                        "Action Item has been created."
                    )
                }
            )

        if self.instance and (
            "milestone" in data or "parent_action_item" in data
        ):
            parent_action_item = data.get(
                "parent_action_item",
                self.instance.parent_action_item,
            )
            milestone = data.get("milestone", self.instance.milestone)
            if parent_action_item:
                milestone = parent_action_item.milestone

            project_id = milestone.project_id if milestone else None
            requirements = data.get(
                "requirements",
                self.instance.requirements.all(),
            )
            if any(
                requirement.project_id != project_id
                for requirement in requirements
            ):
                raise serializers.ValidationError(
                    {
                        "requirements": (
                            "Linked Requirements must belong to the same project."
                        )
                    }
                )

        # TODO: Adjust as per new permission system
        # if self.instance and self.instance.assignee == request_user:
        #     allowed_users = self.get_user_permission_flags(self.instance, request_user)
        #     if not allowed_users:
        #         restricted_fields = {
        #             'start_date': self.instance.start_date,
        #             'target_date': self.instance.target_date,
        #             'priority_level': self.instance.priority_level,
        #             'allocated_time': self.instance.allocated_time,
        #             'allocated_budget': self.instance.allocated_budget
        #         }

        #         for field, original_value in restricted_fields.items():
        #             if field in data and data[field] != original_value:
        #                 raise serializers.ValidationError(
        #                     f"As an assignee, you cannot modify {field}"
        #                 )

        # Existing date validation logic
        parent_start, parent_end = self.get_parent_start_end_date()
        start_date = data.get('start_date', self.instance.start_date if self.instance else None)
        target_date = data.get('target_date', self.instance.target_date if self.instance else None)
        
        if parent_end and target_date and target_date > parent_end:
            raise serializers.ValidationError("Target date must not be later than the parent's due date.")
        
        if start_date and target_date and start_date > target_date:
            raise serializers.ValidationError("Start date must be less than or equal to the target date.")

        return data

    def create(self, validated_data):
        request_user = self.context.get('user')
        assignees = validated_data.pop("assignees", [])
        linked_folders = validated_data.pop("linked_folders", [])
        predecessors = validated_data.pop("predecessor_ids", None)
        dependents = validated_data.pop("dependent_ids", None)
        related_fields = ["milestone", "company_profile", "freelancer"]
        provided_fields = [field for field in related_fields if validated_data.get(field)]

        if len(provided_fields) > 1:
            raise serializers.ValidationError("Action items can only be linked to one of milestone, company profile, or freelancer.")

        if validated_data.get('status') == "closed":
            raise serializers.ValidationError("Action items cannot be created with a closed status.")        
        
        validated_data["created_by"] = request_user

        with transaction.atomic():
            parent_action_item = validated_data.get("parent_action_item")
            if parent_action_item:
                parent_action_item = ActionItem.objects.select_for_update().get(
                    pk=parent_action_item.pk
                )
                if parent_action_item.parent_action_item_id:
                    raise serializers.ValidationError(
                        {
                            "parent_action_item": (
                                "A sub-action item cannot have another "
                                "sub-action item as its parent."
                            )
                        }
                    )
                if (
                    str(parent_action_item.status or "").strip().lower()
                    == "closed"
                ):
                    raise serializers.ValidationError(
                        {
                            "parent_action_item": (
                                "Sub-action items cannot be created under a "
                                "closed Action Item."
                            )
                        }
                    )
                validated_data["parent_action_item"] = parent_action_item

            instance = super().create(validated_data)
            if assignees:
                instance._assignee_notification_event = "create"
                instance.assignees.set(assignees)
            self.set_linked_folders(instance, linked_folders)
            if predecessors is not None:
                replace_action_item_predecessors(
                    instance,
                    predecessors,
                    request_user,
                )
            if dependents:
                replace_action_item_dependents(
                    instance,
                    dependents,
                    request_user,
                )

        return instance
    
    def update(self, instance, validated_data):
        if "created_by" in validated_data:
            raise serializers.ValidationError("You cannot update the creator of an action item.")
        assignees = validated_data.pop("assignees", None)
        assignees_changed = (
            assignees is not None
            and set(instance.assignees.values_list("id", flat=True))
            != {assignee.id for assignee in assignees}
        )
        linked_folders = validated_data.pop("linked_folders", None)
        predecessors = validated_data.pop("predecessor_ids", None)
        dependents = validated_data.pop("dependent_ids", None)
        with transaction.atomic():
            if assignees_changed:
                instance._assignee_notification_event = "update"
            instance = super().update(instance, validated_data)
            if assignees_changed:
                instance.assignees.set(assignees, clear=True)
            self.set_linked_folders(instance, linked_folders)
            if predecessors is not None:
                request_user = self.context.get("user")
                replace_action_item_predecessors(
                    instance,
                    predecessors,
                    request_user,
                )
            if dependents is not None:
                request_user = self.context.get("user")
                replace_action_item_dependents(
                    instance,
                    dependents,
                    request_user,
                )
            for cache_attribute in (
                "prefetched_predecessor_dependencies",
                "prefetched_dependent_dependencies",
            ):
                if hasattr(instance, cache_attribute):
                    delattr(instance, cache_attribute)
        return instance

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation.pop("assignee", None)
        assignees = list(instance.assignees.all())
        representation["assignees"] = [
            {
                "id": assignee.id,
                "name": assignee.full_name,
            }
            for assignee in assignees
        ]
        project_id, _, _ = self.get_company_id_or_project_id_or_freelancer_from_instance(
            instance.parent_action_item or instance
        )
        visible_requirements = []
        if project_id:
            project = (
                instance.milestone.project
                if instance.milestone_id
                else instance.parent_action_item.milestone.project
            )
            if self._can_view_project_requirements(project):
                visible_requirements = [
                    requirement
                    for requirement in instance.requirements.all()
                    if requirement.project_id == project_id
                ]
        representation["requirements"] = [
            {
                "id": requirement.id,
                "name": requirement.name,
                "object_number": requirement.object_number,
                "sequence": self._get_requirement_sequence(requirement),
            }
            for requirement in visible_requirements
        ]
        # Resolve effective milestone (from instance or parent)
        effective_milestone = instance.milestone
        if not effective_milestone and instance.parent_action_item:
            effective_milestone = instance.parent_action_item.milestone

        if effective_milestone:
            try:
                representation["milestone"] = {
                    "id": effective_milestone.id,
                    "name": effective_milestone.name,
                    "colour": effective_milestone.colour,
                    "due_date": effective_milestone.due_date,
                    "start_date": effective_milestone.start_date,
                    "project": {
                        "id": effective_milestone.project.id,
                        "currency": effective_milestone.project.currency,
                        "name": effective_milestone.project.project_name,
                        "start_date": effective_milestone.project.project_start_date,
                        "end_date": effective_milestone.project.project_end_date,
                        "solution": effective_milestone.project.solution,
                    }
                }
            except:
                representation["milestone"] = None
        
        if instance.company_profile:
            representation["company_profile"] = {
                "id": instance.company_profile.id,
                "company_name": instance.company_profile.company_name,
            }

        if instance.freelancer:
            representation["freelancer"] = {
                "id": instance.freelancer.id,
                "name": instance.freelancer.full_name,
            }
        status_color = "#00FF00"

        if instance.closed_by:
            representation["closed_by"] = {
                "id": instance.closed_by.id,
                "name": instance.closed_by.full_name
            }
        if instance.created_by:
            representation["created_by"] = {
                "id": instance.created_by.id,
                "name": instance.created_by.full_name
            }
        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data

        representation["opportunities"] = [
            {
                "id": opportunity.id,
                "title": opportunity.title,
                "reference": opportunity.reference,
                "status": opportunity.status,
                "customer_user": (
                    {
                        "id": opportunity.customer_user.id,
                        "name": opportunity.customer_user.name,
                    }
                    if opportunity.customer_user
                    else None
                ),
                "customer_company": (
                    {
                        "id": opportunity.customer_company.id,
                        "name": opportunity.customer_company.company_name,
                    }
                    if opportunity.customer_company
                    else None
                ),
            }
            for opportunity in instance.opportunities.all()
        ]

        if effective_milestone:
            try:
                ecpmp_config = effective_milestone.project.configurations
            except:
                ecpmp_config = None
        elif instance.company_profile:
            try:
                ecpmp_config = instance.company_profile.global_config
            except:
                ecpmp_config = None
        else:
            ecpmp_config = None
        if ecpmp_config and ecpmp_config.status_options:
            status_dict = dict(option.split(":") for option in ecpmp_config.status_options)
            status_color = status_dict.get(instance.status, status_color)
        
        annotated_booked_hour = getattr(instance, "booked_hour", None)
        annotated_booked_cost = getattr(instance, "booked_cost", None)
        representation["booked_hour"] = (
            annotated_booked_hour
            if annotated_booked_hour is not None
            else instance.time_cards.filter(status=StatusType.APPROVED.value).aggregate(total_hours=Sum('hours'))['total_hours']
        ) or 0
        representation["booked_cost"] = (
            annotated_booked_cost
            if annotated_booked_cost is not None
            else instance.expense_cards.filter(status=StatusType.APPROVED.value).aggregate(total_cost=Sum('total'))['total_cost']
        ) or 0

        representation["status_color"] = status_color
        # representation["spent_hours"] = instance.project_works.all().aggregate(total_hours=Sum('hours'))['total_hours'] or 0
        return representation
