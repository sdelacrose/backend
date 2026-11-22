# QUALITY Q1: Permission catalog rows for the Quality module (`quality.view`, `quality.full_access`).
#
# Same pattern as 0228_seed_correspondence_permissions: idempotent `get_or_create` on the existing
# Permission model, never updating an existing row, granting NOTHING to any role (an administrator
# assigns the right to roles through the existing role administration), reverse is a no-op.
from django.db import migrations

PERMISSIONS = [
    ("quality.view", "View Quality", "Open the Quality module: dashboard and supplier evaluations of the company."),
    ("quality.full_access", "Full access to Quality", "Every action of the Quality module."),
]


def seed_permissions(apps, schema_editor):
    Permission = apps.get_model("models", "Permission")
    for key, name, description in PERMISSIONS:
        Permission.objects.get_or_create(
            key=key,
            defaults={"name": name, "description": description, "module": "quality"},
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("models", "0130_companyconfig_correspondence_types"),
        ("project_management", "0258_supplier_evaluation_foundation"),
    ]

    operations = [
        migrations.RunPython(seed_permissions, noop),
    ]
