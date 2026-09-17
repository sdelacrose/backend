from django.db import migrations


OLD_VALUE = "Fixed Price"
NEW_VALUE = "Fixed Price Model"


def _remap_list(values):
    remapped = []
    seen = set()
    for item in values or []:
        value = NEW_VALUE if item == OLD_VALUE else item
        if value in seen:
            continue
        remapped.append(value)
        seen.add(value)
    return remapped


def remap_commercial_model(apps, schema_editor):
    project_model = apps.get_model("project_management", "Project")
    project_field_names = {field.name for field in project_model._meta.get_fields()}
    if "commercial_model" in project_field_names:
        to_update = []
        for obj in project_model.objects.all().only("id", "commercial_model"):
            if getattr(obj, "commercial_model", None) == OLD_VALUE:
                obj.commercial_model = NEW_VALUE
                to_update.append(obj)
        if to_update:
            project_model.objects.bulk_update(to_update, ["commercial_model"])

    config_model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in config_model._meta.get_fields()}
    if "commercial_model" not in field_names:
        return

    to_update = []
    for obj in config_model.objects.all().only("id", "commercial_model"):
        current = list(getattr(obj, "commercial_model", []) or [])
        updated = _remap_list(current)
        if updated != current:
            obj.commercial_model = updated
            to_update.append(obj)
    if to_update:
        config_model.objects.bulk_update(to_update, ["commercial_model"])


def reverse_remap_commercial_model(apps, schema_editor):
    project_model = apps.get_model("project_management", "Project")
    project_field_names = {field.name for field in project_model._meta.get_fields()}
    if "commercial_model" in project_field_names:
        to_update = []
        for obj in project_model.objects.all().only("id", "commercial_model"):
            if getattr(obj, "commercial_model", None) == NEW_VALUE:
                obj.commercial_model = OLD_VALUE
                to_update.append(obj)
        if to_update:
            project_model.objects.bulk_update(to_update, ["commercial_model"])

    config_model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in config_model._meta.get_fields()}
    if "commercial_model" not in field_names:
        return

    to_update = []
    for obj in config_model.objects.all().only("id", "commercial_model"):
        current = list(getattr(obj, "commercial_model", []) or [])
        updated = []
        seen = set()
        for item in current:
            value = OLD_VALUE if item == NEW_VALUE else item
            if value in seen:
                continue
            updated.append(value)
            seen.add(value)
        if updated != current:
            obj.commercial_model = updated
            to_update.append(obj)
    if to_update:
        config_model.objects.bulk_update(to_update, ["commercial_model"])


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0160_remap_commercial_model_time_and_material_to_ampersand",
        ),
    ]

    operations = [
        migrations.RunPython(
            remap_commercial_model,
            reverse_code=reverse_remap_commercial_model,
        ),
    ]
