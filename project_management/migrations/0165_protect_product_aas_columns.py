from django.db import migrations


MANUFACTURER_COLUMN = {
    "key": "manufacturer",
    "label": "Manufacturer",
    "label_en": "Manufacturer",
    "label_de": "Hersteller",
    "label_fr": "Fabricant",
    "type": "text",
    "order": 0,
    "idta_semantic_id": "0173-1#02-AAO677#002",
    "system": True,
    "protected": True,
    "aas_field": "ManufacturerName",
    "aas_ownership": "aas",
}


def protect_manufacturer_column(apps, schema_editor):
    PartsListConfig = apps.get_model("project_management", "PartsListConfig")
    for config in PartsListConfig.objects.filter(project__isnull=True).iterator(
        chunk_size=500
    ):
        columns = [dict(column) for column in (config.available_product_columns or [])]
        existing_index = next(
            (
                index
                for index, column in enumerate(columns)
                if str(column.get("key") or column.get("id") or "").strip()
                == "manufacturer"
            ),
            None,
        )
        if existing_index is None:
            columns.insert(0, dict(MANUFACTURER_COLUMN))
        else:
            order = columns[existing_index].get("order", existing_index)
            columns[existing_index] = {**MANUFACTURER_COLUMN, "order": order}
        for column in columns:
            key = str(column.get("key") or column.get("id") or "").strip()
            column["aas_ownership"] = (
                "links" if key in {"description", "quantity"} else "aas"
            )
        config.available_product_columns = columns
        config.save(update_fields=["available_product_columns", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0164_company_solution_space_aas_product"),
    ]

    operations = [
        migrations.RunPython(
            protect_manufacturer_column,
            migrations.RunPython.noop,
        ),
    ]
