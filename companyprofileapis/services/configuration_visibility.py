from copy import deepcopy

from project_management.models import Project

from models.configuration_visibility import (
    FLAT_VISIBILITY_KEYS,
    disabled_flat_values,
    filter_enabled_flat_values,
)


PROJECT_CONFIGURATION_FIELDS = {
    "contract_type": "contract_type",
    "international_standard": "international_standard",
    "major_project_model": "major_project_model",
    "commercial_model": "commercial_model",
    "supporting_agreement": "supporting_agreement",
}

LEGACY_PROJECT_CONFIGURATION_REPLACEMENTS = {
    "contract_type": {
        "authority": "Other",
        "client": "Other",
        "consultant": "Other",
        "contractor": "Other",
    }
}


class ProjectConfigurationVisibilityError(ValueError):
    def __init__(self, blocked_values):
        self.blocked_values = blocked_values
        super().__init__(
            "Configuration values currently used by projects cannot be removed or hidden."
        )


def _projects_for_configuration(configuration):
    project = getattr(configuration, "project", None)
    if project is not None:
        return Project.objects.filter(pk=project.pk)

    user_id = getattr(configuration, "user_id", None)
    if user_id:
        return Project.objects.filter(
            user_id=user_id,
            solution=configuration.solution,
        )

    company_profile_id = getattr(configuration, "company_profile_id", None)
    if company_profile_id:
        return Project.objects.filter(
            company_profile_id=company_profile_id,
            solution=configuration.solution,
        )

    return Project.objects.none()


def validate_project_configuration_visibility(
    configuration,
    current_visibility,
    proposed_visibility,
):
    projects = _projects_for_configuration(configuration)
    blocked = {}

    for config_key in FLAT_VISIBILITY_KEYS:
        previous_disabled = disabled_flat_values(current_visibility, config_key)
        proposed_disabled = disabled_flat_values(proposed_visibility, config_key)
        newly_disabled = proposed_disabled - previous_disabled
        if not newly_disabled:
            continue

        project_field = PROJECT_CONFIGURATION_FIELDS[config_key]
        used_values = set(
            projects.filter(
                **{f"{project_field}__in": list(newly_disabled)}
            ).values_list(project_field, flat=True)
        )
        if used_values:
            blocked[config_key] = sorted(used_values)

    if blocked:
        raise ProjectConfigurationVisibilityError(blocked)


def validate_project_configuration_values(
    configuration,
    proposed_values,
    proposed_visibility,
):
    projects = _projects_for_configuration(configuration)
    blocked = {}

    for config_key, project_field in PROJECT_CONFIGURATION_FIELDS.items():
        enabled_values = filter_enabled_flat_values(
            proposed_values.get(config_key, []),
            proposed_visibility,
            config_key,
        )
        used_values = set(
            projects.exclude(**{f"{project_field}__isnull": True})
            .exclude(**{project_field: ""})
            .exclude(**{f"{project_field}__in": enabled_values})
            .values_list(project_field, flat=True)
        )
        if used_values:
            blocked[config_key] = sorted(used_values)

    if blocked:
        raise ProjectConfigurationVisibilityError(blocked)


def get_project_configuration_value_repairs(configuration):
    projects = _projects_for_configuration(configuration)
    missing_values = {}
    hidden_values = {}
    legacy_replacements = {}

    for config_key, project_field in PROJECT_CONFIGURATION_FIELDS.items():
        used_values = {
            value
            for value in projects.exclude(**{f"{project_field}__isnull": True})
            .exclude(**{project_field: ""})
            .values_list(project_field, flat=True)
            if value
        }
        if not used_values:
            continue

        replacement_map = LEGACY_PROJECT_CONFIGURATION_REPLACEMENTS.get(
            config_key,
            {},
        )
        replacements = {
            value: replacement_map[value]
            for value in used_values
            if value in replacement_map
        }
        effective_used_values = {
            replacement_map.get(value, value) for value in used_values
        }

        configured_values = set(getattr(configuration, config_key, []) or [])
        missing = sorted(effective_used_values - configured_values)
        hidden = sorted(
            effective_used_values
            & disabled_flat_values(
                configuration.configuration_visibility,
                config_key,
            )
        )
        if missing:
            missing_values[config_key] = missing
        if hidden:
            hidden_values[config_key] = hidden
        if replacements:
            legacy_replacements[config_key] = replacements

    return missing_values, hidden_values, legacy_replacements


def repair_project_configuration_values(configuration):
    missing_values, hidden_values, legacy_replacements = (
        get_project_configuration_value_repairs(configuration)
    )
    update_fields = []

    projects = _projects_for_configuration(configuration)
    for config_key, replacements in legacy_replacements.items():
        project_field = PROJECT_CONFIGURATION_FIELDS[config_key]
        for old_value, replacement in replacements.items():
            projects.filter(**{project_field: old_value}).update(
                **{project_field: replacement}
            )

    for config_key, values in missing_values.items():
        current_values = list(getattr(configuration, config_key, []) or [])
        setattr(configuration, config_key, [*current_values, *values])
        update_fields.append(config_key)

    if hidden_values:
        visibility = deepcopy(configuration.configuration_visibility or {})
        for config_key, values in hidden_values.items():
            entry = dict(visibility.get(config_key) or {})
            hidden_set = set(values)
            remaining = [
                value
                for value in entry.get("disabled_values", [])
                if value not in hidden_set
            ]
            if remaining:
                entry["disabled_values"] = remaining
                visibility[config_key] = entry
            else:
                entry.pop("disabled_values", None)
                if entry:
                    visibility[config_key] = entry
                else:
                    visibility.pop(config_key, None)

        configuration.configuration_visibility = visibility
        update_fields.append("configuration_visibility")

    if update_fields:
        configuration.save(update_fields=update_fields)

    return missing_values, hidden_values, legacy_replacements
