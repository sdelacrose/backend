from django.db import migrations


REMOVED_PROJECT_FIELDS = (
    "area_of_expertise",
    "commercial_model",
    "configuration_visibility",
    "contract_type",
    "expense_types",
    "international_standard",
    "invoice_status",
    "major_project_model",
    "opportunity_sales_phase",
    "project_types",
    "purchase_order_status",
    "supporting_agreement",
)


def remove_project_area_of_expertise(apps, schema_editor):
    ECPMPConfig = apps.get_model("project_management", "ECPMPConfig")
    for configuration in ECPMPConfig.objects.only(
        "id",
        "nested_configurations",
    ).iterator():
        nested = dict(configuration.nested_configurations or {})
        if nested.pop("area_of_expertise", None) is not None:
            configuration.nested_configurations = nested
            configuration.save(update_fields=["nested_configurations"])


def remove_existing_project_columns(apps, schema_editor):
    ECPMPConfig = apps.get_model("project_management", "ECPMPConfig")
    connection = schema_editor.connection
    table_name = ECPMPConfig._meta.db_table

    with connection.cursor() as cursor:
        existing_columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor,
                table_name,
            )
        }

    for field_name in REMOVED_PROJECT_FIELDS:
        field = ECPMPConfig._meta.get_field(field_name)
        if field.column in existing_columns:
            schema_editor.remove_field(ECPMPConfig, field)


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0169_extraction_source_snapshots"),
    ]

    operations = [
        migrations.RunPython(
            remove_project_area_of_expertise,
            migrations.RunPython.noop,
        ),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    remove_existing_project_columns,
                    migrations.RunPython.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(
                    model_name="ecpmpconfig",
                    name=field_name,
                )
                for field_name in REMOVED_PROJECT_FIELDS
            ],
        ),
    ]
