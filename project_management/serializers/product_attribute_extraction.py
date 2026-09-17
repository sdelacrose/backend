from rest_framework import serializers

from project_management.models import ProductAttributeExtractionRun
from project_management.serializers.product_assembly_extraction import (
    ProductAssemblyEvidenceSerializer,
)
from project_management.services.product_attribute_extraction import (
    source_documents_changed,
)


class ProductAttributeValueSerializer(serializers.Serializer):
    solution_demand_id = serializers.IntegerField(min_value=1)
    value = serializers.CharField(max_length=2000, trim_whitespace=True)
    confidence = serializers.FloatField(min_value=0, max_value=1)
    evidence = ProductAssemblyEvidenceSerializer(many=True, allow_empty=False)
    selected = serializers.BooleanField(required=False, default=True)


class ProductAttributeProposalSerializer(serializers.Serializer):
    proposal_key = serializers.CharField(max_length=128)
    label = serializers.CharField(max_length=255)
    label_en = serializers.CharField(required=False, allow_blank=True, max_length=255)
    label_de = serializers.CharField(required=False, allow_blank=True, max_length=255)
    label_fr = serializers.CharField(required=False, allow_blank=True, max_length=255)
    unit = serializers.CharField(required=False, allow_blank=True, max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, max_length=1000)
    existing_column_key = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
        max_length=128,
    )
    confidence = serializers.FloatField(min_value=0, max_value=1)
    evidence = ProductAssemblyEvidenceSerializer(many=True, allow_empty=False)
    values = ProductAttributeValueSerializer(many=True, allow_empty=False)
    conflicts = serializers.ListField(
        child=serializers.CharField(max_length=1000),
        required=False,
        allow_empty=True,
    )
    selected = serializers.BooleanField(required=False, default=True)

    def validate_existing_column_key(self, value):
        return value or None


class ProductAttributeApplySerializer(serializers.Serializer):
    attributes = ProductAttributeProposalSerializer(many=True, required=False)


class ProductAttributeExtractionCreateSerializer(serializers.Serializer):
    project = serializers.IntegerField(min_value=1)
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=False,
    )
    project_language = serializers.ChoiceField(choices=["en", "de", "fr"], default="en")


class ProductAttributeExtractionRunSerializer(serializers.ModelSerializer):
    sources = serializers.SerializerMethodField()
    applied_columns = serializers.SerializerMethodField()
    source_documents_changed = serializers.SerializerMethodField()

    class Meta:
        model = ProductAttributeExtractionRun
        fields = [
            "id",
            "project",
            "status",
            "schema_version",
            "project_language",
            "source_snapshot_hash",
            "source_selection_mode",
            "source_documents_changed",
            "matrix_snapshot_hash",
            "hint_only",
            "result",
            "warnings",
            "error_code",
            "error_message",
            "sources",
            "applied_columns",
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
        if obj.status != ProductAttributeExtractionRun.Status.REVIEW_READY:
            return False
        request = self.context.get("request")
        return source_documents_changed(obj, request=request)

    def get_applied_columns(self, obj):
        return [
            {
                "proposal_key": column.proposal_key,
                "column_key": column.column_key,
                "value_count": column.applied_values.count(),
            }
            for column in obj.applied_columns.prefetch_related("applied_values").all()
        ]
