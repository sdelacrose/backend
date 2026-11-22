# QUALITY Q3: Permission catalog row `quality.evaluation_manage` (start an evaluation from a purchase order and
# fill its questionnaire). Reading stays `quality.view`. Same idempotent pattern as 0259 / 0261: get_or_create
# only, nobody is granted anything, reverse is a no-op.
from django.db import migrations


def seed_permission(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    Permission.objects.get_or_create(
        key="quality.evaluation_manage",
        defaults={
            "name": "Manage supplier evaluations",
            "description": "Start a supplier evaluation from a purchase order and fill in its questionnaire.",
            "module": "quality",
        },
    )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0130_companyconfig_correspondence_types"),
        ("project_management", "0262_evaluation_responses_and_references"),
    ]

    operations = [migrations.RunPython(seed_permission, noop)]
