from django.db import migrations


CONTRACT_TYPE_ALIASES = {
    "Time and Material",
    "Time & Material",
    "Time & Materials",
}
CONTRACT_TYPE_VALUE = "Time and Materials"
PREVIOUS_CONTRACT_TYPE_VALUE = "Time and Material"

COMMERCIAL_MODEL_ALIASES = {
    "Time and Material",
    "Time and Materials",
    "Time & Material",
}
COMMERCIAL_MODEL_VALUE = "Time & Materials"
PREVIOUS_COMMERCIAL_MODEL_VALUE = "Time & Material"


def _remap_list(values, aliases, replacement):
    remapped = []
    seen = set()
    for item in values or []:
        value = replacement if item in aliases else item
        if value in seen:
            continue
        remapped.append(value)
        seen.add(value)
    return remapped


def _update_project_values(apps, contract_value, commercial_value):
    project_model = apps.get_model("project_management", "Project")
    to_update = []
    for project in project_model.objects.all().only(
        "id",
        "contract_type",
        "commercial_model",
    ):
        changed = False
        if project.contract_type in CONTRACT_TYPE_ALIASES:
            project.contract_type = contract_value
            changed = True
        if project.commercial_model in COMMERCIAL_MODEL_ALIASES:
            project.commercial_model = commercial_value
            changed = True
        if changed:
            to_update.append(project)

    if to_update:
        project_model.objects.bulk_update(
            to_update,
            ["contract_type", "commercial_model"],
        )


def _update_config_values(apps, contract_value, commercial_value):
    config_model = apps.get_model("project_management", "ECPMPConfig")
    to_update = []
    for config in config_model.objects.all().only(
        "id",
        "contract_type",
        "commercial_model",
    ):
        contract_types = list(config.contract_type or [])
        commercial_models = list(config.commercial_model or [])
        updated_contract_types = _remap_list(
            contract_types,
            CONTRACT_TYPE_ALIASES,
            contract_value,
        )
        updated_commercial_models = _remap_list(
            commercial_models,
            COMMERCIAL_MODEL_ALIASES,
            commercial_value,
        )
        if (
            updated_contract_types == contract_types
            and updated_commercial_models == commercial_models
        ):
            continue

        config.contract_type = updated_contract_types
        config.commercial_model = updated_commercial_models
        to_update.append(config)

    if to_update:
        config_model.objects.bulk_update(
            to_update,
            ["contract_type", "commercial_model"],
        )


def pluralize_time_and_material_options(apps, schema_editor):
    _update_project_values(
        apps,
        CONTRACT_TYPE_VALUE,
        COMMERCIAL_MODEL_VALUE,
    )
    _update_config_values(
        apps,
        CONTRACT_TYPE_VALUE,
        COMMERCIAL_MODEL_VALUE,
    )


def reverse_pluralize_time_and_material_options(apps, schema_editor):
    _update_project_values(
        apps,
        PREVIOUS_CONTRACT_TYPE_VALUE,
        PREVIOUS_COMMERCIAL_MODEL_VALUE,
    )
    _update_config_values(
        apps,
        PREVIOUS_CONTRACT_TYPE_VALUE,
        PREVIOUS_COMMERCIAL_MODEL_VALUE,
    )


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0161_remap_commercial_model_fixed_price_to_fixed_price_model",
        ),
    ]

    operations = [
        migrations.RunPython(
            pluralize_time_and_material_options,
            reverse_code=reverse_pluralize_time_and_material_options,
        ),
    ]
