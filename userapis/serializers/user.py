from rest_framework import serializers

from userapis.models import UserDashboardConfig
from models.models import User

class UserDashboardConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserDashboardConfig
        fields = ['id', 'project_dashboard', 'company_dashboard', 'personal_dashboard']


class PublicUserSerializer(serializers.ModelSerializer):
    company = serializers.CharField(source='company.company_name', allow_null=True)
    company_id = serializers.IntegerField(
        source='company.id', allow_null=True, read_only=True
    )
    company_sharemundo_links_id = serializers.CharField(
        source='company.sharemundo_links_id', allow_null=True, read_only=True
    )
    company_admin_email = serializers.EmailField(
        source='company.admin_user.email', allow_null=True, read_only=True
    )

    class Meta:
        model = User
        fields = (
            'id', 'username', 'title',
            'first_name', 'last_name', 'job_title', 'city', 'state',
            'country', 'preferred_language', 'spoken_languages', 'user_type', 'dob',
            'professional_headline', 'industry', 'current_position',
            'qualifications', 'keywords', 'availability_from', 'availability_until',
            'availability_type', 'availability_location', 'availability_hours_per_week',
            'open_to_new_challenges', 'user_image', "user_searchable", "company", "email", "mobile_phone",
            "sharemundo_links_id", "company_id", "company_sharemundo_links_id", "company_admin_email",
            "main_area_of_expertise", "sub_area_of_expertise",
        )
