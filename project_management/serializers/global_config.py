from rest_framework import serializers

from models.models import GlobalConfig
from models.hierarchical_configurations import annotate_nested_configurations


class GlobalConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalConfig
        fields = '__all__'

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["nested_configurations"] = annotate_nested_configurations(
            instance.nested_configurations
        )
        return representation
