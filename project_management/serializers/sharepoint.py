from rest_framework import serializers

class SharePointSiteSYNCSerializer(serializers.Serializer):
    project_id = serializers.IntegerField(required=False)
    company_id = serializers.IntegerField(required=False)
    freelancer_id = serializers.IntegerField(required=False)
    site_name = serializers.CharField()
    site_id = serializers.CharField()
