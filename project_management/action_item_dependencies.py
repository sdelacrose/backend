from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework import serializers

from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.models import ActionItem, ActionItemDependency


DEPENDENCY_TYPE = ActionItemDependency.DependencyType.FINISH_TO_START
CIRCULAR_DEPENDENCY_ERROR = (
    "These actions cannot be linked because this would create a circular "
    "dependency. Please remove the existing dependency before creating a new "
    "one."
)


def get_root_action_item(action_item):
    return action_item.parent_action_item or action_item


def get_action_item_dependency_scope(action_item):
    root_action_item = get_root_action_item(action_item)
    if root_action_item.milestone_id:
        return (
            "project",
            root_action_item.milestone.project_id,
        )
    if root_action_item.company_profile_id:
        return "company", root_action_item.company_profile_id
    if root_action_item.freelancer_id:
        return "freelancer", root_action_item.freelancer_id
    return None, None


def get_action_item_dependency_candidate_queryset(action_item):
    if action_item.parent_action_item_id:
        queryset = ActionItem.objects.filter(
            parent_action_item_id=action_item.parent_action_item_id,
        )
    else:
        scope, scope_id = get_action_item_dependency_scope(action_item)
        if scope == "project":
            queryset = ActionItem.objects.filter(
                parent_action_item__isnull=True,
                milestone__project_id=scope_id,
            )
        elif scope == "company":
            queryset = ActionItem.objects.filter(
                parent_action_item__isnull=True,
                company_profile_id=scope_id,
            )
        elif scope == "freelancer":
            queryset = ActionItem.objects.filter(
                parent_action_item__isnull=True,
                freelancer_id=scope_id,
            )
        else:
            return ActionItem.objects.none()

    return (
        queryset.exclude(id=action_item.id)
        .select_related(
            "milestone",
            "milestone__project",
            "parent_action_item",
            "parent_action_item__milestone",
            "parent_action_item__milestone__project",
        )
        .order_by(Lower("name"), "id")
    )


def get_predecessor_candidate_queryset(dependent):
    return get_action_item_dependency_candidate_queryset(dependent)


def get_dependent_candidate_queryset(predecessor):
    return get_action_item_dependency_candidate_queryset(predecessor)


def filter_dependency_candidates_by_visibility(queryset, user, action_item):
    scope, scope_id = get_action_item_dependency_scope(action_item)
    if scope == "freelancer":
        if user and user.id == scope_id:
            return queryset
        return queryset.none()

    root_action_item = get_root_action_item(action_item)
    if scope == "company":
        if not user or user.company_id != scope_id:
            return queryset.none()

        has_broad_visibility = require_any_permission_multiple_modules(
            user,
            {"action_item": ["view", "full_access"]},
            "company",
            {},
        )
        has_assigned_visibility = require_any_permission_multiple_modules(
            user,
            {"action_item": ["view_assigned_created"]},
            "company",
            {},
        )
    elif scope == "project":
        project = root_action_item.milestone.project
        context = {"project": project}
        project_company_id = (
            project.company_profile_id
            or (
                project.user.company_id
                if project.user_id and project.user
                else None
            )
        )
        has_company_project_visibility = (
            bool(user)
            and not user.is_freelancer
            and user.company_id == project_company_id
            and require_any_permission_multiple_modules(
                user,
                {"projects": ["full_access", "view_all_access"]},
                "company",
                context,
            )
        )
        has_broad_visibility = (
            has_company_project_visibility
            or require_any_permission_multiple_modules(
                user,
                {"action_item": ["view", "full_access"]},
                "project",
                context,
            )
        )
        has_assigned_visibility = require_any_permission_multiple_modules(
            user,
            {"action_item": ["view_assigned_created"]},
            "project",
            context,
        )
    else:
        return queryset.none()

    if has_broad_visibility:
        return queryset
    if not has_assigned_visibility:
        return queryset.none()

    assigned_or_created = Q(assignees=user) | Q(created_by=user)
    if action_item.parent_action_item_id is None:
        assigned_or_created |= (
            Q(sub_action_items__assignees=user)
            | Q(sub_action_items__created_by=user)
        )
    return queryset.filter(assigned_or_created).distinct()


def filter_predecessor_candidates_by_visibility(queryset, user, dependent):
    return filter_dependency_candidates_by_visibility(
        queryset,
        user,
        dependent,
    )


def filter_dependent_candidates_by_update_permission(
    queryset,
    user,
    predecessor,
):
    scope, scope_id = get_action_item_dependency_scope(predecessor)
    if scope == "freelancer":
        if user and user.id == scope_id:
            return queryset
        return queryset.none()

    if scope == "company":
        if not user or user.company_id != scope_id:
            return queryset.none()
        context = {}
        has_broad_update = require_any_permission_multiple_modules(
            user,
            {"action_item": ["full_access", "update"]},
            "company",
            context,
        )
        has_assigned_update = require_any_permission_multiple_modules(
            user,
            {"action_item": ["update_assigned_created"]},
            "company",
            context,
        )
    elif scope == "project":
        project = get_root_action_item(predecessor).milestone.project
        context = {"project": project}
        project_company_id = (
            project.company_profile_id
            or (
                project.user.company_id
                if project.user_id and project.user
                else None
            )
        )
        has_company_project_update = (
            bool(user)
            and not user.is_freelancer
            and user.company_id == project_company_id
            and require_any_permission_multiple_modules(
                user,
                {"projects": ["full_access"]},
                "company",
                context,
            )
        )
        has_broad_update = (
            has_company_project_update
            or require_any_permission_multiple_modules(
                user,
                {"action_item": ["full_access", "update"]},
                "project",
                context,
            )
        )
        has_assigned_update = require_any_permission_multiple_modules(
            user,
            {"action_item": ["update_assigned_created"]},
            "project",
            context,
        )
    else:
        return queryset.none()

    if has_broad_update:
        return queryset
    if not has_assigned_update:
        return queryset.none()
    return queryset.filter(
        Q(assignees=user) | Q(created_by=user)
    ).distinct()


def search_action_item_dependency_candidates(queryset, search):
    search = str(search or "").strip()
    if not search:
        return queryset

    query = Q(name__icontains=search) | Q(milestone__name__icontains=search)
    if search.isdigit():
        query |= Q(id=int(search))
    return queryset.filter(query).distinct()


def search_predecessor_candidates(queryset, search):
    return search_action_item_dependency_candidates(queryset, search)


def search_dependent_candidates(queryset, search):
    return search_action_item_dependency_candidates(queryset, search)


def serialize_action_item_dependency_candidate(action_item):
    effective_milestone = action_item.milestone
    if effective_milestone is None and action_item.parent_action_item:
        effective_milestone = action_item.parent_action_item.milestone

    return {
        "id": action_item.id,
        "name": action_item.name,
        "start_date": action_item.start_date,
        "target_date": action_item.target_date,
        "status": action_item.status,
        "parent_action_item_id": action_item.parent_action_item_id,
        "milestone": (
            {
                "id": effective_milestone.id,
                "name": effective_milestone.name,
            }
            if effective_milestone
            else None
        ),
    }


def serialize_predecessor_candidate(action_item):
    return serialize_action_item_dependency_candidate(action_item)


def serialize_dependent_candidate(action_item):
    return serialize_action_item_dependency_candidate(action_item)


def _dependency_would_create_cycle(dependent_id, predecessor_ids):
    target_ids = set(predecessor_ids)
    frontier = {dependent_id}
    visited = set()

    while frontier:
        visited.update(frontier)
        reachable_ids = set(
            ActionItemDependency.objects.filter(
                predecessor_id__in=frontier,
                dependency_type=DEPENDENCY_TYPE,
            )
            .exclude(dependent_id=dependent_id)
            .values_list("dependent_id", flat=True)
        )
        if reachable_ids & target_ids:
            return True
        frontier = reachable_ids - visited

    return False


def _dependent_replacement_would_create_cycle(
    predecessor_id,
    dependent_ids,
):
    frontier = set(dependent_ids)
    visited = set()

    while frontier:
        if predecessor_id in frontier:
            return True

        visited.update(frontier)
        reachable_ids = set(
            ActionItemDependency.objects.filter(
                predecessor_id__in=frontier,
                dependency_type=DEPENDENCY_TYPE,
            )
            .exclude(predecessor_id=predecessor_id)
            .values_list("dependent_id", flat=True)
        )
        frontier = reachable_ids - visited

    return False


def validate_action_item_predecessors(dependent, predecessors, user):
    predecessor_ids = [predecessor.id for predecessor in predecessors]
    if len(predecessor_ids) != len(set(predecessor_ids)):
        raise serializers.ValidationError(
            {
                "predecessor_ids": (
                    "Duplicate predecessor Action Items are not allowed."
                )
            }
        )
    if dependent.id in predecessor_ids:
        raise serializers.ValidationError(
            {
                "predecessor_ids": (
                    "An Action Item cannot depend on itself."
                )
            }
        )
    if not predecessor_ids:
        return []

    candidate_queryset = get_predecessor_candidate_queryset(dependent).filter(
        id__in=predecessor_ids
    )
    candidates_by_id = {
        candidate.id: candidate
        for candidate in candidate_queryset
    }
    invalid_scope_ids = set(predecessor_ids) - set(candidates_by_id)
    if invalid_scope_ids:
        raise serializers.ValidationError(
            {
                "predecessor_ids": (
                    "Predecessors must belong to the allowed scope and "
                    "Action Item level."
                )
            }
        )

    visible_ids = set(
        filter_predecessor_candidates_by_visibility(
            candidate_queryset,
            user,
            dependent,
        ).values_list("id", flat=True)
    )
    if set(predecessor_ids) - visible_ids:
        raise serializers.ValidationError(
            {
                "predecessor_ids": (
                    "You do not have permission to view one or more "
                    "selected predecessors."
                )
            }
        )

    if _dependency_would_create_cycle(dependent.id, predecessor_ids):
        raise serializers.ValidationError(
            {"predecessor_ids": CIRCULAR_DEPENDENCY_ERROR}
        )

    return [
        candidates_by_id[predecessor_id]
        for predecessor_id in predecessor_ids
    ]


@transaction.atomic
def replace_action_item_predecessors(dependent, predecessors, user):
    item_ids = sorted(
        {dependent.id, *(predecessor.id for predecessor in predecessors)}
    )
    locked_item_ids = list(
        ActionItem.objects.select_for_update()
        .filter(id__in=item_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if locked_item_ids != item_ids:
        raise serializers.ValidationError(
            {"predecessor_ids": "One or more Action Items no longer exist."}
        )

    locked_items = {
        action_item.id: action_item
        for action_item in (
            ActionItem.objects.filter(id__in=item_ids)
            .select_related(
                "milestone",
                "milestone__project",
                "milestone__project__user",
                "milestone__project__user__company",
                "parent_action_item",
                "parent_action_item__milestone",
                "parent_action_item__milestone__project",
                "parent_action_item__milestone__project__user",
                "parent_action_item__milestone__project__user__company",
            )
            .order_by("id")
        )
    }
    locked_dependent = locked_items.get(dependent.id)
    if locked_dependent is None:
        raise serializers.ValidationError(
            {"predecessor_ids": "One or more Action Items no longer exist."}
        )

    locked_predecessors = [
        locked_items[predecessor.id]
        for predecessor in predecessors
    ]
    validated_predecessors = validate_action_item_predecessors(
        locked_dependent,
        locked_predecessors,
        user,
    )

    locked_dependent.incoming_dependencies.filter(
        dependency_type=DEPENDENCY_TYPE
    ).delete()
    ActionItemDependency.objects.bulk_create(
        [
            ActionItemDependency(
                predecessor=predecessor,
                dependent=locked_dependent,
                dependency_type=DEPENDENCY_TYPE,
            )
            for predecessor in validated_predecessors
        ]
    )


def validate_action_item_dependents(predecessor, dependents, user):
    dependent_ids = [dependent.id for dependent in dependents]
    if len(dependent_ids) != len(set(dependent_ids)):
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "Duplicate dependent Action Items are not allowed."
                )
            }
        )
    if predecessor.id in dependent_ids:
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "An Action Item cannot depend on itself."
                )
            }
        )
    if not dependent_ids:
        return []

    candidate_queryset = get_dependent_candidate_queryset(predecessor).filter(
        id__in=dependent_ids
    )
    candidates_by_id = {
        candidate.id: candidate
        for candidate in candidate_queryset
    }
    invalid_scope_ids = set(dependent_ids) - set(candidates_by_id)
    if invalid_scope_ids:
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "Dependents must belong to the allowed scope and Action "
                    "Item level."
                )
            }
        )

    visible_ids = set(
        filter_dependency_candidates_by_visibility(
            candidate_queryset,
            user,
            predecessor,
        ).values_list("id", flat=True)
    )
    if set(dependent_ids) - visible_ids:
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "You do not have permission to view one or more selected "
                    "dependents."
                )
            }
        )

    if _dependent_replacement_would_create_cycle(
        predecessor.id,
        dependent_ids,
    ):
        raise serializers.ValidationError(
            {"dependent_ids": CIRCULAR_DEPENDENCY_ERROR}
        )

    return [
        candidates_by_id[dependent_id]
        for dependent_id in dependent_ids
    ]


@transaction.atomic
def replace_action_item_dependents(predecessor, dependents, user):
    current_dependent_ids = set(
        ActionItemDependency.objects.filter(
            predecessor=predecessor,
            dependency_type=DEPENDENCY_TYPE,
        ).values_list("dependent_id", flat=True)
    )
    selected_dependent_ids = [dependent.id for dependent in dependents]
    item_ids = sorted(
        {
            predecessor.id,
            *current_dependent_ids,
            *selected_dependent_ids,
        }
    )
    locked_item_ids = list(
        ActionItem.objects.select_for_update()
        .filter(id__in=item_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    if locked_item_ids != item_ids:
        raise serializers.ValidationError(
            {"dependent_ids": "One or more Action Items no longer exist."}
        )

    locked_items = {
        action_item.id: action_item
        for action_item in (
            ActionItem.objects.filter(id__in=item_ids)
            .select_related(
                "milestone",
                "milestone__project",
                "milestone__project__user",
                "milestone__project__user__company",
                "parent_action_item",
                "parent_action_item__milestone",
                "parent_action_item__milestone__project",
                "parent_action_item__milestone__project__user",
                "parent_action_item__milestone__project__user__company",
            )
            .prefetch_related("assignees")
            .order_by("id")
        )
    }
    locked_predecessor = locked_items.get(predecessor.id)
    if locked_predecessor is None:
        raise serializers.ValidationError(
            {"dependent_ids": "One or more Action Items no longer exist."}
        )

    locked_dependents = [
        locked_items[dependent_id]
        for dependent_id in selected_dependent_ids
    ]
    validated_dependents = validate_action_item_dependents(
        locked_predecessor,
        locked_dependents,
        user,
    )

    from project_management.permissions import ActionItemPermission

    update_permission = ActionItemPermission(action="update")
    changed_dependent_ids = current_dependent_ids ^ set(selected_dependent_ids)
    changed_dependents = [
        locked_items[item_id] for item_id in changed_dependent_ids
    ]
    if not update_permission.can_update_object(user, locked_predecessor):
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "You do not have permission to update this Action Item."
                )
            }
        )
    if any(
        not update_permission.can_update_object(user, action_item)
        for action_item in changed_dependents
    ):
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "You do not have permission to update one or more "
                    "dependent Action Items."
                )
            }
        )

    existing_dependencies = list(
        ActionItemDependency.objects.select_for_update().filter(
            predecessor=locked_predecessor,
            dependency_type=DEPENDENCY_TYPE,
        )
    )
    existing_dependent_ids = {
        dependency.dependent_id for dependency in existing_dependencies
    }
    if existing_dependent_ids != current_dependent_ids:
        raise serializers.ValidationError(
            {
                "dependent_ids": (
                    "Dependent relationships changed while updating. Please "
                    "retry."
                )
            }
        )
    selected_dependent_id_set = set(selected_dependent_ids)

    ActionItemDependency.objects.filter(
        predecessor=locked_predecessor,
        dependency_type=DEPENDENCY_TYPE,
    ).exclude(dependent_id__in=selected_dependent_id_set).delete()
    ActionItemDependency.objects.bulk_create(
        [
            ActionItemDependency(
                predecessor=locked_predecessor,
                dependent=dependent,
                dependency_type=DEPENDENCY_TYPE,
            )
            for dependent in validated_dependents
            if dependent.id not in existing_dependent_ids
        ]
    )
    return validated_dependents
