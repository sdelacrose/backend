from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models

NEW_DOCUMENT_STATUSES = [
    "Draft",
    "Under review",
    "Submitted",
    "Approved",
    "Sent",
    "To be updated",
    "For construction (IFC)",
    "Issued",
    "Revised",
    "Final",
    "As-Built",
    "On Hold",
    "Cancelled",
    "Archived",
    "Other",
]
NEW_DOCUMENT_INTERNAL_REVIEW_STATUSES = [
    "In progress",
    "Approved",
    "Commented",
    "Rejected",
]
NEW_DOCUMENT_EXTERNAL_REVIEW_STATUSES = [
    "In progress",
    "Approved",
    "Reviewed",
    "Commented",
    "Rejected",
    "For information",
    "Superseded",
]
NEW_TRANSMITTAL_CATEGORIES = [
    "Not sent",
    "Available",
    "For review",
    "For information",
]

LEGACY_DOCUMENT_STATUS_RENAMES = {
    "In review": "Under review",
    "Complete": "Approved",
}
LEGACY_DOCUMENT_STATUS_DEFAULTS = {
    "Draft",
    "In review",
}


def _normalize_document_status(current):
    values = []
    for item in current or []:
        value = str(item).strip()
        if not value:
            continue
        values.append(LEGACY_DOCUMENT_STATUS_RENAMES.get(value, value))

    customs = [
        value
        for value in values
        if value not in NEW_DOCUMENT_STATUSES and value not in LEGACY_DOCUMENT_STATUS_DEFAULTS
    ]

    ordered = list(NEW_DOCUMENT_STATUSES)
    seen = set(ordered)
    for custom in customs:
        if custom not in seen:
            ordered.append(custom)
            seen.add(custom)
    return ordered


def _seed_list_field(current, defaults):
    values = [str(item).strip() for item in (current or []) if str(item).strip()]
    if not values:
        return list(defaults)

    ordered = list(defaults)
    seen = set(ordered)
    for value in values:
        if value not in seen:
            ordered.append(value)
            seen.add(value)
    return ordered


def update_ecpmp_document_status_configs(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in model._meta.get_fields()}
    update_fields = [
        field
        for field in (
            "document_status",
            "document_internal_review_status",
            "document_external_review_status",
            "transmittal_category",
        )
        if field in field_names
    ]
    if not update_fields:
        return

    to_update = []
    for obj in model.objects.all().only("id", *update_fields):
        changed = False

        if "document_status" in update_fields:
            current = list(getattr(obj, "document_status", []) or [])
            updated = _normalize_document_status(current)
            if updated != current:
                obj.document_status = updated
                changed = True

        if "document_internal_review_status" in update_fields:
            current = list(getattr(obj, "document_internal_review_status", []) or [])
            updated = _seed_list_field(current, NEW_DOCUMENT_INTERNAL_REVIEW_STATUSES)
            if updated != current:
                obj.document_internal_review_status = updated
                changed = True

        if "document_external_review_status" in update_fields:
            current = list(getattr(obj, "document_external_review_status", []) or [])
            updated = _seed_list_field(current, NEW_DOCUMENT_EXTERNAL_REVIEW_STATUSES)
            if updated != current:
                obj.document_external_review_status = updated
                changed = True

        if "transmittal_category" in update_fields:
            current = list(getattr(obj, "transmittal_category", []) or [])
            updated = _seed_list_field(current, NEW_TRANSMITTAL_CATEGORIES)
            if updated != current:
                obj.transmittal_category = updated
                changed = True

        if changed:
            to_update.append(obj)

    if to_update:
        model.objects.bulk_update(to_update, update_fields)


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0151_update_project_document_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="document_internal_review_status",
            field=ArrayField(
                models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="ecpmpconfig",
            name="document_external_review_status",
            field=ArrayField(
                models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="ecpmpconfig",
            name="transmittal_category",
            field=ArrayField(
                models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.RunPython(
            update_ecpmp_document_status_configs,
            migrations.RunPython.noop,
        ),
    ]
