from calendar import monthrange
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from chat.serializers import ChatRoomSerializer
from rest_framework import serializers
from datetime import date, timedelta

from project_management.models import (
    Project, ProjectTeamMembers, ProjectExternalUsers, ProjectClient, ActionItem, ExpenseCard,
    TimeCard, PurchaseOrder, Machine
)
from project_management.models.constants import (
    ECPMP_COMMERCIAL_MODELS,
    ECPMP_INTERNATIONAL_STANDARDS,
    ECPMP_MAJOR_PROJECT_MODELS,
    ECPMP_CONTRACT_TYPES,
    ECPMP_FUNDING_STRATEGY,
    ECPMP_SUPPORTING_AGREEMENTS,
    GC_PROJECT_PRIORITY_LEVELS,
    GC_PROJECT_SERVICE_TYPES,
    resolve_project_template_locale,
)
from .time_expense import get_valid_choices
from models.models import CompanyConfig, Role, UserRole
from models import constants
from models.configuration_visibility import filter_enabled_flat_values
from project_management.utils import calculate_project_progress, get_product_name_from_request, has_accepted_connection, has_accepted_internal_user_connection, send_project_member_notification
from project_management.serializers.project_team import ProjectTeamSerializer, ProjectExternalUserSerializer, ProjectClientSerializer
from project_management.serializers.purchase_order import PurchaseOrderSerializer
from project_management.permissions import PurchaseOrderPermission
from ecpmpapp.constants import UserType
from ecpmpapp.utils import require_any_permission

PROJECT_BUDGET_BELOW_PHASES_WARNING = "project_contract_value_below_phases_budget"
PROJECT_BUDGET_BELOW_PHASES_MESSAGE = (
    "The phase budgets exceed the project budget. "
    "Please adjust the budgets accordingly."
)


class ProjectSerializer(serializers.ModelSerializer):
    locale = serializers.CharField(
        write_only=True,
        required=False,
        allow_blank=True,
    )
    client = ProjectClientSerializer(required=False, allow_null=True)
    team_members = ProjectTeamSerializer(many=True, required=False)
    external_users = ProjectExternalUserSerializer(many=True, required=False)
    machines = serializers.PrimaryKeyRelatedField(
        many=True,
        queryset=Machine.objects.all(),
        required=False,
    )
    machine_details = serializers.SerializerMethodField()
    project_chats = ChatRoomSerializer(many=True, read_only=True)
    allocated_hours = serializers.SerializerMethodField()
    purchase_orders = serializers.ListField(required=False, write_only=True)
    total_po_earning = serializers.SerializerMethodField()
    phases_budget = serializers.FloatField(read_only=True, allow_null=True)

    class Meta:
        model = Project
        fields = '__all__'
        read_only_fields = (
            "solution",
            "created_by",
            "created_by_email",
            "created_by_name",
            "total_po_earning",
            "phases_budget",
        )

    def _purchase_order_view_queryset(self, queryset):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        permission = PurchaseOrderPermission(action="view", scope="company")
        view_scope = getattr(self, "_purchase_order_view_scope", None)
        if view_scope is None:
            view_scope = permission.get_view_scope(user)
            self._purchase_order_view_scope = view_scope
        return permission.filter_queryset_for_view(
            queryset,
            user,
            view_scope=view_scope,
        )

    def _require_purchase_order_permission(self, action, purchase_order=None):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        permission = PurchaseOrderPermission(action=action, scope="company")
        if not user or not permission.has_permission(request, None):
            raise serializers.ValidationError(
                {"purchase_orders": f"You do not have permission to {action} purchase orders."}
            )
        if purchase_order and not permission.has_object_permission(
            request,
            None,
            purchase_order,
        ):
            raise serializers.ValidationError(
                {"purchase_orders": f"You do not have permission to {action} this purchase order."}
            )

    def get_total_po_earning(self, obj):
        result = self._purchase_order_view_queryset(
            obj.purchase_orders.all()
        ).aggregate(total=Sum('amount_excl_vat'))
        return result['total'] or 0.0

    def get_machine_details(self, obj):
        return [
            {
                "id": machine.id,
                "name": machine.name,
                "sequence": machine.get_sequence(),
                "created_at": machine.created_at,
                "updated_at": machine.updated_at,
            }
            for machine in obj.machines.all().order_by("sort_order", "id")
        ]

    def validate_company_profile(self, value):
        if value and not value.users.filter(id=self.context.get('user').id).exists():
            raise serializers.ValidationError(f"Company profile {value} does not belong to this user")
        return value

    def validate_user(self, value):
        request_user = self.context.get('user')
        if request_user:
            is_admin = not request_user.is_freelancer and request_user.groups.filter(name=UserType.ADMIN.value).exists()
        if value != request_user and not is_admin:
            raise serializers.ValidationError(f"Cannot assign a project to another user")
        if self.instance and self.instance.user != value:
            raise serializers.ValidationError(f"Cannot update project creator")
        return value

    def _get_company_config_choices(self, config_key, fallback):
        request = self.context.get("request")
        request_user = request.user if request else None
        product_name = (
            get_product_name_from_request(request) if request else "Energy Consultant Solution"
        )

        try:
            if request_user and request_user.is_freelancer:
                company_config = CompanyConfig.objects.filter(
                    user=request_user, solution=product_name
                ).first()
            elif request_user and request_user.company:
                company_config = CompanyConfig.objects.filter(
                    company_profile=request_user.company, solution=product_name
                ).first()
            else:
                company_config = None
            if company_config is None:
                return list(fallback or [])
            choices = getattr(company_config, config_key, None) or []
            return filter_enabled_flat_values(
                choices,
                company_config.configuration_visibility,
                config_key,
            )
        except Exception:
            return list(fallback or [])

    def _validate_configured_choice(self, value, config_key, fallback, error_label):
        if not value:
            return value

        company_choices = self._get_company_config_choices(config_key, fallback)

        if self.instance and value == getattr(self.instance, config_key, None):
            return value

        valid_choices = company_choices

        if value not in valid_choices:
            raise serializers.ValidationError(
                f"Invalid {error_label}. Allowed choices are: {', '.join(valid_choices)}"
            )
        return value

    def validate_contract_type(self, value):
        return self._validate_configured_choice(
            value,
            "contract_type",
            constants.GC_CONTRACT_TYPES or ECPMP_CONTRACT_TYPES,
            "contract type",
        )

    def validate_international_standard(self, value):
        return self._validate_configured_choice(
            value,
            "international_standard",
            constants.GC_INTERNATIONAL_STANDARDS or ECPMP_INTERNATIONAL_STANDARDS,
            "international standard",
        )

    def validate_major_project_model(self, value):
        return self._validate_configured_choice(
            value,
            "major_project_model",
            constants.GC_MAJOR_PROJECT_MODELS or ECPMP_MAJOR_PROJECT_MODELS,
            "major project model",
        )

    def validate_commercial_model(self, value):
        return self._validate_configured_choice(
            value,
            "commercial_model",
            constants.GC_COMMERCIAL_MODELS or ECPMP_COMMERCIAL_MODELS,
            "commercial model",
        )

    def validate_supporting_agreement(self, value):
        return self._validate_configured_choice(
            value,
            "supporting_agreement",
            constants.GC_SUPPORTING_AGREEMENTS or ECPMP_SUPPORTING_AGREEMENTS,
            "supporting agreement",
        )

    def validate_funding_strategy(self, value):
        if value:
            request = self.context.get('request')
            request_user = request.user
            product_name = get_product_name_from_request(request) if request else "Energy Consultant Solution"

            if self.instance:
                project_id = self.instance.id
                valid_choices = get_valid_choices("funding_strategy", project_id, request)
            else:
                try:
                    if request_user.is_freelancer:
                        company_config = CompanyConfig.objects.filter(user=request_user, solution=product_name).first()
                        valid_choices = company_config.funding_strategy if company_config else ECPMP_FUNDING_STRATEGY
                    elif request_user.company:
                        company_config = CompanyConfig.objects.filter(company_profile=request_user.company, solution=product_name).first()
                        valid_choices = company_config.funding_strategy if company_config else ECPMP_FUNDING_STRATEGY
                    else:
                        valid_choices = ECPMP_FUNDING_STRATEGY
                except Exception:
                    valid_choices = ECPMP_FUNDING_STRATEGY

            if valid_choices and value not in valid_choices:
                raise serializers.ValidationError(f"Invalid funding strategy. Allowed choices are: {', '.join(valid_choices)}")
        return value

    def validate_project_priority(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        request_user = request.user
        product_name = get_product_name_from_request(request) if request else "Energy Consultant Solution"
        if self.instance:
            project_id = self.instance.id
            valid_choices = get_valid_choices("project_priority_levels", project_id, request)
        else:
            try:
                if request_user.is_freelancer:
                    company_config = CompanyConfig.objects.filter(user=request_user, solution=product_name).first()
                    valid_choices = company_config.project_priority_levels if company_config else GC_PROJECT_PRIORITY_LEVELS
                elif request_user.company:
                    company_config = CompanyConfig.objects.filter(company_profile=request_user.company, solution=product_name).first()
                    valid_choices = company_config.project_priority_levels if company_config else GC_PROJECT_PRIORITY_LEVELS
                else:
                    valid_choices = GC_PROJECT_PRIORITY_LEVELS
            except Exception:
                valid_choices = GC_PROJECT_PRIORITY_LEVELS


        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid project priority. Allowed choices are: {', '.join(valid_choices)}")
        return value

    def validate_project_status(self, value):
        error_message = "All related {} must be marked as 'closed' before closing the {}."
        if value == "Closed" and self.instance:
            project = self.instance
            open_milestones_or_action_items = project.milestones.exclude(
                Q(status__iexact="closed") | Q(action_items__status__iexact="closed")
            ).exists()
            if open_milestones_or_action_items:
                raise serializers.ValidationError(error_message.format("project phases and action items", "project"))

        return value
    
    def validate_project_service_types(self, value):
        if value is None:
            return value
        request = self.context.get('request')
        request_user = request.user
        product_name = get_product_name_from_request(request) if request else "Energy Consultant Solution"
        if self.instance:
            project_id = self.instance.id
            valid_choices = get_valid_choices("project_service_types", project_id, request)
        else:
            try:
                if request_user.is_freelancer:
                    company_config = CompanyConfig.objects.filter(user=request_user, solution=product_name).first()
                    valid_choices = company_config.project_service_types if company_config else GC_PROJECT_SERVICE_TYPES
                elif request_user.company:
                    company_config = CompanyConfig.objects.filter(company_profile=request_user.company, solution=product_name).first()
                    valid_choices = company_config.project_service_types if company_config else GC_PROJECT_SERVICE_TYPES
                else:
                    valid_choices = GC_PROJECT_SERVICE_TYPES
            except Exception:
                valid_choices = GC_PROJECT_SERVICE_TYPES
        # TODO: Remove this after adding project service types to the company config and default values to the global config
        if len(valid_choices) == 0:
            return value
        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid service type. Allowed choices are: {', '.join(valid_choices)}")
        return value

    def check_team_member_connection_status(self, request_user, team_member):
        if request_user.is_freelancer:
            return team_member.is_freelancer and \
                has_accepted_connection(
                    request_user.sharemundo_links_id,
                    team_member.sharemundo_links_id,
                )
        elif request_user.company:
            if team_member.is_freelancer and \
                has_accepted_connection(
                    request_user.company.sharemundo_links_id,
                    team_member.sharemundo_links_id,
                ):
                return True
            if team_member.company and  \
               has_accepted_internal_user_connection(
                    request_user.company.sharemundo_links_id,
                    team_member.company.sharemundo_links_id,
                    team_member.id
            ):
                return True
                
        return False

    def validate_team_members(self, value):
        """
        Ensure no duplicate team members are added and validate roles.
        """
        request_user = self.context['request'].user
        team_member_ids = [team_member['team_member'] for team_member in value]
        
        # Check for duplicates
        if len(team_member_ids) != len(set(team_member_ids)):
            raise serializers.ValidationError("Duplicate team members are not allowed.")

        # Get project company for role validation
        project_company = None
        if self.instance:
            project_company = self.instance.company_profile if self.instance.company_profile else self.instance.user.company
        else:
            # For new projects, get company from request user
            if not request_user.is_freelancer:
                project_company = request_user.company

        for team_member in value:
            project_team_member = team_member['team_member']

            try:
                # Validate connection status
                if request_user.is_freelancer:
                    if request_user == project_team_member:
                        continue
                    if self.check_team_member_connection_status(request_user, project_team_member):
                        continue
                elif request_user.company:
                    if project_team_member.company and project_team_member.company == request_user.company:
                        continue
                    
                    if self.check_team_member_connection_status(request_user, project_team_member):
                        continue

            except Exception as e:
                raise serializers.ValidationError(f"User id: {project_team_member.id} isn't in your connections so can't be added as a team member.")

        for team_member in value:
            # Validate project_role if provided
            project_role = team_member.get('project_role')
            if project_role:
                # Check if role belongs to the same company as the project
                if project_company and project_role.company != project_company:
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' does not belong to the same company as the project"
                    )
                
                # Check if role is project-scoped
                if project_role.scope != 'project':
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' is not a project-scoped role"
                    )
                
                # Check if role is solution type
                if project_role.type != 'solution':
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' is not a solution-type role"
                    )
        return value

    def validate_external_users(self, value):
        """
        Ensure no duplicate external users are added and validate roles.
        """
        external_user_ids = [external_user.get('contact_user') for external_user in value if external_user.get('contact_user')]
        external_company_ids = [external_user['contact_company'] for external_user in value if external_user.get('contact_company')]
        
        # Check for duplicates
        if len(external_user_ids) != len(set(external_user_ids)) or len(external_company_ids) != len(set(external_company_ids)):
            raise serializers.ValidationError("Duplicate external users are not allowed.")
        
        # Get project company for role validation
        project_company = None
        if self.instance:
            project_company = self.instance.company_profile if self.instance.company_profile else self.instance.user.company
        else:
            # For new projects, get company from request user
            request_user = self.context['request'].user
            if not request_user.is_freelancer:
                project_company = request_user.company

        for external_user in value:
            # Validate project_role if provided
            project_role = external_user.get('project_role')
            if project_role:
                # Check if role belongs to the same company as the project
                if project_company and project_role.company != project_company:
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' does not belong to the same company as the project"
                    )
                
                # Check if role is project-scoped
                if project_role.scope != 'project':
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' is not a project-scoped role"
                    )
                
                # Check if role is solution type
                if project_role.type != 'solution':
                    raise serializers.ValidationError(
                        f"Role '{project_role.name}' is not a solution-type role"
                    )
        
        return value

    def validate(self, attrs):
        request_user = self.context['request'].user

        # Check if the request user is a freelancer
        is_request_user_freelancer = request_user.is_freelancer
        is_project_user_freelancer = attrs.get('user') and attrs.get('user').is_freelancer

        # If the user is a freelancer or the project's user is a freelancer, company_profile should be None
        if (is_request_user_freelancer or is_project_user_freelancer) and attrs.get('company_profile') is not None:
            raise serializers.ValidationError(
                "Freelancer cannot link any company profile with the project"
            )

        return attrs

    def validate_machines(self, value):
        company_profile = getattr(self.instance, "company_profile", None)
        if not company_profile:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            company_profile = getattr(user, "company", None)

        company_profile_id = getattr(company_profile, "id", None)
        for machine in value:
            if not company_profile_id or int(machine.company_profile_id) != int(company_profile_id):
                raise serializers.ValidationError(
                    "Linked machines must belong to the same company."
                )
        return value





    @transaction.atomic
    def create(self, validated_data):
        request = self.context.get("request")
        user = request.user
        requested_locale = validated_data.pop("locale", None)
        creation_locale = resolve_project_template_locale(
            requested_locale,
            getattr(user, "preferred_language", None),
        )
        if user.is_freelancer:
            validated_data['company_profile'] = None
        else:
            validated_data['company_profile'] = user.company
        
        freelancer = None
        company = None
        if user.is_freelancer:
            freelancer = user
        else:
            company = user.company

        product_name = get_product_name_from_request(request)

        validated_data["solution"] = product_name
        team_members_data = validated_data.pop('team_members', [])
        external_users_data = validated_data.pop('external_users', [])
        purchase_orders_data = validated_data.pop('purchase_orders', [])
        if purchase_orders_data:
            self._require_purchase_order_permission("create")
        machines = validated_data.pop('machines', [])
        client = validated_data.pop('client', None)
        validated_data["created_by"] = request.user
        validated_data["created_by_email"] = request.user.email
        validated_data["created_by_name"] = request.user.full_name
        project = Project(**validated_data)
        project._creation_locale = creation_locale
        project.save()

        if client:
            ProjectClient.objects.create(
                project=project,
                contact_user=client.get('contact_user'),
                contact_company=client.get('contact_company')
            )
        for member_data in team_members_data:
            # Handle both old role string and new project_role
            role = member_data.get('role')
            project_role = member_data.get('project_role')
            
            if project_role:
                # Use new role system
                project_team = ProjectTeamMembers.objects.create(
                    project=project,
                    role=project_role.name,  # Keep role field for backward compatibility
                    project_role=project_role,
                    team_member=member_data['team_member'],
                    project_access=member_data['project_access']
                )
                
                # Create UserRole object for project-level role assignment
                # Use get_or_create to avoid duplicate key errors

                UserRole.objects.get_or_create(
                    user=member_data['team_member'],
                    role=project_role,
                    company=company,
                    freelancer=freelancer,
                    defaults={
                        'assigned_by': request.user
                    }
                )
            else:
                # Use old role system with validation
                valid_choices = get_valid_choices("team_members_roles", project.id, request)
                if role not in valid_choices and role != 'client':
                    raise serializers.ValidationError(f"Invalid Team Member Role. Allowed choices are: {', '.join(valid_choices)}")
                project_team = ProjectTeamMembers.objects.create(
                    project=project,
                    role=role,
                    team_member=member_data['team_member'],
                    project_access=member_data['project_access']
                )
            
            project_team.save()
            if not project_team.team_member == project.user:
                send_project_member_notification(
                    project=project,
                    new_project_team_member=project_team,
                    requester=request.user,
                    created=True
                )
        
        for external_user in external_users_data:
            # Handle both old role string and new project_role
            role = external_user.get('role')
            project_role = external_user.get('project_role')
            
            if project_role:
                # Use new role system
                project_external_member = ProjectExternalUsers.objects.create(
                    project=project,
                    role=project_role.name,  # Keep role field for backward compatibility
                    project_role=project_role,
                    contact_user=external_user.get('contact_user'),
                    contact_company=external_user.get('contact_company')
                )
            else:
                # Use old role system with validation
                valid_choices = get_valid_choices("team_members_roles", project.id, request)
                if role not in valid_choices and role != 'client':
                    raise serializers.ValidationError(f"Invalid External User Role. Allowed choices are: {', '.join(valid_choices)}")
                project_external_member = ProjectExternalUsers.objects.create(
                    project=project,
                    role=role,
                    contact_user=external_user.get('contact_user'),
                    contact_company=external_user.get('contact_company')
                )
            
            project_external_member.save()
        
        for purchase_order in purchase_orders_data:

            purchase_order_serializer = PurchaseOrderSerializer(data=purchase_order, context={'project': project, 'request': request})
            purchase_order_serializer.is_valid(raise_exception=True)
            purchase_order_serializer.save()

        if machines:
            project.machines.set(machines)
           
        return project

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop("locale", None)
        new_contract_value = validated_data.get("contract_value")
        if new_contract_value is not None:
            previous_contract_value = Decimal(str(instance.contract_value))
            new_contract_value_decimal = Decimal(str(new_contract_value))
            if new_contract_value_decimal != previous_contract_value:
                phases_budget = instance.milestones.aggregate(
                    total=Sum("allocated_budget")
                )["total"] or 0
                if Decimal(str(phases_budget)) > new_contract_value_decimal:
                    self._response_warnings = [
                        {
                            "code": PROJECT_BUDGET_BELOW_PHASES_WARNING,
                            "message": PROJECT_BUDGET_BELOW_PHASES_MESSAGE,
                        }
                    ]

        team_members_data = validated_data.pop('team_members', None)
        external_users_data = validated_data.pop('external_users', None)
        client_data = validated_data.pop('client', None)
        solution = validated_data.get('solution', None)
        purchase_orders_data = validated_data.pop('purchase_orders', None)
        machines = validated_data.pop('machines', None)
        request = self.context.get('request')

        if solution and instance.solution != solution:
            raise serializers.ValidationError(f"Project solution cannot be changed.")

        instance = super().update(instance, validated_data)
        # Determine if this is a freelancer project or company project
        freelancer = None
        company = instance.company_profile

        # If project has no company_profile AND the user is a freelancer, it's a freelancer project
        if not instance.company_profile and instance.user.is_freelancer:
            freelancer = instance.user

        # Update or create ProjectClient
        if client_data is not None:
            ProjectClient.objects.update_or_create(
                project=instance,
                defaults={
                    'contact_user': client_data.get('contact_user'),
                    'contact_company': client_data.get('contact_company')
                }
            )
        else:
            ProjectClient.objects.filter(project=instance).delete()
        
        # Process team members
        if team_members_data is not None:
            processed_member_ids = []
            for member_data in team_members_data:
                # Handle both old role string and new project_role
                role = member_data.get('role')
                project_role = member_data.get('project_role')
                
                if project_role:
                    # Use new role system
                    project_team = ProjectTeamMembers.objects.filter(
                        project=instance, 
                        team_member=member_data['team_member']
                    ).first()

                    if project_team:
                        # Check if access is being changed and the requester is not the project owner
                        if (
                            project_team.project_role != project_role or 
                            project_team.project_access != member_data['project_access']
                        ):
                            # Check if user has either full_access or update permission for the project
                            has_permission = require_any_permission(
                                request.user, 
                                'project', 
                                ['full_access', 'update'], 
                                'solution', 
                                {'project': instance}
                            )
                            
                            if not has_permission:
                                raise serializers.ValidationError(
                                    "You do not have permission to modify project team member roles. "
                                    "Required permissions: project.full_access or project.update"
                                )
                    
                        # If role is being changed, delete the old UserRole object
                        if project_team.project_role and project_team.project_role != project_role:
                            # Check if this role is still assigned to this user on any other project in the same company
                            if not ProjectTeamMembers.objects.filter(
                                team_member=member_data['team_member'],
                                project_role=project_team.project_role,
                                project__company_profile=instance.company_profile
                            ).exclude(project=instance).exists():
                                # Only delete if no other project assignments exist for this role in this company
                                UserRole.objects.filter(
                                    user=member_data['team_member'],
                                    role=project_team.project_role,
                                    company=company,
                                    freelancer=freelancer
                                ).delete()
                        
                    project_team, created = ProjectTeamMembers.objects.update_or_create(
                        project=instance,
                        team_member=member_data['team_member'],
                        defaults={
                            'role': project_role.name,  # Keep role field for backward compatibility
                            'project_role': project_role, 
                            'project_access': member_data['project_access']
                        }
                    )
                    

                    # Create or update UserRole object for project-level role assignment
                    UserRole.objects.get_or_create(
                        user=member_data['team_member'],
                        role=project_role,
                        company=company,
                        freelancer=freelancer,
                        defaults={
                            'assigned_by': request.user
                        }
                    )
                else:
                    # Use old role system with validation
                    valid_choices = get_valid_choices("team_members_roles", instance.id, request)
                    if role not in valid_choices:
                        raise serializers.ValidationError(f"Invalid Team Member Role. Allowed choices are: {', '.join(valid_choices)}")
                    
                    project_team = ProjectTeamMembers.objects.filter(
                        project=instance, 
                        team_member=member_data['team_member']
                    ).first()

                    if project_team:
                        # Check if access is being changed and the requester is not the project owner
                        if (
                            (project_team.role != role or project_team.project_access != member_data['project_access'])
                            and self.context['request'].user != instance.user
                        ):
                            raise serializers.ValidationError(
                                "You do not have permission to modify your own access rights."
                            )
                    
                        # If switching from project_role to string role, delete the old UserRole object
                        if project_team.project_role:
                            # Check if this role is still assigned to this user on any other project in the same company
                            if not ProjectTeamMembers.objects.filter(
                                team_member=member_data['team_member'],
                                project_role=project_team.project_role,
                                project__company_profile=instance.company_profile
                            ).exclude(project=instance).exists():
                                UserRole.objects.filter(
                                    user=member_data['team_member'],
                                    role=project_team.project_role,
                                    company=company,
                                    freelancer=freelancer
                                ).delete()
                
                    project_team, created = ProjectTeamMembers.objects.update_or_create(
                        project=instance,
                        team_member=member_data['team_member'],
                        defaults={'role': role, 'project_access': member_data['project_access']}
                    )
                
                processed_member_ids.append(project_team.id)
                # project_team.save()
                if created and not project_team.team_member == instance.user:
                    send_project_member_notification(
                        project=instance,
                        new_project_team_member=project_team,
                        requester=request.user
                    )

            # Remove team members not in the update list
            removed_members = instance.team_members.exclude(id__in=processed_member_ids)
            
            # Remove UserRole objects for removed team members
            for removed_member in removed_members:
                if removed_member.project_role:
                    # Determine if this is a freelancer project or company project
                    freelancer = None
                    company = instance.company_profile

                    # If project has no company_profile AND the user is a freelancer, it's a freelancer project
                    if not instance.company_profile and instance.user.is_freelancer:
                        freelancer = instance.user

                    # Check if this role is still assigned to this user on any other project in the same company
                    if not ProjectTeamMembers.objects.filter(
                        team_member=removed_member.team_member,
                        project_role=removed_member.project_role,
                        project__company_profile=instance.company_profile
                    ).exclude(project=instance).exists():
                        UserRole.objects.filter(
                            user=removed_member.team_member,
                            role=removed_member.project_role,
                            company=company,
                            freelancer=freelancer
                        ).delete()
            
            removed_members.delete()

        # Process external users
        if external_users_data is not None:
            processed_external_user_ids = []
            for external_user in external_users_data:
                # Handle both old role string and new project_role
                role = external_user.get('role')
                project_role = external_user.get('project_role')
                
                if project_role:
                    # Use new role system
                    project_external_user, created = ProjectExternalUsers.objects.update_or_create(
                        project=instance,
                        contact_user=external_user.get('contact_user'),
                        contact_company=external_user.get('contact_company'),
                        defaults={
                            'role': project_role.name,  # Keep role field for backward compatibility
                            'project_role': project_role
                        }
                    )
                else:
                    # Use old role system with validation
                    request = self.context.get('request')
                    valid_choices = get_valid_choices("team_members_roles", instance.id, request)
                    if role not in valid_choices:
                        raise serializers.ValidationError(f"Invalid External User Role. Allowed choices are: {', '.join(valid_choices)}")
                    
                    project_external_user, created = ProjectExternalUsers.objects.update_or_create(
                        project=instance,
                        contact_user=external_user.get('contact_user'),
                        contact_company=external_user.get('contact_company'),
                        defaults={'role': role}
                    )
                
                processed_external_user_ids.append(project_external_user.id)
                project_external_user.save()

            # Remove external users not in the update list
            instance.external_users.exclude(id__in=processed_external_user_ids).delete()

        # Process purchase orders
        if purchase_orders_data is not None:
            new_purchase_orders = []
            existing_purchase_orders = []
            created_po_ids = []  # Track newly created PO IDs
            
            for purchase_order in purchase_orders_data:
                if purchase_order.get('id'):
                    existing_purchase_orders.append(purchase_order)
                else:
                    new_purchase_orders.append(purchase_order)
            
            # Create new purchase orders
            if new_purchase_orders:
                self._require_purchase_order_permission("create")
            for purchase_order in new_purchase_orders:
                purchase_order_serializer = PurchaseOrderSerializer(data=purchase_order, context={'project': instance, 'request': request})
                purchase_order_serializer.is_valid(raise_exception=True)
                saved_po = purchase_order_serializer.save()
                created_po_ids.append(saved_po.id)  # Track the ID of newly created PO
            
            # Update existing purchase orders using serializer
            for purchase_order in existing_purchase_orders:
                try:
                    existing_po = instance.purchase_orders.get(
                        id=purchase_order.get('id')
                    )
                    self._require_purchase_order_permission("update", existing_po)
                    purchase_order_serializer = PurchaseOrderSerializer(
                        instance=existing_po, 
                        data=purchase_order, 
                        context={'project': instance, 'request': request},
                        partial=True
                    )
                    purchase_order_serializer.is_valid(raise_exception=True)
                    purchase_order_serializer.save()
                except PurchaseOrder.DoesNotExist:
                    raise serializers.ValidationError(f"Purchase order with id {purchase_order.get('id')} does not exist")
            
            # Collect all PO IDs that should be kept (existing + newly created)
            all_po_ids = [po.get('id') for po in existing_purchase_orders] + created_po_ids
            
            # Only visible rows participate in nested synchronization. Hidden rows
            # must never be deleted because they were omitted from the payload.
            purchase_orders_to_delete = self._purchase_order_view_queryset(
                instance.purchase_orders.exclude(id__in=all_po_ids)
            )
            for purchase_order in purchase_orders_to_delete:
                self._require_purchase_order_permission("delete", purchase_order)
                if purchase_order.change_notices.exists():
                    raise serializers.ValidationError(
                        {
                            "purchase_orders": (
                                "A purchase order with linked change notices cannot be deleted."
                            )
                        }
                    )
                try:
                    purchase_order.invoices.update(purchase_order=None)
                    purchase_order.delete()
                except ProtectedError as exc:
                    raise serializers.ValidationError(
                        {
                            "purchase_orders": (
                                "A purchase order linked to other records cannot be deleted."
                            )
                        }
                    ) from exc

        if machines is not None:
            instance.machines.set(machines)

        return instance

    def get_evm_related_data(self, instance):
        end_date = instance.project_end_date if instance.project_end_date else date.today()
        start_date = instance.project_start_date
        all_action_items = ActionItem.objects.filter(
            Q(milestone__project=instance) | Q(parent_action_item__milestone__project=instance)
        )
        all_milestones = instance.milestones.all()
        all_ac_hours = all_action_items.aggregate(allocated_hours=Sum("allocated_time"))["allocated_hours"] or 0
        all_milestone_hours = all_milestones.aggregate(allocated_hours=Sum("allocated_time"))["allocated_hours"] or 0
        total_hours = all_ac_hours + all_milestone_hours
        project_hourly_rate = float(instance.contract_value / total_hours if total_hours else instance.user.staff.hourly_rate)
        response_data = {}
        prev_ev = 0
        prev_ac = 0
        prev_pv = 0
        while start_date <= end_date:
            _, last_day = monthrange(start_date.year, start_date.month)
            month_end = min(date(start_date.year, start_date.month, last_day), end_date)

            ac_for_this_month = all_action_items.filter(target_date__gte=start_date, target_date__lte=month_end)
            unfinished_ac = ac_for_this_month.exclude(status__iexact="closed")
            unfinished_ac_allocated_hours = float(ac_for_this_month.aggregate(unfinished_ac_allocated_hours=Sum("allocated_time"))["unfinished_ac_allocated_hours"] or 0)
            unfinished_ac_allocated_budget = float(ac_for_this_month.aggregate(unfinished_ac_allocated_budget=Sum("allocated_budget"))["unfinished_ac_allocated_budget"] or 0)
            
            closed_ac_allocated_hours = float(ac_for_this_month.filter(status__iexact="closed").aggregate(ac_allocated_hours=Sum('allocated_time'))['ac_allocated_hours'] or 0)
            
            unfinished_ac_booked_hours = float(TimeCard.objects.filter(
                Q(action_item__in=ac_for_this_month)
            ).aggregate(ac_booked_hours=Sum('hours'))['ac_booked_hours'] or 0)
            
            unfinished_ac_booked_cost = float(ExpenseCard.objects.filter(
                Q(action_item__in=ac_for_this_month)
            ).aggregate(ac_booked_cost=Sum('total'))['ac_booked_cost'] or 0)
            

            milestone_for_this_month = all_milestones.filter(due_date__gte=start_date, due_date__lte=month_end)
            unfinished_milestone = milestone_for_this_month.exclude(status__iexact="closed")

            milestone_allocated_hours = float(milestone_for_this_month.aggregate(milestone_allocated_hours=Sum("allocated_time"))["milestone_allocated_hours"] or 0)
            milestone_allocated_budget = float(milestone_for_this_month.aggregate(milestone_allocated_budget=Sum("allocated_budget"))["milestone_allocated_budget"] or 0)
            
            milestone_passed_allocated_hours = float(milestone_for_this_month.filter(status__iexact="closed").aggregate(milestone_allocated_hours=Sum("allocated_time"))["milestone_allocated_hours"] or 0)
            
            milestone_booked_hours = float(TimeCard.objects.filter(
                Q(milestone__in=milestone_for_this_month)
            ).aggregate(milestone_booked_hours=Sum('hours'))['milestone_booked_hours'] or 0)
            
            milestone_booked_cost = float(ExpenseCard.objects.filter(
                Q(milestone__in=milestone_for_this_month)
            ).aggregate(milestone_booked_cost=Sum('total'))['milestone_booked_cost'] or 0)

            people_cost = (milestone_booked_hours + unfinished_ac_booked_hours) * project_hourly_rate
            total_cost = unfinished_ac_booked_cost + milestone_booked_cost + people_cost

            ev = ((milestone_passed_allocated_hours + closed_ac_allocated_hours) * project_hourly_rate) + milestone_allocated_budget + unfinished_ac_allocated_budget
            ac = total_cost
            pv = ((unfinished_ac_allocated_hours + milestone_allocated_hours) * project_hourly_rate) + milestone_allocated_budget + unfinished_ac_allocated_budget

            ev += prev_ev
            ac += prev_ac
            pv += prev_pv

            prev_ev = ev
            prev_ac = ac
            prev_pv = pv
            
            sv = ev - pv
            cv = ev - ac
            spi = ev/pv if pv else 0
            cpi = ev / ac if ac else 0
            response_data[f"{start_date.strftime('%d-%m-%Y')}_{month_end.strftime('%d-%m-%Y')}"] = {
                "ev": ev,
                "ac": ac,
                "pv": pv,
                "sv": sv,
                "cv": cv,
                "spi": spi,
                "cpi": cpi,
            }
            start_date = month_end + timedelta(days=1)
        return response_data



    def calculate_action_item_progress(self, action_item):
        sub_items = action_item.sub_action_items.all()
        if sub_items.exists():
            closed = sub_items.filter(status__iexact="closed").count()
            total = sub_items.count()
            return closed / total if total else 0
        return 1.0 if action_item.status.lower() == "closed" else 0.0

    def calculate_milestone_progress(self, milestone):
        action_items = milestone.action_items.all()
        if not action_items.exists():
            return 1.0 if milestone.status.lower() == "closed" else 0.0
        progress_sum = 0
        for ai in action_items:
            progress_sum += self.calculate_action_item_progress(ai)
        return progress_sum / action_items.count() if action_items.count() else 0.0

    def calc_kpis(self, ev, pv, ac, bac):
        cpi = ev / ac if ac else 0
        spi = ev / pv if pv else 0
        eac = bac / cpi if cpi else 0
        cv = eac - bac
        return {
            "CPI": round(cpi, 2),
            "SPI": round(spi, 2),
            "EAC": round(eac, 2),
            "CV": round(cv, 2),
        }


    def get_new_evm_related_data(self, instance):
        """
        Calculates EVM metrics using both Calendar Progress (elapsed time) and Estimated Progress (from milestone/action item completion).
        Includes people cost in actual cost calculation with proper fallbacks.
        """
        try:
            milestones = instance.milestones.all().order_by('start_date')
            today = date.today()
            bac = sum(m.allocated_budget or 0 for m in milestones)  # Budget at Completion

            # Calculate total allocated hours for hourly rate with fallback
            all_action_items = []
            for m in milestones:
                all_action_items.extend(list(m.action_items.all()))
            all_ac_hours = sum(ai.allocated_time or 0 for ai in all_action_items)
            all_milestone_hours = sum(m.allocated_time or 0 for m in milestones)
            total_hours = all_ac_hours + all_milestone_hours
            project_hourly_rate = float(instance.contract_value / total_hours) if total_hours else 0

            calendar_progress_data = []
            estimated_progress_data = []

            for m in milestones:
                planned_cost = m.allocated_budget or 0
                start = m.start_date
                end = m.due_date

                # --- Calendar Progress (Elapsed Time) ---
                if start and end and start < end:
                    total_duration = (end - start).days
                    elapsed_duration = (min(today, end) - start).days
                    calendar_progress = max(0, min(1, elapsed_duration / total_duration))
                else:
                    calendar_progress = 0

                planned_value_calendar = planned_cost * calendar_progress
                earned_value_calendar = planned_value_calendar  # EV = PV for calendar progress

                calendar_progress_data.append({
                    "name": m.name or "Unnamed Milestone",
                    "planned_cost": planned_cost,
                    "calendar_progress": round(calendar_progress * 100, 2),
                    "planned_value": round(planned_value_calendar, 2),
                    "earned_value": round(earned_value_calendar, 2),
                })

                # --- Estimated Progress (from action items/sub items) ---
                estimated_progress = self.calculate_milestone_progress(m)
                planned_value_estimated = planned_cost * calendar_progress  # PV is still based on calendar progress
                earned_value_estimated = planned_cost * estimated_progress

                estimated_progress_data.append({
                    "name": m.name or "Unnamed Milestone",
                    "planned_cost": planned_cost,
                    "estimated_progress": round(estimated_progress * 100, 2),
                    "planned_value": round(planned_value_estimated, 2),
                    "earned_value": round(earned_value_estimated, 2),
                })

            total_planned_value_calendar = sum(m['planned_value'] for m in calendar_progress_data)
            total_earned_value_calendar = sum(m['earned_value'] for m in calendar_progress_data)
            total_planned_value_estimated = sum(m['planned_value'] for m in estimated_progress_data)
            total_earned_value_estimated = sum(m['earned_value'] for m in estimated_progress_data)

            # --- People Cost Calculation with fallbacks ---
            try:
                milestone_ids = [m.id for m in milestones]
                action_item_ids = [ai.id for ai in all_action_items]

                milestone_booked_hours = instance.time_cards.filter(
                    milestone_id__in=milestone_ids, status="approved"
                ).aggregate(total=Sum('hours'))['total'] or 0

                action_item_booked_hours = instance.time_cards.filter(
                    action_item_id__in=action_item_ids, status="approved"
                ).aggregate(total=Sum('hours'))['total'] or 0

                people_cost = (milestone_booked_hours + action_item_booked_hours) * project_hourly_rate
            except Exception:
                people_cost = 0

            # --- Expense Cost Calculation with fallback ---
            try:
                expense_cost = instance.expense_cards.filter(status='approved').aggregate(total=Sum('total'))['total'] or 0
            except Exception:
                expense_cost = 0

            # --- Actual Cost (AC) ---
            actual_cost = people_cost + expense_cost

            kpis_calendar = self.calc_kpis(total_earned_value_calendar, total_planned_value_calendar, actual_cost, bac)
            kpis_estimated = self.calc_kpis(total_earned_value_estimated, total_planned_value_estimated, actual_cost, bac)

            return {
                "calendar_progress": {
                    "phases": calendar_progress_data,
                    "total_planned_value": round(total_planned_value_calendar, 2),
                    "total_earned_value": round(total_earned_value_calendar, 2),
                    "actual_cost": round(actual_cost, 2),
                    "people_cost": round(people_cost, 2),
                    "expense_cost": round(expense_cost, 2),
                    "bac": round(bac, 2),
                    "kpis": kpis_calendar,
                },
                "estimated_progress": {
                    "phases": estimated_progress_data,
                    "total_planned_value": round(total_planned_value_estimated, 2),
                    "total_earned_value": round(total_earned_value_estimated, 2),
                    "actual_cost": round(actual_cost, 2),
                    "people_cost": round(people_cost, 2),
                    "expense_cost": round(expense_cost, 2),
                    "bac": round(bac, 2),
                    "kpis": kpis_estimated,
                }
            }
        except Exception as e:
            # Return empty structure with zeros if any error occurs
            return {
                "calendar_progress": {
                    "phases": [],
                    "total_planned_value": 0,
                    "total_earned_value": 0,
                    "actual_cost": 0,
                    "people_cost": 0,
                    "expense_cost": 0,
                    "bac": 0,
                    "kpis": {"CPI": 0, "SPI": 0, "EAC": 0, "CV": 0},
                },
                "estimated_progress": {
                    "phases": [],
                    "total_planned_value": 0,
                    "total_earned_value": 0,
                    "actual_cost": 0,
                    "people_cost": 0,
                    "expense_cost": 0,
                    "bac": 0,
                    "kpis": {"CPI": 0, "SPI": 0, "EAC": 0, "CV": 0},
                }
            }


    def get_allocated_hours(self, instance):
        action_items = ActionItem.objects.filter(
            Q(milestone__project=instance) | Q(parent_action_item__milestone__project=instance)
        ).distinct()
        return action_items.aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0

    def get_evm_timeseries_forecast(self, instance):
        """
        Returns two separate time series forecasts: calendar_progress and estimated_progress.
        Calendar progress: EV = PV (time-based view)
        Estimated progress: EV based on actual completion (work-based view)
        """
        milestones = instance.milestones.all().order_by('due_date')
        today = date.today()
        
        # Calculate total allocated hours for hourly rate (same as new EVM)
        all_action_items = []
        for m in milestones:
            all_action_items.extend(list(m.action_items.all()))
        all_ac_hours = sum(ai.allocated_time or 0 for ai in all_action_items)
        all_milestone_hours = sum(m.allocated_time or 0 for m in milestones)
        total_hours = all_ac_hours + all_milestone_hours
        project_hourly_rate = float(instance.contract_value / total_hours) if total_hours else 0

        # Get project start and end dates with fallback
        if not milestones:
            return {
                "calendar_progress": [],
                "estimated_progress": []
            }
        
        try:
            project_start = min(m.start_date for m in milestones if m.start_date)
            project_end = max(m.due_date for m in milestones if m.due_date)
        except ValueError:
            # Fallback if no valid dates
            return {
                "calendar_progress": [],
                "estimated_progress": []
            }
        
        # Create data points at regular intervals (weekly)
        calendar_data_points = []
        estimated_data_points = []
        current_date = project_start
        
        while current_date <= project_end:
            cumulative_pv_calendar = 0
            cumulative_ev_calendar = 0
            cumulative_ac_calendar = 0
            
            cumulative_pv_estimated = 0
            cumulative_ev_estimated = 0
            cumulative_ac_estimated = 0
            
            for m in milestones:
                planned_cost = m.allocated_budget or 0
                start = m.start_date
                end = m.due_date
                
                # Calculate calendar progress up to current_date
                if start and end and start < end:
                    total_duration = (end - start).days
                    elapsed_duration = (min(current_date, end) - start).days
                    calendar_progress = max(0, min(1, elapsed_duration / total_duration))
                else:
                    calendar_progress = 0
                
                # PV: based on calendar progress up to current_date
                planned_value = planned_cost * calendar_progress
                
                # Calendar Progress: EV = PV (time-based view)
                earned_value_calendar = planned_value
                
                # Estimated Progress: EV based on actual completion
                if current_date >= start:
                    estimated_progress = self.calculate_milestone_progress(m)
                    earned_value_estimated = planned_cost * estimated_progress
                else:
                    earned_value_estimated = 0
                
                # AC calculation (same for both)
                ac = self._calculate_milestone_actual_cost_up_to_date(instance, m, current_date, project_hourly_rate)
                
                # Calendar progress cumulative
                cumulative_pv_calendar += planned_value
                cumulative_ev_calendar += earned_value_calendar
                cumulative_ac_calendar += ac
                
                # Estimated progress cumulative
                cumulative_pv_estimated += planned_value
                cumulative_ev_estimated += earned_value_estimated
                cumulative_ac_estimated += ac

            calendar_data_points.append({
                "date": current_date,
                "pv": round(cumulative_pv_calendar, 2),
                "ev": round(cumulative_ev_calendar, 2),
                "ac": round(cumulative_ac_calendar, 2),
            })
            
            estimated_data_points.append({
                "date": current_date,
                "pv": round(cumulative_pv_estimated, 2),
                "ev": round(cumulative_ev_estimated, 2),
                "ac": round(cumulative_ac_estimated, 2),
            })
            
            # Move to next week
            current_date += timedelta(days=7)
        
        return {
            "calendar_progress": calendar_data_points,
            "estimated_progress": estimated_data_points
        }

    def _calculate_milestone_actual_cost_up_to_date(self, instance, milestone, target_date, project_hourly_rate):
        """
        Calculate actual cost for a milestone up to a specific date with proper fallbacks.
        """
        try:
            # People cost for this milestone
            milestone_booked_hours = instance.time_cards.filter(
                milestone_id=milestone.id, status="approved", date__lte=target_date
            ).aggregate(total=Sum('hours'))['total'] or 0
            
            action_item_ids = [ai.id for ai in milestone.action_items.all()]
            action_item_booked_hours = instance.time_cards.filter(
                action_item_id__in=action_item_ids, status="approved", date__lte=target_date
            ).aggregate(total=Sum('hours'))['total'] or 0
            
            people_cost = (milestone_booked_hours + action_item_booked_hours) * project_hourly_rate
            
            # Expense cost for this milestone
            expense_cost = instance.expense_cards.filter(
                milestone_id=milestone.id, status='approved', expense_date__lte=target_date
            ).aggregate(total=Sum('total'))['total'] or 0
            expense_cost += instance.expense_cards.filter(
                action_item_id__in=action_item_ids, status='approved', expense_date__lte=target_date
            ).aggregate(total=Sum('total'))['total'] or 0
            
            return people_cost + expense_cost
        except Exception:
            return 0

    def _get_project_company_roles(self, instance):
        """
        Get the roles for the project company.
        """
        filters = Q(type="solution", scope="project")
        if instance.user.is_freelancer:
            filters &= Q(freelancer=instance.user)
        else:
            filters &= Q(company=instance.user.company)
        roles = Role.objects.filter(filters)

        return roles

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        open_milestones = instance.milestones.exclude(status="closed").order_by('due_date')
        if instance.user:
            representation["user"] = {
                "id": instance.user.id,
                "name": instance.user.full_name,
                "company": instance.user.company.id if not instance.user.is_freelancer else None,
                "user_image": instance.user.user_image,
            }
        if open_milestones.exists():
            representation['next_milestone'] = {
                "name": open_milestones[0].name,
                "colour": open_milestones[0].colour,
                "due_date": open_milestones[0].due_date
            }
        else:
            representation['next_milestone'] = None
        representation["project_progress"] = round(calculate_project_progress(instance), 2)
        action_items = ActionItem.objects.filter(
            Q(milestone__project=instance) | Q(parent_action_item__milestone__project=instance)
        )

        # representation["evm"] = self.get_evm_related_data(instance)
        # representation["new_evm"] = self.get_new_evm_related_data(instance)
        # representation["evm_timeseries_forecast"] = self.get_evm_timeseries_forecast(instance)
        representation["allocated_hours"] = self.get_allocated_hours(instance)
        visible_purchase_orders = self._purchase_order_view_queryset(
            instance.purchase_orders.all()
        )
        representation["purchase_orders"] = PurchaseOrderSerializer(
            visible_purchase_orders,
            many=True,
            context=self.context,
        ).data
        representation["project_company_roles"] = [{
            "id": role.id,
            "name": role.name,
            "description": role.description,
            "is_default": role.is_default,
            "is_active": role.is_active
        } for role in self._get_project_company_roles(instance)]
        warnings = getattr(self, "_response_warnings", None)
        if warnings:
            representation["warnings"] = warnings
        return representation
