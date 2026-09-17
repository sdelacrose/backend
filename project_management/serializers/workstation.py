from rest_framework import serializers

from project_management.models import (
    ProjectWorkstation,
    ProjectWorkstationNode,
    WorkstationRevision,
)


class ProjectWorkstationNodeSerializer(serializers.ModelSerializer):
    source_company_item_name = serializers.CharField(
        source="source_company_item.assembly_name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = ProjectWorkstationNode
        fields = [
            "id",
            "occurrence_id",
            "parent",
            "source_company_item",
            "source_company_item_name",
            "source_project_item",
            "supersedes",
            "name",
            "assembly_type",
            "role",
            "is_active",
            "approval_status",
            "quantity",
            "unit",
            "level",
            "sort_order",
            "required_skills",
            "provided_skills",
            "custom_fields",
        ]


class WorkstationRevisionSerializer(serializers.ModelSerializer):
    content_hash = serializers.CharField(source="snapshot.content_hash", read_only=True)
    node_count = serializers.IntegerField(source="nodes.count", read_only=True)

    class Meta:
        model = WorkstationRevision
        fields = [
            "id",
            "number",
            "reason",
            "source_hash",
            "content_hash",
            "node_count",
            "summary",
            "created_by",
            "created_at",
        ]


class ProjectWorkstationSerializer(serializers.ModelSerializer):
    nodes = serializers.SerializerMethodField()
    source_process_assembly_name = serializers.CharField(
        source="source_process_assembly.assembly_name",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = ProjectWorkstation
        fields = [
            "id",
            "project",
            "source_process_assembly",
            "source_requirement",
            "source_process_assembly_name",
            "name",
            "status",
            "template_hash",
            "active_revision",
            "nodes",
            "created_by",
            "modified_by",
            "created_at",
            "updated_at",
        ]

    def get_nodes(self, obj):
        nodes = getattr(obj, "active_nodes", None)
        if nodes is None:
            nodes = obj.nodes.filter(revision__number=obj.active_revision).select_related(
                "source_company_item",
            )
        else:
            nodes = [
                node
                for node in nodes
                if node.revision.number == obj.active_revision
            ]
        return ProjectWorkstationNodeSerializer(nodes, many=True).data


class ProjectWorkstationCreateSerializer(serializers.Serializer):
    source_requirement = serializers.IntegerField(min_value=1, required=False)
    name = serializers.CharField(max_length=255, required=False, allow_blank=True, default="")
    project = serializers.IntegerField(min_value=1)
    source_process_assembly = serializers.IntegerField(min_value=1, required=False)
    idempotency_key = serializers.CharField(max_length=128, trim_whitespace=True)


class WorkstationRevisionPreviewRequestSerializer(serializers.Serializer):
    expected_revision = serializers.IntegerField(min_value=1)
    reason = serializers.CharField(required=False, allow_blank=True, max_length=2000)
    operations = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        max_length=100,
    )


class WorkstationRevisionCommitRequestSerializer(
    WorkstationRevisionPreviewRequestSerializer,
):
    idempotency_key = serializers.CharField(max_length=128, trim_whitespace=True)


class WorkbenchTargetSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    expected_revision = serializers.IntegerField(min_value=1)
    parent_node = serializers.IntegerField(min_value=1, required=False)


class WorkbenchEditSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=["copy", "replace"])
    source_node = serializers.IntegerField(min_value=1)
    expected_revision = serializers.IntegerField(min_value=1)
    targets = WorkbenchTargetSerializer(many=True, allow_empty=False, max_length=25)
    replacement = serializers.IntegerField(min_value=1, required=False)
    reason = serializers.CharField(max_length=2000, allow_blank=False)
    idempotency_key = serializers.CharField(max_length=128, allow_blank=False)
    commit = serializers.BooleanField(default=False)
