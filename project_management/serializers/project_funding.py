from rest_framework import serializers

from project_management.models import (
    ProjectFunding
)
from project_management.models.constants import (
    COLLABORATIVE_SOLUTION
)
from project_management.serializers import DocumentSerializer
from project_management.utils import get_product_name_from_request

class ProjectFundingSerializer(serializers.ModelSerializer):
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = ProjectFunding
        fields = '__all__'

    # TODO: Adjust as per new permission system
    # def validate_project(self, value):
    #     request_user = self.context.get("request").user
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)

    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]

    #     if value.user == request_user and value.solution in products:
    #         return value
        
    #     if request_user.is_company_admin:
    #         if value.user.company == request_user.company:
    #             return value

    #     # Check if the request_user is a project manager of the project
    #     team_member = value.team_members.filter(team_member=request_user, role="project manager", project_access="RW").first()
    #     if team_member:
    #         return value
        
    #     raise serializers.ValidationError(f"Project {value} does not belong to this user")

    def validate_documents(self, documents):
        project = self.instance.project.id if self.instance else int(self.initial_data.get('project'))

        for document in documents:
            if document.folder.project.id != project:
                raise serializers.ValidationError(
                    f"Document {document.file_name} is not linked to the same project as the object."
                )
        return documents

    def create(self, validated_data):
        request_user = self.context['request'].user
        validated_data['created_by'] = request_user

        return super().create(validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.documents:
            representation["documents"] = DocumentSerializer(instance.documents, many=True).data
        return representation
