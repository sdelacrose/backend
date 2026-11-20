from django.db import migrations


def seed_validate_permission(apps, schema_editor):
    # GED-WF.1.1. Catalog only: this migration never grants the right to any role or user. Existing
    # validators are caught up explicitly and reversibly by `manage.py backfill_document_validate`.
    apps.get_model("models", "Permission").objects.get_or_create(
        key="documents.validate",
        defaults={"module": "documents", "name": "Validate documents",
                  "description": "Sign, verify, approve or reject a document when assigned to the current workflow step. Never allows editing the document."},
    )


def drop_validate_permission(apps, schema_editor):
    # Deleting the catalog row also removes every role grant (cascade): a clean rollback.
    apps.get_model("models", "Permission").objects.filter(key="documents.validate").delete()


class Migration(migrations.Migration):
    dependencies = [("project_management", "0244_private_internal_surveillance_reports"),
                    ("models", "0130_companyconfig_correspondence_types")]
    operations = [migrations.RunPython(seed_validate_permission, drop_validate_permission)]
