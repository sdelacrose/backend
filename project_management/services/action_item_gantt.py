from __future__ import annotations

from collections import defaultdict

from models.models import User
from project_management.models import ActionItem, ActionItemDependency


class ActionItemGanttPayloadService:
    """Build a normalized Gantt payload without materializing model graphs."""

    ACTION_FIELDS = (
        "id",
        "name",
        "status",
        "priority_level",
        "start_date",
        "target_date",
        "forecast_date",
        "closed_at",
        "parent_action_item_id",
        "milestone_id",
    )

    @classmethod
    def build(cls, parents, children):
        parent_rows = list(
            parents.order_by("milestone__sort_order", "milestone_id", "id").values(
                *cls.ACTION_FIELDS
            )
        )
        parent_ids = [row["id"] for row in parent_rows]
        child_rows = list(
            children.filter(parent_action_item_id__in=parent_ids)
            .order_by("parent_action_item_id", "id")
            .values(*cls.ACTION_FIELDS)
        )
        all_rows = parent_rows + child_rows
        action_ids = [row["id"] for row in all_rows]

        assignee_ids_by_action = defaultdict(list)
        user_ids = set()
        through_model = ActionItem.assignees.through
        for relation in through_model.objects.filter(
            actionitem_id__in=action_ids
        ).values("actionitem_id", "user_id"):
            assignee_ids_by_action[relation["actionitem_id"]].append(
                relation["user_id"]
            )
            user_ids.add(relation["user_id"])

        users = []
        for user in User.objects.filter(id__in=user_ids).values(
            "id", "first_name", "last_name", "email"
        ):
            name = f'{user["first_name"]} {user["last_name"]}'.strip()
            users.append({"id": user["id"], "name": name or user["email"]})

        dependencies = list(
            ActionItemDependency.objects.filter(
                dependent_id__in=action_ids,
                predecessor_id__in=action_ids,
                dependency_type=(
                    ActionItemDependency.DependencyType.FINISH_TO_START
                ),
            )
            .order_by("id")
            .values("predecessor_id", "dependent_id", "dependency_type")
        )

        children_by_parent = defaultdict(list)
        for row in child_rows:
            children_by_parent[row["parent_action_item_id"]].append(
                cls._serialize_row(row, assignee_ids_by_action)
            )

        serialized_parents = []
        for row in parent_rows:
            serialized = cls._serialize_row(row, assignee_ids_by_action)
            serialized["sub_action_items"] = children_by_parent[row["id"]]
            serialized_parents.append(serialized)

        return {
            "users": users,
            "dependencies": dependencies,
            "action_items": serialized_parents,
        }

    @staticmethod
    def _serialize_row(row, assignee_ids_by_action):
        return {
            **row,
            "assignee_ids": assignee_ids_by_action[row["id"]],
            "sub_action_items": [],
        }
