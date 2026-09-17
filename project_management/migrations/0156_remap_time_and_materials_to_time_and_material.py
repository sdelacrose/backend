from django.db import migrations


OLD_VALUE = "Time & Materials"
NEW_VALUE = "Time and Material"
CONFIG_FIELDS = (
    "contract_type",
    "commercial_model",
)
PROJECT_FIELDS = (
    "contract_type",
    "commercial_model",
)


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


def remap_time_and_material(apps, schema_editor):
    project_model = apps.get_model("project_management", "Project")
    project_field_names = {field.name for field in project_model._meta.get_fields()}
    project_update_fields = [
        field for field in PROJECT_FIELDS if field in project_field_names
    ]
    if project_update_fields:
        to_update = []
        for obj in project_model.objects.all().only("id", *project_update_fields):
            changed = False
            for field in project_update_fields:
                current = getattr(obj, field, None)
                if current == OLD_VALUE:
                    setattr(obj, field, NEW_VALUE)
                    changed = True
            if changed:
                to_update.append(obj)
        if to_update:
            project_model.objects.bulk_update(to_update, project_update_fields)

    config_model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in config_model._meta.get_fields()}
    update_fields = [field for field in CONFIG_FIELDS if field in field_names]
    if not update_fields:
        return

    to_update = []
    for obj in config_model.objects.all().only("id", *update_fields):
        changed = False
        for field in update_fields:
            current = list(getattr(obj, field, []) or [])
            updated = _remap_list(current)
            if updated != current:
                setattr(obj, field, updated)
                changed = True
        if changed:
            to_update.append(obj)
    if to_update:
        config_model.objects.bulk_update(to_update, update_fields)


def reverse_remap_time_and_material(apps, schema_editor):
    project_model = apps.get_model("project_management", "Project")
    project_field_names = {field.name for field in project_model._meta.get_fields()}
    project_update_fields = [
        field for field in PROJECT_FIELDS if field in project_field_names
    ]
    if project_update_fields:
        to_update = []
        for obj in project_model.objects.all().only("id", *project_update_fields):
            changed = False
            for field in project_update_fields:
                current = getattr(obj, field, None)
                if current == NEW_VALUE:
                    setattr(obj, field, OLD_VALUE)
                    changed = True
            if changed:
                to_update.append(obj)
        if to_update:
            project_model.objects.bulk_update(to_update, project_update_fields)

    config_model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in config_model._meta.get_fields()}
    update_fields = [field for field in CONFIG_FIELDS if field in field_names]
    if not update_fields:
        return

    to_update = []
    for obj in config_model.objects.all().only("id", *update_fields):
        changed = False
        for field in update_fields:
            current = list(getattr(obj, field, []) or [])
            updated = []
            seen = set()
            for item in current:
                value = OLD_VALUE if item == NEW_VALUE else item
                if value in seen:
                    continue
                updated.append(value)
                seen.add(value)
            if updated != current:
                setattr(obj, field, updated)
                changed = True
        if changed:
            to_update.append(obj)
    if to_update:
        config_model.objects.bulk_update(to_update, update_fields)


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0155_project_contract_standard_commercial_model_supporting_agreement",
        ),
    ]

    operations = [
        migrations.RunPython(
            remap_time_and_material,
            reverse_code=reverse_remap_time_and_material,
        ),
    ]
