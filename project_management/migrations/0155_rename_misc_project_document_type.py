from django.db import migrations


MISCELLANEOUS_LABELS = {
    "Miscellaneous",
    "Miscellaneous (not covered by any specific document type above)",
}
OTHER_DOCUMENT_TYPE = "Other:OTH"


def _normalize_document_type(item):
    value = str(item).strip()
    if not value:
        return ""

    label = value.rsplit(":", 1)[0].strip()
    if label in MISCELLANEOUS_LABELS:
        return OTHER_DOCUMENT_TYPE
    return value


def rename_miscellaneous_document_type(apps, schema_editor):
    model = apps.get_model("project_management", "ECPMPConfig")
    field_names = {field.name for field in model._meta.get_fields()}
    if "document_types" not in field_names:
        return

    to_update = []
    for obj in model.objects.all().only("id", "document_types"):
        normalized = []
        seen_labels = set()

        for item in getattr(obj, "document_types", []) or []:
            value = _normalize_document_type(item)
            if not value:
                continue

            label = value.rsplit(":", 1)[0].strip()
            if label in seen_labels:
                continue

            normalized.append(value)
            seen_labels.add(label)

        if normalized != list(getattr(obj, "document_types", []) or []):
            obj.document_types = normalized
            to_update.append(obj)

    if to_update:
        model.objects.bulk_update(to_update, ["document_types"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0154_historicaldocument_review_status_fields"),
    ]

    operations = [
        migrations.RunPython(
            rename_miscellaneous_document_type,
            migrations.RunPython.noop,
        ),
    ]
