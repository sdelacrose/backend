from django.db import migrations


ASSEMBLY_TYPE_COLUMN = {
    "key": "assembly_type",
    "label": "Assembly Type",
    "label_en": "Assembly Type",
    "label_de": "Baugruppentyp",
    "label_fr": "Type d'assemblage",
    "type": "assembly_type",
    "scope": "both",
    "order": 1,
    "visible": True,
    "system": True,
    "idta_semantic_id": "",
}


def add_assembly_type_column(apps, schema_editor):
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )
    configs_to_update = []

    for config in SolutionSpaceConfig.objects.all().only("id", "columns"):
        columns = list(config.columns or [])
        if any(
            isinstance(column, dict) and column.get("key") == "assembly_type"
            for column in columns
        ):
            continue

        insert_at = 1
        for index, column in enumerate(columns):
            if isinstance(column, dict) and column.get("key") == "assembly_name":
                insert_at = index + 1
                break

        columns.insert(insert_at, {**ASSEMBLY_TYPE_COLUMN, "order": insert_at})
        for index, column in enumerate(columns):
            if isinstance(column, dict):
                column["order"] = index

        config.columns = columns
        configs_to_update.append(config)

    if configs_to_update:
        SolutionSpaceConfig.objects.bulk_update(configs_to_update, ["columns"])


def remove_assembly_type_column(apps, schema_editor):
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )
    configs_to_update = []

    for config in SolutionSpaceConfig.objects.all().only("id", "columns"):
        columns = [
            column
            for column in list(config.columns or [])
            if not (isinstance(column, dict) and column.get("key") == "assembly_type")
        ]
        if len(columns) == len(list(config.columns or [])):
            continue

        for index, column in enumerate(columns):
            if isinstance(column, dict):
                column["order"] = index

        config.columns = columns
        configs_to_update.append(config)

    if configs_to_update:
        SolutionSpaceConfig.objects.bulk_update(configs_to_update, ["columns"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0143_solution_space_assembly_type"),
    ]

    operations = [
        migrations.RunPython(
            add_assembly_type_column,
            reverse_code=remove_assembly_type_column,
        ),
    ]
