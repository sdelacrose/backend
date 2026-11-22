# QUALITY Q2: Permission catalog row `quality.template_manage` (create / edit / activate / duplicate the
# supplier evaluation templates). Same idempotent pattern as 0259: get_or_create only, no role is granted
# anything, reverse is a no-op.
from django.db import migrations


def seed_permission(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    Permission.objects.get_or_create(
        key="quality.template_manage",
        defaults={
            "name": "Manage evaluation templates",
            "description": "Create, edit, activate/deactivate and duplicate supplier evaluation templates.",
            "module": "quality",
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0130_companyconfig_correspondence_types"),
        ("project_management", "0260_evaluation_templates"),
    ]

    operations = [migrations.RunPython(seed_permission, noop)]
