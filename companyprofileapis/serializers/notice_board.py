from rest_framework import serializers
from django.utils.timezone import now

from companyprofileapis.models import NoticeBoard

class NoticeBoardSerializer(serializers.ModelSerializer):
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = NoticeBoard
        fields = "__all__"

    def validate(self, data):
        # Permission checks are handled by the permission classes
        # This method should only handle business logic validation
        return data

    def create(self, validated_data):
        # Set freelancer or company_profile based on the request user
        request_user = self.context["request"].user

        if request_user.is_freelancer:
            validated_data["freelancer"] = request_user
        else:
            validated_data["company_profile"] = request_user.company

        # Create the offer
        offer = super().create(validated_data)
        return offer
