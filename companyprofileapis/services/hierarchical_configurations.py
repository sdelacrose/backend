from django.db.models import Q
from rest_framework import serializers

from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    DOCUMENT_DISCIPLINES_KEY,
    resolve_nested_configurations,
)
from project_management.models import ContactCompany, ContactUser, Document


def _nested_visibility_ids(visibility, field_name):
    source = visibility if isinstance(visibility, dict) else {}
    entry = source.get(AREA_OF_EXPERTISE_KEY, {})
    if not isinstance(entry, dict):
        return set()
    return {
        str(value)
        for value in entry.get(field_name, [])
        if str(value or "").strip()
    }


def _area_of_expertise_labels(nested_configurations):
    hierarchy = resolve_nested_configurations(nested_configurations).get(
        AREA_OF_EXPERTISE_KEY,
        {},
    )
    labels = {}
    for group in hierarchy.get("groups", []):
        group_id = str(group.get("id") or "").strip()
        if group_id:
            labels[group_id] = str(group.get("label") or group_id)
        for child in group.get("children", []):
            child_id = str(child.get("id") or "").strip()
            if child_id:
                labels[child_id] = str(child.get("label") or child_id)
    return labels


def _contact_expertise_values_in_use(configuration, field_name, values):
    if not values:
        return set()

    user_id = getattr(configuration, "user_id", None)
    company_id = getattr(configuration, "company_profile_id", None)
    if user_id:
        owner_filter = {"user_id": user_id}
    elif company_id:
        owner_filter = {"company_profile_id": company_id}
    else:
        return set()

    is_multiple_value_field = field_name == "sub_area_of_expertise"
    lookup_suffix = "overlap" if is_multiple_value_field else "in"
    lookup = {f"{field_name}__{lookup_suffix}": values}
    used = set()
    for model in (ContactCompany, ContactUser):
        stored_values = model.objects.filter(
            **owner_filter,
            **lookup,
        ).values_list(field_name, flat=True)
        if is_multiple_value_field:
            for stored_value_list in stored_values:
                used.update(
                    str(value)
                    for value in (stored_value_list or [])
                    if str(value) in values
                )
        else:
            used.update(str(value) for value in stored_values if value)
    return used


def validate_area_of_expertise_visibility(
    configuration,
    nested_configurations,
    current_visibility,
    next_visibility,
):
    if not configuration:
        return

    newly_disabled_groups = _nested_visibility_ids(
        next_visibility,
        "disabled_group_ids",
    ) - _nested_visibility_ids(current_visibility, "disabled_group_ids")
    newly_disabled_children = _nested_visibility_ids(
        next_visibility,
        "disabled_child_ids",
    ) - _nested_visibility_ids(current_visibility, "disabled_child_ids")
    if not newly_disabled_groups and not newly_disabled_children:
        return

    blocked_ids = _contact_expertise_values_in_use(
        configuration,
        "main_area_of_expertise",
        newly_disabled_groups,
    )
    blocked_ids.update(
        _contact_expertise_values_in_use(
            configuration,
            "sub_area_of_expertise",
            newly_disabled_children,
        )
    )
    if not blocked_ids:
        return

    labels = _area_of_expertise_labels(nested_configurations)
    blocked_labels = sorted(labels.get(value, value) for value in blocked_ids)
    raise serializers.ValidationError(
        {
            "configuration_visibility": {
                AREA_OF_EXPERTISE_KEY: {
                    "code": "configuration_value_in_use",
                    "values": blocked_labels,
                }
            }
        }
    )


def validate_removed_document_disciplines(configuration, next_nested):
    if not configuration:
        return

    current_hierarchy = resolve_nested_configurations(
        configuration.nested_configurations
    )[DOCUMENT_DISCIPLINES_KEY]
    next_hierarchy = resolve_nested_configurations(next_nested)[
        DOCUMENT_DISCIPLINES_KEY
    ]
    current_children = {
        child["id"]: child
        for group in current_hierarchy["groups"]
        for child in group["children"]
    }
    next_child_ids = {
        child["id"]
        for group in next_hierarchy["groups"]
        for child in group["children"]
    }
    removed_children = [
        child
        for child_id, child in current_children.items()
        if child_id not in next_child_ids
    ]
    if not removed_children:
        return

    project_id = getattr(configuration, "project_id", None)
    user_id = getattr(configuration, "user_id", None)
    company_id = getattr(configuration, "company_profile_id", None)

    if project_id:
        scope = Q(folder__project_id=project_id)
    elif user_id:
        scope = Q(folder__freelancer_id=user_id) | Q(
            folder__project__user_id=user_id
        )
    else:
        scope = (
            Q(folder__company_profile_id=company_id)
            | Q(folder__project__company_profile_id=company_id)
            | Q(folder__project__user__company_id=company_id)
        )

    removed_ids = [child["id"] for child in removed_children]
    removed_labels = [child["label"] for child in removed_children]
    linked = Document.objects.filter(scope).filter(
        Q(discipline_option_id__in=removed_ids)
        | (Q(discipline_option_id__isnull=True) | Q(discipline_option_id=""))
        & Q(discipline__in=removed_labels)
    )
    if linked.exists():
        raise serializers.ValidationError(
            {
                "nested_configurations": (
                    "Document disciplines linked to existing documents "
                    "cannot be deleted."
                )
            }
        )
