from django.db import migrations

CANONICAL_INVOICE_STATUS = [
    "open",
    "issued",
    "due",
    "overdue",
    "paid",
    "closed",
]
REMOVED_STATUSES = {"not paid"}


def _normalize_invoice_status(current):
    values = list(current or [])
    kept = [
        item
        for item in values
        if str(item).strip().lower() not in REMOVED_STATUSES
    ]
    lower_kept = {str(item).strip().lower() for item in kept}

    for status in CANONICAL_INVOICE_STATUS:
        if status not in lower_kept:
            kept.append(status)
            lower_kept.add(status)

    ordered = []
    remaining = list(kept)
    for status in CANONICAL_INVOICE_STATUS:
        match = next(
            (
                item
                for item in remaining
                if str(item).strip().lower() == status
            ),
            None,
        )
        if match is not None:
            ordered.append(match)
            remaining.remove(match)
    ordered.extend(remaining)
    return ordered


def update_ecpmp_invoice_statuses(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in model._meta.get_fields()}
    if "invoice_status" not in field_names:
        return

    to_update = []
    for obj in model.objects.all().only("id", "invoice_status"):
        current = list(getattr(obj, "invoice_status", []) or [])
        updated = _normalize_invoice_status(current)
        if updated != current:
            obj.invoice_status = updated
            to_update.append(obj)

    if to_update:
        model.objects.bulk_update(to_update, ["invoice_status"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0135_searchcontentsegment_searchfieldvalue_and_more"),
    ]

    operations = [
        migrations.RunPython(
            update_ecpmp_invoice_statuses,
            migrations.RunPython.noop,
        ),
    ]
