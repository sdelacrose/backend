import logging

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from project_management.models import ActionItem
from project_management.permissions import ActionItemPermission
from project_management.serializers import ActionItemSerializer


MAX_BULK_ACTION_ITEMS = 200
logger = logging.getLogger(__name__)
BULK_UPDATE_FIELDS = {
    "assignees",
    "labels",
    "milestone",
    "priority_level",
    "reminder",
    "status",
}


class BulkActionItemChangesSerializer(serializers.Serializer):
    assignees = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
    )
    labels = serializers.ListField(
        child=serializers.CharField(),
        required=False,
    )
    milestone = serializers.IntegerField(min_value=1, required=False)
    priority_level = serializers.CharField(required=False)
    reminder = serializers.CharField(required=False)
    status = serializers.CharField(required=False)

    def to_internal_value(self, data):
        unknown_fields = set(data) - BULK_UPDATE_FIELDS
        if unknown_fields:
            raise serializers.ValidationError(
                "Unsupported fields: " + ", ".join(sorted(unknown_fields))
            )
        return super().to_internal_value(data)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                "At least one field must be provided."
            )
        return attrs


class BulkActionItemUpdateRequestSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=MAX_BULK_ACTION_ITEMS,
    )
    changes = BulkActionItemChangesSerializer()

    def validate_ids(self, value):
        return list(dict.fromkeys(value))


def _scope_key(action_item):
    owner = action_item.parent_action_item or action_item
    if owner.milestone_id:
        return "project", owner.milestone.project_id
    if owner.company_profile_id:
        return "company", owner.company_profile_id
    if owner.freelancer_id:
        return "freelancer", owner.freelancer_id
    return None


def _error_text(error):
    if isinstance(error, dict):
        return "; ".join(
            f"{field}: {_error_text(value)}"
            for field, value in error.items()
        )
    if isinstance(error, (list, tuple)):
        return "; ".join(_error_text(value) for value in error)
    return str(error)


class ActionItemBulkUpdateService:
    def __init__(self, *, request, serializer_context):
        self.request = request
        self.serializer_context = serializer_context
        self.permission = ActionItemPermission(action="update")

    def execute(self, *, action_item_ids, changes):
        queryset = ActionItem.objects.select_related(
            "milestone__project",
            "company_profile",
            "freelancer",
            "parent_action_item__milestone__project",
            "parent_action_item__company_profile",
            "parent_action_item__freelancer",
        ).prefetch_related("assignees")
        action_items = queryset.in_bulk(action_item_ids)

        updated_ids = []
        failures = []
        request_scope = None

        with transaction.atomic():
            for action_item_id in action_item_ids:
                action_item = action_items.get(action_item_id)
                if action_item is None:
                    failures.append(
                        {"id": action_item_id, "error": "Action Item not found."}
                    )
                    continue

                if not self.permission.can_update_object(
                    self.request.user,
                    action_item,
                ):
                    failures.append(
                        {
                            "id": action_item_id,
                            "error": "You do not have permission to update this Action Item.",
                        }
                    )
                    continue

                item_scope = _scope_key(action_item)
                if request_scope is None:
                    request_scope = item_scope
                elif item_scope != request_scope:
                    failures.append(
                        {
                            "id": action_item_id,
                            "error": "All Action Items must belong to the same scope.",
                        }
                    )
                    continue

                if action_item.status == "closed":
                    failures.append(
                        {
                            "id": action_item_id,
                            "error": "A closed action item cannot be updated.",
                        }
                    )
                    continue

                try:
                    with transaction.atomic():
                        serializer = ActionItemSerializer(
                            action_item,
                            data=changes,
                            partial=True,
                            context=self.serializer_context,
                        )
                        serializer.is_valid(raise_exception=True)
                        action_item._suppress_action_item_notifications = True
                        save_kwargs = {}
                        if (
                            serializer.validated_data.get("status") == "closed"
                            and action_item.status != "closed"
                        ):
                            save_kwargs = {
                                "closed_at": timezone.localdate(),
                                "closed_by": self.request.user,
                            }
                        serializer.save(**save_kwargs)
                        updated_ids.append(action_item_id)
                except (serializers.ValidationError, ValueError) as error:
                    detail = getattr(error, "detail", error)
                    failures.append(
                        {"id": action_item_id, "error": _error_text(detail)}
                    )

            if updated_ids:
                from project_management.tasks import (
                    send_bulk_action_item_update_notification,
                )

                transaction.on_commit(
                    lambda: self._enqueue_summary_notification(
                        send_bulk_action_item_update_notification,
                        updated_ids,
                        sorted(changes),
                    )
                )

        return {"updated_ids": updated_ids, "failures": failures}

    def _enqueue_summary_notification(self, task, updated_ids, changed_fields):
        try:
            task.delay(
                updated_ids,
                self.request.user.id,
                changed_fields,
            )
        except Exception:
            logger.exception(
                "Unable to enqueue bulk Action Item update notification."
            )
