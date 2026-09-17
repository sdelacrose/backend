from rest_framework import serializers
from models.models import Role

from project_management.models import (
    ProjectTeamMembers, ProjectExternalUsers, ProjectClient
)
from project_management.serializers import ContactCompanySerializer, ContactUserSerializer

class ProjectTeamSerializer(serializers.ModelSerializer):
    project_role = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.all(),
        required=False,
        allow_null=True,
        help_text="Role ID for the team member in this project",
        error_messages={
            'does_not_exist': 'Role with ID {pk_value} does not exist.',
            'invalid': 'Invalid role ID provided.'
        }
    )

    class Meta:
        model = ProjectTeamMembers
        fields = ['id', 'role', 'project_role', 'team_member', 'project_access', 'created_at', 'updated_at']

    def validate_project_role(self, value):
        """
        Validate that the role belongs to the same company as the project
        and is a project-scoped role
        """
        if value:
            # Get the project from context
            project = self.context.get('project')
            if project:
                # Check if role belongs to the same company as the project
                if project.company_profile and value.company != project.company_profile:
                    raise serializers.ValidationError(
                        "Role must belong to the same company as the project"
                    )
                
                # Check if role is project-scoped
                if value.scope != 'project':
                    raise serializers.ValidationError(
                        "Only project-scoped roles can be assigned to project team members"
                    )
                
                # Check if role is solution type
                if value.type != 'solution':
                    raise serializers.ValidationError(
                        "Only solution-type roles can be assigned to project team members"
                    )
            else:
                # For new projects, validate against request user's company
                request = self.context.get('request')
                if request and request.user and not request.user.is_freelancer:
                    if value.company != request.user.company:
                        raise serializers.ValidationError(
                            "Role must belong to the same company as the project"
                        )
                
                # Check if role is project-scoped
                if value.scope != 'project':
                    raise serializers.ValidationError(
                        "Only project-scoped roles can be assigned to project team members"
                    )
                
                # Check if role is solution type
                if value.type != 'solution':
                    raise serializers.ValidationError(
                        "Only solution-type roles can be assigned to project team members"
                    )
        return value



    def to_representation(self, instance):
        representation = super().to_representation(instance)
        # Customize member representation here
        if instance.team_member:
            representation['team_member'] = {
                "id": instance.team_member.id,
                "first_name": instance.team_member.first_name,
                "last_name": instance.team_member.last_name,
                "email": instance.team_member.email,
                "mobile_phone": instance.team_member.mobile_phone,
                "professional_headline": instance.team_member.professional_headline,
            }
            if not instance.team_member.is_freelancer:
                representation['team_member']["company_name"] = instance.team_member.company.company_name
        
        # Add role details if project_role is set and exists
        try:
            if instance.project_role:
                representation['project_role_details'] = {
                    "id": instance.project_role.id,
                    "name": instance.project_role.name,
                    "description": instance.project_role.description,
                    "scope": instance.project_role.scope,
                    "type": instance.project_role.type
                }
        except Role.DoesNotExist:
            # Handle case where project_role references a non-existent Role
            representation['project_role_details'] = None
            # Optionally, you could also set the project_role field to None here
            # instance.project_role = None
            # instance.save()
        
        return representation

class ProjectExternalUserSerializer(serializers.ModelSerializer):
    project_role = serializers.PrimaryKeyRelatedField(
        queryset=Role.objects.all(),
        required=False,
        allow_null=True,
        help_text="Role ID for the external user in this project",
        error_messages={
            'does_not_exist': 'Role with ID {pk_value} does not exist.',
            'invalid': 'Invalid role ID provided.'
        }
    )

    class Meta:
        model = ProjectExternalUsers
        fields = ['id', 'role', 'project_role', 'contact_user', 'contact_company', 'created_at', 'updated_at']
    
    def validate_project_role(self, value):
        """
        Validate that the role belongs to the same company as the project
        and is a project-scoped role
        """
        if value:
            # Get the project from context
            project = self.context.get('project')
            if project:
                # Check if role belongs to the same company as the project
                if project.company_profile and value.company != project.company_profile:
                    raise serializers.ValidationError(
                        "Role must belong to the same company as the project"
                    )
                
                # Check if role is project-scoped
                if value.scope != 'project':
                    raise serializers.ValidationError(
                        "Only project-scoped roles can be assigned to external users"
                    )
                
                # Check if role is solution type
                if value.type != 'solution':
                    raise serializers.ValidationError(
                        "Only solution-type roles can be assigned to external users"
                    )
            else:
                # For new projects, validate against request user's company
                request = self.context.get('request')
                if request and request.user and not request.user.is_freelancer:
                    if value.company != request.user.company:
                        raise serializers.ValidationError(
                            "Role must belong to the same company as the project"
                        )
                
                # Check if role is project-scoped
                if value.scope != 'project':
                    raise serializers.ValidationError(
                        "Only project-scoped roles can be assigned to external users"
                    )
                
                # Check if role is solution type
                if value.type != 'solution':
                    raise serializers.ValidationError(
                        "Only solution-type roles can be assigned to external users"
                    )
        return value

    def validate(self, data):
        contact_user = data.get('contact_user')
        contact_company = data.get('contact_company')

        if contact_user and contact_company:
            raise serializers.ValidationError("Only one of 'contact_user' or 'contact_company' can be provided.")
        if not contact_user and not contact_company:
            raise serializers.ValidationError("At least one of 'contact_user' or 'contact_company' must be provided.")

        return data

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        # Customize contact_user representation here
        if instance.contact_user:
            representation['contact_user'] = {
                "id": instance.contact_user.id,
                "name": instance.contact_user.name,
                "surname": instance.contact_user.surname,
                "company_name": instance.contact_user.company_name
            }
        if instance.contact_company:
            representation['contact_company'] = {
                "id": instance.contact_company.id,
                "company_name": instance.contact_company.company_name,
                "contact_name": instance.contact_company.contact_name,
                "contact_surname": instance.contact_company.contact_surname
            }
        
        # Add role details if project_role is set and exists
        try:
            if instance.project_role:
                representation['project_role_details'] = {
                    "id": instance.project_role.id,
                    "name": instance.project_role.name,
                    "description": instance.project_role.description,
                    "scope": instance.project_role.scope,
                    "type": instance.project_role.type
                }
        except Role.DoesNotExist:
            # Handle case where project_role references a non-existent Role
            representation['project_role_details'] = None
        
        return representation


class ProjectClientSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectClient
        fields = ['id', 'contact_user', 'contact_company']

    def validate(self, data):
        contact_user = data.get('contact_user')
        contact_company = data.get('contact_company')

        if contact_user and contact_company:
            raise serializers.ValidationError("A project client can only be either a ContactUser or a ContactCompany.")
        if not contact_user and not contact_company:
            raise serializers.ValidationError("A project client must have at least one contact set.")

        return data

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        is_funding_screen = self.context.get("is_funding_screen", False)
        if instance.contact_user:
            if is_funding_screen:
                representation['contact_user'] = ContactUserSerializer(instance.contact_user).data
            else:
                representation['contact_user'] = {
                    "id": instance.contact_user.id,
                    "name": instance.contact_user.name,
                    "surname": instance.contact_user.surname,
                    "email": instance.contact_user.email,
                    "telephone_no": instance.contact_user.telephone_no,
                    "company_name": instance.contact_user.company_name
                }
        if instance.contact_company:
            if is_funding_screen:
                representation['contact_company'] = ContactCompanySerializer(instance.contact_company).data
            else:
                representation['contact_company'] = {
                    "id": instance.contact_company.id,
                    "company_name": instance.contact_company.company_name,
                    "contact_name": instance.contact_company.contact_name,
                    "contact_email": instance.contact_company.contact_email,
                    "company_tel_no": instance.contact_company.company_tel_no,
                    "contact_surname": instance.contact_company.contact_surname
                }
        return representation
