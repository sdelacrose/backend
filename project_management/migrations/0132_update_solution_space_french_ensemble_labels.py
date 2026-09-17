from django.db import migrations


FRENCH_LABEL_UPDATES = {
    "assembly_name": (
        "Nom de l'assemblage",
        "Nom de l'ensemble",
    ),
    "projects": (
        "Assemblage utilisé dans le projet",
        "Ensemble utilisé dans le projet",
    ),
}


def update_french_labels(apps, schema_editor):
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )

    configs_to_update = []
    for config in SolutionSpaceConfig.objects.all().only("id", "columns"):
        columns = list(config.columns or [])
        changed = False

        for column in columns:
            if not isinstance(column, dict):
                continue
            labels = FRENCH_LABEL_UPDATES.get(column.get("key"))
            if not labels:
                continue
            old_label, new_label = labels
            if column.get("label_fr") == old_label:
                column["label_fr"] = new_label
                changed = True

        if changed:
            config.columns = columns
            configs_to_update.append(config)

    if configs_to_update:
        SolutionSpaceConfig.objects.bulk_update(configs_to_update, ["columns"])


def restore_french_labels(apps, schema_editor):
    SolutionSpaceConfig = apps.get_model(
        "project_management",
        "SolutionSpaceConfig",
    )

    configs_to_update = []
    for config in SolutionSpaceConfig.objects.all().only("id", "columns"):
        columns = list(config.columns or [])
        changed = False

        for column in columns:
            if not isinstance(column, dict):
                continue
            labels = FRENCH_LABEL_UPDATES.get(column.get("key"))
            if not labels:
                continue
            old_label, new_label = labels
            if column.get("label_fr") == new_label:
                column["label_fr"] = old_label
                changed = True

        if changed:
            config.columns = columns
            configs_to_update.append(config)

    if configs_to_update:
        SolutionSpaceConfig.objects.bulk_update(configs_to_update, ["columns"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0131_objectassessment_rich_notes"),
    ]

    operations = [
        migrations.RunPython(update_french_labels, restore_french_labels),
    ]
