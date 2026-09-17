from rest_framework import serializers

from project_management.models import BomGenerationRun, BomPlanOperation


class BomPlanOperationSerializer(serializers.ModelSerializer):
    class Meta:
        model = BomPlanOperation
        fields = [
            "id",
            "operation",
            "source_workstation",
            "source_snapshot",
            "source_occurrence_id",
            "target_bom_part",
            "before_values",
            "after_values",
            "resolution_state",
            "sort_order",
        ]


class BomGenerationRunSerializer(serializers.ModelSerializer):
    operations = BomPlanOperationSerializer(many=True, read_only=True)
    workstation_snapshots = serializers.PrimaryKeyRelatedField(many=True, read_only=True)

    class Meta:
        model = BomGenerationRun
        fields = [
            "id",
            "project",
            "workstation_snapshots",
            "source_hash",
            "target_hash",
            "status",
            "summary",
            "request_payload",
            "metrics",
            "node_count",
            "duration_ms",
            "error_details",
            "operations",
            "created_by",
            "applied_by",
            "created_at",
            "updated_at",
            "applied_at",
            "started_at",
            "completed_at",
        ]


class BomGenerationRequestSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    workstations = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=100,
    )
    idempotency_key = serializers.CharField(max_length=128, trim_whitespace=True)

    def validate_idempotency_key(self, value):
        if value.startswith("__bom_worker__:"):
            raise serializers.ValidationError("This idempotency-key namespace is reserved.")
        return value


class BomApplyRequestSerializer(serializers.Serializer):
    expected_target_hash = serializers.CharField(min_length=64, max_length=64)
    confirm_detach = serializers.BooleanField(required=False, default=False)


class BomConflictResolutionSerializer(serializers.Serializer):
    operation = serializers.IntegerField(min_value=1)
    decision = serializers.ChoiceField(choices=["keep_existing", "use_workstation"])
    target_bom_part = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if attrs["decision"] == "use_workstation" and not attrs.get("target_bom_part"):
            raise serializers.ValidationError(
                {"target_bom_part": "Choose the existing BoM row to update."},
            )
        return attrs


class BomConflictResolutionRequestSerializer(serializers.Serializer):
    resolutions = BomConflictResolutionSerializer(many=True, allow_empty=False)
