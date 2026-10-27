from rest_framework import serializers
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from project_management.models import (
    Contact, ContactUser, ContactCompany,
    ECPMPConfig, Project, ProjectExternalUsers, ProjectTeamMembers
)
from .time_expense import get_valid_choices
from models.models import User
from companyprofileapis.services.area_of_expertise import (
    SubAreaOfExpertiseListField,
    get_company_config_for_request,
    validate_area_of_expertise_fields,
)
from project_management.utils import (
    get_contact_project_ids,
    get_sharemundo_user,
    get_sharemundo_company,
)
from chat.utils import can_open_direct_chat


def get_allowed_company_roles_for_request(request):
    company_config = get_company_config_for_request(request)

    return list(company_config.company_roles or []) if company_config else []


def validate_contact_area_of_expertise(attrs, *, instance=None, request=None):
    company_config = get_company_config_for_request(request)
    return validate_area_of_expertise_fields(
        attrs,
        instance=instance,
        company_config=company_config,
        allow_multiple_sub_areas=True,
    )


def resolve_external_contact_role(project_data, valid_choices, existing_role=None):
    requested_role = str(project_data.get("role") or "").strip()
    if requested_role:
        if requested_role not in valid_choices:
            raise serializers.ValidationError(
                "Invalid External User Role. Allowed choices are: "
                f"{', '.join(valid_choices)}"
            )
        return requested_role

    if existing_role and existing_role in valid_choices:
        return existing_role

    if "consultant" in valid_choices:
        return "consultant"

    raise serializers.ValidationError(
        "An External User Role is required. Allowed choices are: "
        f"{', '.join(valid_choices)}"
    )


def resolve_external_contact_projects(
    projects_data,
    request,
    *,
    contact_user=None,
    contact_company=None,
):
    resolved_projects = []
    for project_data in projects_data:
        project_id = project_data.get("project_id")
        try:
            project = Project.objects.get(id=project_id)
        except Project.DoesNotExist:
            raise ValidationError(f"Project with id {project_id} does not exist.")

        existing_link = None
        if contact_user is not None:
            existing_link = ProjectExternalUsers.objects.filter(
                project=project,
                contact_user=contact_user,
            ).first()
        elif contact_company is not None:
            existing_link = ProjectExternalUsers.objects.filter(
                project=project,
                contact_company=contact_company,
            ).first()

        valid_choices = get_valid_choices(
            "team_members_roles",
            project_id=project_id,
            request=request,
        )
        role = resolve_external_contact_role(
            project_data,
            valid_choices,
            existing_role=existing_link.role if existing_link else None,
        )
        resolved_projects.append((project, role))

    return resolved_projects


class ProjectExternalUserNestedSerializer(serializers.Serializer):
    project_id = serializers.IntegerField(write_only=True, required=False)
    role = serializers.CharField(max_length=255, write_only=True, required=False)


class CrmOwnerFieldMixin:
    """CRM-1B: the optional "Responsable" must be a user of the requester's own tenant."""

    def validate_owner(self, value):
        if value is None:
            return value
        from project_management.crm_tenancy import tenant_of_user, user_belongs_to_tenant

        request = self.context.get("request")
        tenant = tenant_of_user(getattr(request, "user", None)) if request else None
        if tenant is None or not user_belongs_to_tenant(value, tenant):
            raise serializers.ValidationError("The owner must belong to your company.")
        return value

    @staticmethod
    def owner_summary(instance):
        owner = instance.owner
        if owner is None:
            return None
        return {"id": owner.id, "name": getattr(owner, "full_name", None) or owner.email}


class ContactUserSerializer(CrmOwnerFieldMixin, serializers.ModelSerializer):
    projects = ProjectExternalUserNestedSerializer(
        many=True, write_only=True, required=False,
    )
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    sub_area_of_expertise = SubAreaOfExpertiseListField(required=False)

    class Meta:
        model = ContactUser
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        contact_company = attrs.get("contact_company")
        if contact_company is not None and request is not None:
            # SEC-02: a contact can only be attached to a company of the requester's own tenant.
            user = request.user
            owned = (
                contact_company.user_id == user.id
                if user.is_freelancer
                else user.company_id is not None and contact_company.company_profile_id == user.company_id
            )
            if not owned:
                raise serializers.ValidationError({"contact_company": "Company not found."})
        return validate_contact_area_of_expertise(
            attrs,
            instance=self.instance,
            request=request,
        )

    def _sync_contact_company(self, validated_data):
        request = self.context.get("request")
        company_profile = validated_data.get("company_profile")
        if company_profile is None and request and getattr(request.user, "company", None):
            company_profile = request.user.company

        contact_company = validated_data.get("contact_company")
        if contact_company:
            validated_data["company_name"] = contact_company.company_name
            return validated_data

        company_name = (validated_data.get("company_name") or "").strip()
        if not company_name:
            validated_data["contact_company"] = None
            return validated_data

        company_qs = ContactCompany.objects.filter(
            company_name__iexact=company_name,
            is_deleted=False,
        )
        if company_profile:
            company_qs = company_qs.filter(company_profile=company_profile)
        elif request and getattr(request.user, "is_freelancer", False):
            company_qs = company_qs.filter(user=request.user)

        matched_company = company_qs.first()
        if matched_company:
            validated_data["contact_company"] = matched_company
            validated_data["company_name"] = matched_company.company_name
        return validated_data

    @transaction.atomic
    def create(self, validated_data):
        projects_data = validated_data.pop('projects', [])
        request = self.context["request"]
        validated_data["user"] = request.user if request.user.is_freelancer else None
        validated_data["company_profile"] = request.user.company if request.user.company else None
        self._sync_contact_company(validated_data)
        resolved_projects = resolve_external_contact_projects(
            projects_data,
            request,
        )
        contact = super().create(validated_data)
        for project, role in resolved_projects:
            ProjectExternalUsers.objects.create(
                project=project,
                role=role,
                contact_user=contact,
            )
        return contact

    @transaction.atomic
    def update(self, instance, validated_data):
        projects_data = validated_data.pop('projects', None)
        request = self.context.get('request')
        resolved_projects = None
        if projects_data is not None:
            resolved_projects = resolve_external_contact_projects(
                projects_data,
                request,
                contact_user=instance,
            )
        if "is_deleted" in validated_data:
            validated_data["deleted_at"] = timezone.now() if validated_data["is_deleted"] else None
        if "contact_company" in validated_data or "company_name" in validated_data:
            self._sync_contact_company(validated_data)
        contact = super().update(instance, validated_data)
        project_list = []
        if resolved_projects is not None:
            for project, role in resolved_projects:
                ProjectExternalUsers.objects.update_or_create(
                    project=project,
                    contact_user=contact,
                    defaults={'role': role}
                )
                project_list.append(project.id)
            ProjectExternalUsers.objects.filter(
                contact_user=contact
            ).exclude(project__id__in=project_list).delete()
        return contact

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        sharemundo_user = get_sharemundo_user(instance.email)
        request = self.context.get("request")
        request_user = getattr(request, "user", None)
        representation['is_sharemundo_user'] = True if sharemundo_user else False
        representation['user_type'] = getattr(sharemundo_user, 'user_type', None)
        representation['sharemundo_user_id'] = sharemundo_user.id if sharemundo_user else None
        representation['direct_chat_user_id'] = sharemundo_user.id if sharemundo_user else None
        representation['can_start_chat'] = bool(
            sharemundo_user
            and request_user
            and getattr(request_user, "is_authenticated", False)
            and can_open_direct_chat(request_user, sharemundo_user)
        )
        if sharemundo_user and sharemundo_user.company:
            representation['company_sharemundo_links_id'] = sharemundo_user.company.sharemundo_links_id
            representation['company_admin_email'] = sharemundo_user.company.admin_user.email
            representation['company_id'] = sharemundo_user.company.id
        else:
            representation['company_sharemundo_links_id'] = None
            representation['company_admin_email'] = None
            representation['company_id'] = None

        company_contact_email = None
        if instance.company_name and instance.company_profile_id:
            contact_company = ContactCompany.objects.filter(
                company_profile_id=instance.company_profile_id,
                company_name__iexact=instance.company_name.strip(),
                is_deleted=False,
            ).first()
            if contact_company:
                company_contact_email = contact_company.contact_email
        representation['company_contact_email'] = company_contact_email

        projects_qs = ProjectExternalUsers.objects.filter(contact_user=instance).values_list('id', 'role', 'project__id', 'project__project_name')
        representation['availability'] = not ProjectExternalUsers.objects.filter(
            contact_company__isnull=True,
            contact_user=instance
        ).exists()
        representation['projects'] = [
            {
                'external_user_id': ext_id,
                'role': role,
                'project': {
                    'id': proj_id,
                    'name': proj_name,
                }
            }
            for ext_id, role, proj_id, proj_name in projects_qs
        ]
        representation["owner_detail"] = self.owner_summary(instance)
        return representation


class ContactCompanySerializer(CrmOwnerFieldMixin, serializers.ModelSerializer):
    projects = ProjectExternalUserNestedSerializer(
        many=True, write_only=True, required=False,
    )
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    sub_area_of_expertise = SubAreaOfExpertiseListField(required=False)

    class Meta:
        model = ContactCompany
        fields = "__all__"

    def validate(self, attrs):
        attrs = super().validate(attrs)
        return validate_contact_area_of_expertise(
            attrs,
            instance=self.instance,
            request=self.context.get("request"),
        )

    def validate_company_role(self, value):
        normalized_value = (value or "").strip()
        if not normalized_value:
            return normalized_value or value

        request = self.context.get("request")
        allowed_roles = get_allowed_company_roles_for_request(request)
        if not allowed_roles:
            raise serializers.ValidationError(
                "Company role configuration is not available."
            )
        if normalized_value not in allowed_roles:
            raise serializers.ValidationError(
                f"Invalid Company Role. Allowed choices are: {', '.join(allowed_roles)}"
            )
        return normalized_value

    @transaction.atomic
    def create(self, validated_data):
        projects_data = validated_data.pop('projects', [])
        request = self.context["request"]
        validated_data["user"] = request.user if request.user.is_freelancer else None
        validated_data["company_profile"] = request.user.company if request.user.company else None
        resolved_projects = resolve_external_contact_projects(
            projects_data,
            request,
        )
        contact = super().create(validated_data)
        for project, role in resolved_projects:
            ProjectExternalUsers.objects.create(
                project=project,
                contact_company=contact,
                role=role,
            )
        return contact

    @transaction.atomic
    def update(self, instance, validated_data):
        projects_data = validated_data.pop('projects', None)
        request = self.context.get('request')
        resolved_projects = None
        if projects_data is not None:
            resolved_projects = resolve_external_contact_projects(
                projects_data,
                request,
                contact_company=instance,
            )
        if 'is_deleted' in validated_data:
            validated_data['deleted_at'] = timezone.now() if validated_data['is_deleted'] else None
        contact = super().update(instance, validated_data)
        project_list = []
        if resolved_projects is not None:
            for project, role in resolved_projects:
                ProjectExternalUsers.objects.update_or_create(
                    project=project,
                    contact_company=contact,
                    defaults={'role': role}
                )
                project_list.append(project.id)
            ProjectExternalUsers.objects.filter(
                contact_company=contact
            ).exclude(project__id__in=project_list).delete()
        return contact
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get("request")
        request_user = getattr(request, "user", None)
        sharemundo_company = get_sharemundo_company(instance.contact_email)
        target_user = getattr(sharemundo_company, "admin_user", None) if sharemundo_company else None
        representation['is_sharemundo_company'] = True if sharemundo_company else False
        representation['direct_chat_user_id'] = target_user.id if target_user else None
        representation['can_start_chat'] = bool(
            target_user
            and request_user
            and getattr(request_user, "is_authenticated", False)
            and can_open_direct_chat(request_user, target_user)
        )
        linked_contact_users_count = getattr(instance, "linked_contact_users_count", None)
        if linked_contact_users_count is None:
            linked_contact_users_count = instance.linked_contact_users.filter(
                is_deleted=False
            ).count()
        representation["has_contacts"] = linked_contact_users_count > 0
        projects_qs = ProjectExternalUsers.objects.filter(contact_company=instance).values_list('id', 'role', 'project__id', 'project__project_name')
        representation['availability'] = not ProjectExternalUsers.objects.filter(
            contact_user__isnull=True,
            contact_company=instance
        ).exists()
        representation['projects'] = [
            {
                'external_user_id': ext_id,
                'role': role,
                'project': {
                    'id': proj_id,
                    'name': proj_name,
                }
            }
            for ext_id, role, proj_id, proj_name in projects_qs
        ]
        representation["owner_detail"] = self.owner_summary(instance)
        return representation

class ContactHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Contact.history.model
        fields = [
            'id',
            'history_date',
            'history_change_reason',
            'history_user',
            'name',
            'surname',
            'address_line_1',
            'postcode',
            'town'
        ]
        read_only_fields = fields

class ContactSerialier(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(), 
        required=True
    )

    history = serializers.SerializerMethodField()

    class Meta:
        model = Contact
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['contact_type'].choices = self.get_contact_type_choices(self.context.get('user'))

    def validate_user(self, value):
        if value != self.context.get('user'):
            raise serializers.ValidationError(f"Invalid user id")
        return value

    def get_history(self, obj):
        include_history = self.context.get('include_history', True)
        if not include_history:
            return None
        history = obj.history.all()
        # Serialize the historical records
        serializer = ContactHistorySerializer(history, many=True)
        return serializer.data

    def get_contact_type_choices(self, user):
        if user and user.is_freelancer:
            config_contact_types = GlobalConfig.objects.filter(user=user).values_list("contact_type", flat=True)

        elif user and user.company:
            config_contact_types = GlobalConfig.objects.filter(company_profile=user.company).values_list("contact_type", flat=True)

        else:
            # extra check to avoid exception:
            config_contact_types = ECPMPConfig.objects.values_list("contact_type", flat=True)

        return list(set([contact for contact_types in config_contact_types for contact in contact_types]))

    def validate_contact_type(self, value):
        valid_choices = self.get_contact_type_choices(self.context.get('user'))
        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid contact type. Allowed choices are: {', '.join(valid_choices)}")
        
        if self.instance and self.instance.contact_type == 'client' and value != 'client':
            linked_projects = Project.objects.filter(client=self.instance)
            if linked_projects.exists():
                raise serializers.ValidationError("Cannot change contact type from 'client' to another type as it is assigned as a client in one or more projects.")
        
        return value

    # def validate_contact_projects(self, value):
    #     """Ensure that the user has access to all the provided projects."""
    #     user = self.context.get('user')
    #     unauthorized_projects = []
    #     for project in value:
    #         if not project.company_profile.consultant_user == user:
    #             unauthorized_projects.append(project.project_name)
    #     if unauthorized_projects:
    #         raise serializers.ValidationError(
    #             f"You do not have access to the following projects: {', '.join(unauthorized_projects)}"
    #         )

    #     return value
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['contact_projects'] = get_contact_project_ids(instance)

        return representation


class CSVUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
