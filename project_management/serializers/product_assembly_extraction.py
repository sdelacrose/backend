from rest_framework import serializers

from project_management.models import ProductAssemblyExtractionRun
from project_management.services.product_assembly_extraction import (
    source_documents_changed,
)


class ProductAssemblyEvidenceSerializer(serializers.Serializer):
    document_id = serializers.IntegerField(min_value=1)
    segment_id = serializers.IntegerField(min_value=1)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    quote = serializers.CharField(max_length=1000)


class ProductAssemblyProposalSerializer(serializers.Serializer):
    proposal_key = serializers.CharField(max_length=128)
    parent_key = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=128,
    )
    name = serializers.CharField(max_length=255)
    demand_numbers = serializers.ListField(
        child=serializers.CharField(max_length=100),
        required=False,
        allow_empty=True,
    )
    confidence = serializers.FloatField(min_value=0, max_value=1)
    evidence = ProductAssemblyEvidenceSerializer(many=True, allow_empty=False)

    def validate_parent_key(self, value):
        return value or None


class ProductAssemblyApplySerializer(serializers.Serializer):
    rows = ProductAssemblyProposalSerializer(many=True, required=False)


class ProductAssemblyExtractionCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
    )
    project_language = serializers.ChoiceField(
        choices=["en", "de", "fr"],
        default="en",
    )


class ProductAssemblyExtractionRunSerializer(serializers.ModelSerializer):
    sources = serializers.SerializerMethodField()
    applied_rows = serializers.SerializerMethodField()
    source_documents_changed = serializers.SerializerMethodField()

    class Meta:
        model = ProductAssemblyExtractionRun
        fields = [
            "id",
            "project",
            "status",
            "schema_version",
            "project_language",
            "source_snapshot_hash",
            "source_selection_mode",
            "source_documents_changed",
            "hint_only",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "sources",
            "applied_rows",
            "started_at",
            "completed_at",
            "applied_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_sources(self, obj):
        return [
            {
                "document_id": source.document_id_snapshot,
                "file_name": source.document_name,
                "content_hash": source.content_hash,
                "parser_version": source.parser_version,
                "segment_count": source.segment_count,
            }
            for source in obj.sources.select_related("document").all()
        ]

    def get_source_documents_changed(self, obj):
        if obj.status != ProductAssemblyExtractionRun.Status.REVIEW_READY:
            return False
        request = self.context.get("request")
        return source_documents_changed(obj, request=request)

    def get_applied_rows(self, obj):
        rows = []
        for applied in obj.applied_rows.select_related("solution_demand").all():
            target = applied.solution_demand
            rows.append(
                {
                    "proposal_key": applied.proposal_key,
                    "solution_demand_id": applied.solution_demand_id,
                    "hierarchy_number": target.get_sequence(),
                    "name": target.subject,
                    "demand_numbers": applied.applied_payload.get(
                        "demand_numbers",
                        [],
                    ),
                }
            )
        return rows
