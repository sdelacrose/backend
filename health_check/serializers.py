from rest_framework import serializers
from models.models import APITestResult
 
class APITestResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = APITestResult
        fields = '__all__' 