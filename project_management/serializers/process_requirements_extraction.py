from rest_framework import serializers

from project_management.models import (
    ProcessAssemblyExtractionRun,
    ProcessAttributeExtractionRun,
)


class ProcessSourceReferenceSerializer(serializers.Serializer):
    source_type = serializers.ChoiceField(
        choices=["customer_demand", "solution_space"]
    )
    source_id = serializers.IntegerField(min_value=1)
    field = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=128,
    )


class ProcessAssemblyProposalSerializer(serializers.Serializer):
    proposal_key = serializers.CharField(max_length=128)
    parent_key = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=128,
    )
    name = serializers.CharField(max_length=255)
    station_number = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=100,
    )
    linked_demand_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
    )
    linked_solution_space_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )
    source_kind = serializers.ChoiceField(choices=["matched"])
    confidence = serializers.FloatField(min_value=0, max_value=1)
    rationale = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
    )
    source_references = ProcessSourceReferenceSerializer(
        many=True,
        allow_empty=False,
    )
    warnings = serializers.ListField(
        child=serializers.CharField(max_length=1000),
        required=False,
        allow_empty=True,
    )
    unresolved_questions = serializers.ListField(
        child=serializers.CharField(max_length=1000),
        required=False,
        allow_empty=True,
    )
    selected = serializers.BooleanField(default=True)

    def validate_parent_key(self, value):
        return value or None


class ProcessAssemblyExtractionCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    project_language = serializers.ChoiceField(
        choices=["en", "de", "fr"],
        default="en",
    )


class ProcessAssemblyApplySerializer(serializers.Serializer):
    rows = ProcessAssemblyProposalSerializer(many=True, required=False)


class ProcessAssemblyExtractionRunSerializer(serializers.ModelSerializer):
    input_summary = serializers.SerializerMethodField()
    applied_rows = serializers.SerializerMethodField()

    class Meta:
        model = ProcessAssemblyExtractionRun
        fields = [
            "id",
            "project",
            "status",
            "schema_version",
            "project_language",
            "input_snapshot_hash",
            "input_summary",
            "hint_only",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "applied_rows",
            "started_at",
            "completed_at",
            "applied_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_input_summary(self, obj):
        snapshot = obj.input_snapshot or {}
        return {
            "customer_demand_count": len(snapshot.get("customer_demands", [])),
            "solution_space_count": len(snapshot.get("project_solution_space", [])),
            "requirement_count": len(snapshot.get("requirements", [])),
        }

    def get_applied_rows(self, obj):
        return [
            {
                "proposal_key": applied.proposal_key,
                "requirement_id": applied.requirement_id,
                "hierarchy_number": applied.requirement.get_sequence(),
                "name": applied.requirement.name,
                "station_number": applied.requirement.object_number,
            }
            for applied in obj.applied_rows.select_related("requirement").all()
        ]


class ProcessAttributeValueProposalSerializer(serializers.Serializer):
    requirement_id = serializers.IntegerField(min_value=1)
    value = serializers.CharField(max_length=2000)
    confidence = serializers.FloatField(min_value=0, max_value=1)
    source_references = ProcessSourceReferenceSerializer(
        many=True,
        allow_empty=False,
    )
    selected = serializers.BooleanField(default=True)


class ProcessAttributeProposalSerializer(serializers.Serializer):
    proposal_key = serializers.CharField(max_length=128)
    label = serializers.CharField(max_length=255)
    label_en = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )
    label_de = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )
    label_fr = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=255,
    )
    unit = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=64,
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
    )
    existing_column_key = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=128,
    )
    confidence = serializers.FloatField(min_value=0, max_value=1)
    source_references = ProcessSourceReferenceSerializer(
        many=True,
        allow_empty=False,
    )
    values = ProcessAttributeValueProposalSerializer(many=True, allow_empty=False)
    conflicts = serializers.ListField(
        child=serializers.CharField(max_length=1000),
        required=False,
        allow_empty=True,
    )
    selected = serializers.BooleanField(default=True)

    def validate_existing_column_key(self, value):
        return value or None


class ProcessAttributeExtractionCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    project_language = serializers.ChoiceField(
        choices=["en", "de", "fr"],
        default="en",
    )


class ProcessAttributeApplySerializer(serializers.Serializer):
    attributes = ProcessAttributeProposalSerializer(many=True, required=False)


class ProcessAttributeExtractionRunSerializer(serializers.ModelSerializer):
    input_summary = serializers.SerializerMethodField()
    applied_columns = serializers.SerializerMethodField()

    class Meta:
        model = ProcessAttributeExtractionRun
        fields = [
            "id",
            "project",
            "status",
            "schema_version",
            "project_language",
            "input_snapshot_hash",
            "input_summary",
            "hint_only",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "applied_columns",
            "started_at",
            "completed_at",
            "applied_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_input_summary(self, obj):
        snapshot = obj.input_snapshot or {}
        return {
            "customer_demand_count": len(snapshot.get("customer_demands", [])),
            "solution_space_count": len(snapshot.get("project_solution_space", [])),
            "requirement_count": len(snapshot.get("requirements", [])),
        }

    def get_applied_columns(self, obj):
        return [
            {
                "proposal_key": applied.proposal_key,
                "column_key": applied.column_key,
                "value_count": applied.applied_values.count(),
            }
            for applied in obj.applied_columns.prefetch_related("applied_values").all()
        ]
