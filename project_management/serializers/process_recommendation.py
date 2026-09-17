from rest_framework import serializers

from project_management.models import ProcessAssemblyRecommendationRun


class ProcessAssemblyRecommendationRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessAssemblyRecommendationRun
        fields = [
            "id",
            "project",
            "input_hash",
            "policy_version",
            "policy",
            "ranked_candidates",
            "selection_outcome",
            "decision_reason",
            "selected_candidate",
            "created_workstation",
            "override_history",
            "created_by",
            "created_at",
            "updated_at",
        ]


class ProcessAssemblyRecommendationCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)


class ProcessAssemblyRecommendationSelectSerializer(serializers.Serializer):
    candidate = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(max_length=2000, trim_whitespace=True)
