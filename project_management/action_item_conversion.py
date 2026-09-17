from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied

from project_management.action_item_dependencies import (
    filter_dependency_candidates_by_visibility,
    get_action_item_dependency_scope,
)
from project_management.models import ActionItem, ActionItemDependency
from project_management.permissions import ActionItemPermission


class MakeSubActionSerializer(serializers.Serializer):
    parent_action_item_id = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False, allow_null=True)
    target_date = serializers.DateField(required=False)
    confirm_dependency_removal = serializers.BooleanField(
        required=False,
        default=False,
    )


class ActionItemConversionConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "action_item_conversion_conflict"


def _conversion_validation_error(code, detail, field=None):
    payload = {"code": code, "detail": detail}
    if field:
        payload["field"] = field
    raise serializers.ValidationError(payload)


def validate_conversion_source(action_item):
    if action_item.parent_action_item_id:
        _conversion_validation_error(
            "already_a_sub_action",
            "This Action Item is already a sub-action.",
            "parent_action_item_id",
        )
    if str(action_item.status or "").strip().lower() == "closed":
        _conversion_validation_error(
            "closed_action_item",
            "A closed Action Item cannot be converted into a sub-action.",
        )
    if action_item.sub_action_items.exists():
        _conversion_validation_error(
            "action_item_has_children",
            (
                "An Action Item with child items cannot be converted into a "
                "sub-action."
            ),
        )


def get_action_item_parent_candidate_queryset(action_item):
    validate_conversion_source(action_item)
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
        .exclude(status__iexact="closed")
        .select_related("milestone", "milestone__project")
        .order_by(Lower("name"), "id")
    )


def filter_parent_candidates_by_visibility(queryset, user, action_item):
    return filter_dependency_candidates_by_visibility(
        queryset,
        user,
        action_item,
    )


def search_parent_candidates(queryset, search):
    search = str(search or "").strip()
    if not search:
        return queryset

    query = Q(name__icontains=search) | Q(milestone__name__icontains=search)
    if search.isdigit():
        query |= Q(id=int(search))
    return queryset.filter(query).distinct()


def serialize_parent_candidate(action_item):
    return {
        "id": action_item.id,
        "name": action_item.name,
        "start_date": action_item.start_date,
        "target_date": action_item.target_date,
        "status": action_item.status,
        "milestone": (
            {
                "id": action_item.milestone.id,
                "name": action_item.milestone.name,
            }
            if action_item.milestone
            else None
        ),
    }


def _validate_selected_parent(source, parent):
    if source.id == parent.id:
        _conversion_validation_error(
            "self_parent_not_allowed",
            "An Action Item cannot be its own parent.",
            "parent_action_item_id",
        )
    if parent.parent_action_item_id:
        _conversion_validation_error(
            "parent_is_sub_action",
            "A sub-action cannot be selected as the parent.",
            "parent_action_item_id",
        )
    if str(parent.status or "").strip().lower() == "closed":
        _conversion_validation_error(
            "closed_parent_action_item",
            "A closed Action Item cannot be selected as the parent.",
            "parent_action_item_id",
        )

    source_scope = get_action_item_dependency_scope(source)
    parent_scope = get_action_item_dependency_scope(parent)
    if source_scope[0] is None or source_scope != parent_scope:
        _conversion_validation_error(
            "parent_outside_allowed_scope",
            (
                "The parent must belong to the same company or freelancer, "
                "or to the same project as the Action Item being converted."
            ),
            "parent_action_item_id",
        )


def _isoformat(value):
    return value.isoformat() if value else None


def _validate_conversion_dates(parent, start_date, target_date):
    date_errors = {}
    if parent.start_date and start_date and start_date < parent.start_date:
        date_errors["start_date"] = (
            "Start date must not be earlier than the parent's start date."
        )
    if parent.start_date and target_date and target_date < parent.start_date:
        date_errors["target_date"] = (
            "End date must not be earlier than the parent's start date."
        )
    if parent.target_date and start_date and start_date > parent.target_date:
        date_errors["start_date"] = (
            "Start date must not be later than the parent's end date."
        )
    if parent.target_date and target_date and target_date > parent.target_date:
        date_errors["target_date"] = (
            "End date must not be later than the parent's end date."
        )
    if start_date and target_date and start_date > target_date:
        date_errors["date_range"] = (
            "Start date must be earlier than or equal to the end date."
        )

    if date_errors:
        raise ActionItemConversionConflict(
            {
                "code": "date_adjustment_required",
                "detail": (
                    "The Action Item dates must fit within the selected "
                    "parent's date range."
                ),
                "date_errors": date_errors,
                "current_dates": {
                    "start_date": _isoformat(start_date),
                    "target_date": _isoformat(target_date),
                },
                "allowed_date_range": {
                    "start_date": _isoformat(parent.start_date),
                    "target_date": _isoformat(parent.target_date),
                },
                "parent_action_item": serialize_parent_candidate(parent),
            }
        )


def _get_dependency_counts(action_item):
    predecessor_count = ActionItemDependency.objects.filter(
        dependent=action_item,
    ).count()
    dependent_count = ActionItemDependency.objects.filter(
        predecessor=action_item,
    ).count()
    return {
        "predecessors": predecessor_count,
        "dependents": dependent_count,
        "total": predecessor_count + dependent_count,
    }


@transaction.atomic
def convert_action_item_to_sub_action(
    *,
    action_item_id,
    parent_action_item_id,
    user,
    start_date=None,
    start_date_provided=False,
    target_date=None,
    target_date_provided=False,
    confirm_dependency_removal=False,
):
    if action_item_id == parent_action_item_id:
        _conversion_validation_error(
            "self_parent_not_allowed",
            "An Action Item cannot be its own parent.",
            "parent_action_item_id",
        )

    locked_item_ids = list(
        ActionItem.objects.select_for_update()
        .filter(id__in=sorted({action_item_id, parent_action_item_id}))
        .order_by("id")
        .values_list("id", flat=True)
    )
    locked_items = {
        action_item.id: action_item
        for action_item in (
            ActionItem.objects.filter(id__in=locked_item_ids)
            .select_related(
                "milestone",
                "milestone__project",
                "company_profile",
                "freelancer",
                "parent_action_item",
            )
            .order_by("id")
        )
    }
    source = locked_items.get(action_item_id)
    parent = locked_items.get(parent_action_item_id)
    if source is None:
        raise NotFound(
            {
                "code": "action_item_not_found",
                "detail": "The Action Item being converted no longer exists.",
            }
        )
    if parent is None:
        raise NotFound(
            {
                "code": "parent_action_item_not_found",
                "detail": "The selected parent Action Item no longer exists.",
                "field": "parent_action_item_id",
            }
        )

    if not ActionItemPermission(action="update").can_update_object(
        user,
        source,
    ):
        raise PermissionDenied(
            "You do not have permission to update this Action Item."
        )

    validate_conversion_source(source)
    _validate_selected_parent(source, parent)
    parent_is_visible = filter_parent_candidates_by_visibility(
        get_action_item_parent_candidate_queryset(source).filter(id=parent.id),
        user,
        source,
    ).exists()
    if not parent_is_visible:
        raise PermissionDenied(
            (
                "You do not have permission to view the selected parent "
                "Action Item."
            )
        )

    effective_start_date = start_date if start_date_provided else source.start_date
    effective_target_date = (
        target_date if target_date_provided else source.target_date
    )
    _validate_conversion_dates(
        parent,
        effective_start_date,
        effective_target_date,
    )

    dependency_ids = list(
        ActionItemDependency.objects.select_for_update()
        .filter(Q(predecessor=source) | Q(dependent=source))
        .values_list("id", flat=True)
    )
    dependency_counts = _get_dependency_counts(source)
    if dependency_counts["total"] and not confirm_dependency_removal:
        raise ActionItemConversionConflict(
            {
                "code": "dependency_confirmation_required",
                "detail": (
                    "Converting this Action Item into a sub-action will remove "
                    "all of its predecessor and dependent links."
                ),
                "dependencies": dependency_counts,
            }
        )

    if dependency_ids:
        ActionItemDependency.objects.filter(id__in=dependency_ids).delete()

    source.parent_action_item = parent
    source.milestone = None
    source.company_profile = None
    source.freelancer = None
    source.start_date = effective_start_date
    source.target_date = effective_target_date
    source.save(
        update_fields=[
            "parent_action_item",
            "milestone",
            "company_profile",
            "freelancer",
            "start_date",
            "target_date",
            "updated_at",
        ]
    )
    return source
