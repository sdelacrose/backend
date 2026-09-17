from rest_framework import serializers
from models.models import User, CompanyProfile, Feedback, FeedbackResponse
from project_management.serializers import DocumentSerializer
from project_management.models import Document, Project
from project_management.utils import get_product_name_from_request
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied
from django.core.exceptions import ObjectDoesNotExist

class FeedbackResponseSerializer(serializers.ModelSerializer):
    
    class Meta:
        model = FeedbackResponse
        fields = ['id', 'response', 'responded_by', 'created_at', 'updated_at']
        read_only_fields = ['responded_by', 'created_at', 'updated_at']

    def create(self, validated_data):
        request = self.context.get('request')
        validated_data['responded_by'] = request.user
        return super().create(validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.responded_by:
            representation["responded_by"] = {
                "id": instance.responded_by.id,
                "name": instance.responded_by.full_name,
            }
        return representation

class FeedbackSerializer(serializers.ModelSerializer):
    solution_name = serializers.CharField(read_only=True)
    responses = FeedbackResponseSerializer(many=True, read_only=True)
    
    class Meta:
        model = Feedback
        fields = [
            'id', 'company_profile', 'solution_name', 'user', 'freelancer',
            'project', 'project_name', 'project_team_role', 'feedback_type', 'description',
            'status', 'answer_required', 'created_at', 'updated_at', 'attachments', 
            'responses'
        ]
        read_only_fields = ['created_at', 'updated_at', 'response_date', 'solution_name' ,'company_profile', 'user', 'freelancer']

    def validate_project(self, value):
        """Validate that the project ID exists and user has access to it"""
        if value is not None:
            try:
                project = Project.objects.get(id=value)
                request = self.context.get('request')
                if request and hasattr(request, 'user'):
                    user = request.user
                    # Check if user has access to this project
                    if not (project.user == user or 
                           project.team_members.filter(team_member=user).exists() or
                           (user.company and project.company_profile == user.company)):
                        raise serializers.ValidationError("You don't have access to this project.")
                return value
            except ObjectDoesNotExist:
                raise serializers.ValidationError("Project with this ID does not exist.")
        return value

    def create(self, validated_data):
        request = self.context.get('request')
        request_user = request.user

        if not request_user.is_freelancer:
            validated_data['company_profile'] = request_user.company
        else:
            validated_data['freelancer'] = request_user
        validated_data['user'] = request_user
        
        solution_name = get_product_name_from_request(request)
        if solution_name:
            validated_data['solution_name'] = solution_name
        
        # Handle project team role assignment
        project_id = validated_data.get('project')
        if project_id:
            try:
                project = Project.objects.get(id=project_id)
                team_member = project.team_members.filter(team_member=request_user).first()
                validated_data['project_team_role'] = team_member.role if team_member else None
                validated_data['project_name'] = project.project_name
            except ObjectDoesNotExist:
                validated_data['project_team_role'] = None
        
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request = self.context.get('request')
        request_user = request.user
        
        if not request_user.is_superuser:
            raise PermissionDenied(detail="Only super admin can reply to feedbacks.") 
        return super().update(instance, validated_data)
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.user:
            representation["user"] = {
                "id": instance.user.id,
                "name": instance.user.full_name,
            }
        if instance.company_profile:
            representation["company_profile"] = {
                "id": instance.company_profile.id,
                "name": instance.company_profile.company_name,
            }
        if instance.project:
            representation["project"] = {
                "id": instance.project,
                "name": instance.project_name,
            }
        if instance.freelancer:
            representation["freelancer"] = {
                "id": instance.freelancer.id,
                "name": instance.freelancer.full_name,
            }
        return representation
    
