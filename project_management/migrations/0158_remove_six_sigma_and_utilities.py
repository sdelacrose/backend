from django.db import migrations


AREA_OF_EXPERTISE_KEY = "area_of_expertise"
REMOVED_IDS = {"six-sigma", "utilities"}
REMOVED_LABELS = {"six sigma", "utilities"}


def remove_area_of_expertise_options(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    for instance in model.objects.all().iterator():
        nested = dict(instance.nested_configurations or {})
        hierarchy = dict(nested.get(AREA_OF_EXPERTISE_KEY) or {})
        groups = []
        nested_changed = False

        for raw_group in hierarchy.get("groups", []):
            if not isinstance(raw_group, dict):
                groups.append(raw_group)
                continue

            group = dict(raw_group)
            current_children = list(group.get("children", []))
            children = [
                child
                for child in current_children
                if not isinstance(child, dict)
                or (
                    str(child.get("id", "")).strip().lower()
                    not in REMOVED_IDS
                    and str(child.get("label", "")).strip().lower()
                    not in REMOVED_LABELS
                )
            ]
            if len(children) != len(current_children):
                nested_changed = True
            group["children"] = children
            groups.append(group)

        update_fields = []
        if nested_changed:
            hierarchy["groups"] = groups
            nested[AREA_OF_EXPERTISE_KEY] = hierarchy
            instance.nested_configurations = nested
            update_fields.append("nested_configurations")

        current_flat = list(instance.area_of_expertise or [])
        filtered_flat = [
            value
            for value in current_flat
            if str(value).strip().lower() not in REMOVED_LABELS
        ]
        if filtered_flat != current_flat:
            instance.area_of_expertise = filtered_flat
            update_fields.append("area_of_expertise")

        if update_fields:
            instance.save(update_fields=update_fields)


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0157_merge_hierarchy_and_time_material"),
    ]

    operations = [
        migrations.RunPython(
            remove_area_of_expertise_options,
            migrations.RunPython.noop,
        ),
    ]
