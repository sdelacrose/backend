from django.db import migrations


OLD_LABEL = "Type d'assemblage"
NEW_LABEL = "Type d'ensemble"


def update_assembly_type_french_label(apps, schema_editor):
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )
    database_alias = schema_editor.connection.alias
    configs_to_update = []

    for config in SolutionSpaceConfig.objects.using(database_alias).only(
        "id",
        "columns",
    ):
        columns = list(config.columns or [])
        changed = False

        for column in columns:
            if (
                isinstance(column, dict)
                and column.get("key") == "assembly_type"
                and column.get("label_fr") == OLD_LABEL
            ):
                column["label_fr"] = NEW_LABEL
                changed = True

        if changed:
            config.columns = columns
            configs_to_update.append(config)

    if configs_to_update:
        SolutionSpaceConfig.objects.using(database_alias).bulk_update(
            configs_to_update,
            ["columns"],
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0172_backfill_legacy_assembly_types",
        ),
    ]

    operations = [
        migrations.RunPython(
            update_assembly_type_french_label,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
