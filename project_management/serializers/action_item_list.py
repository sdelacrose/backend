from rest_framework import serializers

from models.models import User
from project_management.models import ActionItem, Milestone


class ActionItemListUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "name")


class ActionItemListMilestoneSerializer(serializers.ModelSerializer):
    project = serializers.SerializerMethodField()

    class Meta:
        model = Milestone
        fields = (
            "id",
            "name",
            "colour",
            "sort_order",
            "start_date",
            "due_date",
            "project",
        )

    def get_project(self, milestone):
        return {
            "id": milestone.project_id,
            "name": milestone.project.project_name,
        }


class ActionItemListChildSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    closed_by = ActionItemListUserSerializer(read_only=True)
    opportunities = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    num_documents = serializers.IntegerField(read_only=True)
    num_opportunities = serializers.IntegerField(read_only=True)
    num_labels = serializers.IntegerField(read_only=True)
    assignees_count = serializers.IntegerField(read_only=True)
    booked_hour = serializers.FloatField(read_only=True)
    booked_cost = serializers.FloatField(read_only=True)

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "opportunities",
            "created_by",
            "closed_by",
            "num_documents",
            "num_opportunities",
            "num_labels",
            "assignees_count",
            "booked_hour",
            "booked_cost",
        )

    def get_milestone(self, obj):
        milestone = obj.milestone
        if milestone is None and obj.parent_action_item is not None:
            milestone = obj.parent_action_item.milestone
        if milestone is None:
            return None
        return ActionItemListMilestoneSerializer(milestone).data


class ActionItemListSerializer(ActionItemListChildSerializer):
    milestone = ActionItemListMilestoneSerializer(read_only=True)
    sub_action_items = serializers.SerializerMethodField()
    num_sub_action_items = serializers.IntegerField(read_only=True)

    class Meta(ActionItemListChildSerializer.Meta):
        fields = ActionItemListChildSerializer.Meta.fields + (
            "sub_action_items",
            "num_sub_action_items",
        )

    def get_sub_action_items(self, obj):
        children = getattr(obj, "prefetched_list_children", None)
        if children is None:
            children = obj.sub_action_items.all()
            if self.context.get("limit_sub_action_items_to_visible"):
                children = children.filter(
                    id__in=self.context.get("visible_action_item_ids", set())
                )
        return ActionItemListChildSerializer(
            children,
            many=True,
            context=self.context,
        ).data


class ActionItemBoardDocumentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField(allow_blank=True, allow_null=True)


class ActionItemSubActionSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    closed_by = ActionItemListUserSerializer(read_only=True)
    documents = ActionItemBoardDocumentSerializer(many=True, read_only=True)

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "created_by",
            "closed_by",
            "documents",
        )

    def get_milestone(self, obj):
        parent = obj.parent_action_item
        milestone = obj.milestone or (parent.milestone if parent else None)
        return (
            ActionItemListMilestoneSerializer(milestone).data
            if milestone is not None
            else None
        )


class ActionItemBoardSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    documents = ActionItemBoardDocumentSerializer(many=True, read_only=True)
    num_documents = serializers.SerializerMethodField()
    is_sub_action_item = serializers.SerializerMethodField()
    parent_action_item_id = serializers.IntegerField(
        read_only=True, allow_null=True
    )
    parent_action_item_name = serializers.CharField(
        source="parent_action_item.name", read_only=True, allow_null=True
    )
    parent_action_item_status = serializers.CharField(
        source="parent_action_item.status", read_only=True, allow_null=True
    )
    num_sub_action_items = serializers.IntegerField(read_only=True)

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "created_by",
            "documents",
            "num_documents",
            "is_sub_action_item",
            "parent_action_item_id",
            "parent_action_item_name",
            "parent_action_item_status",
            "num_sub_action_items",
        )

    def get_milestone(self, obj):
        milestone = obj.milestone
        if milestone is None and obj.parent_action_item is not None:
            milestone = obj.parent_action_item.milestone
        return (
            ActionItemListMilestoneSerializer(milestone).data
            if milestone is not None
            else None
        )

    def get_num_documents(self, obj):
        return len(obj.documents.all())

    def get_is_sub_action_item(self, obj):
        return obj.parent_action_item_id is not None


class ActionItemCalendarSerializer(serializers.BaseSerializer):
    def to_representation(self, obj):
        parent = obj.parent_action_item
        milestone = obj.milestone or (parent.milestone if parent else None)
        return {
            "id": obj.id,
            "name": obj.name,
            "start_date": obj.start_date,
            "target_date": obj.target_date,
            "status": obj.status,
            "colour": milestone.colour if milestone else "#0057B7",
            "is_sub_action_item": parent is not None,
            "parent_action_item_id": parent.id if parent else None,
            "parent_action_item_name": parent.name if parent else None,
            "parent_action_item_status": parent.status if parent else None,
        }
