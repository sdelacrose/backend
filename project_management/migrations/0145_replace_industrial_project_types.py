from django.db import migrations

NEW_PROJECT_TYPES = [
    "Engineering, Design & Studies",
    "EPC / Turnkey Project",
    "Construction & Installation",
    "Commissioning & Start-up",
    "Operations & Maintenance",
    "Industrial Improvement",
    "Digital, Data & IT Project",
    "Transformation & Change",
    "Energy & Transition Project",
    "Infrastructure Project",
    "Program / Multi-Project",
    "Compliance & Regulatory Project",
]

LEGACY_DEFAULT_PROJECT_TYPES = {
    "Angebot",
    "iSFP",
    "Dachsanierung",
    "Other Project",
    "Implementation",
    "Migration",
    "Integration",
    "Other",
}


def _normalize_project_types(current):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    customs = [item for item in values if item not in LEGACY_DEFAULT_PROJECT_TYPES]

    ordered = list(NEW_PROJECT_TYPES)
    seen = set(ordered)
    for custom in customs:
        if custom not in seen:
            ordered.append(custom)
            seen.add(custom)
    return ordered


def replace_ecpmp_project_types(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in model._meta.get_fields()}
    if "project_types" not in field_names:
        return

    to_update = []
    for obj in model.objects.all().only("id", "project_types"):
        current = list(getattr(obj, "project_types", []) or [])
        updated = _normalize_project_types(current)
        if updated != current:
            obj.project_types = updated
            to_update.append(obj)

    if to_update:
        model.objects.bulk_update(to_update, ["project_types"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0144_add_assembly_type_column_to_solution_space_config"),
    ]

    operations = [
        migrations.RunPython(
            replace_ecpmp_project_types,
            migrations.RunPython.noop,
        ),
    ]
