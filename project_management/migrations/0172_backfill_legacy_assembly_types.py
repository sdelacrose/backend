from django.db import migrations, models


PRODUCT_ASSEMBLY = "components"
PROCESS_ASSEMBLY = "production_lines"


def backfill_legacy_assembly_types(apps, schema_editor):
    database_alias = schema_editor.connection.alias
    CompanyItem = apps.get_model(
        "project_management",
        "CompanySolutionSpaceItem",
    )
    ProjectItem = apps.get_model(
        "project_management",
        "ProjectSolutionSpaceItem",
    )

    company_rows = list(
        CompanyItem.objects.using(database_alias).order_by("level", "sort_order", "id")
    )
    resolved_types = {}
    company_updates = []
    for row in company_rows:
        assembly_type = row.assembly_type
        if assembly_type not in {PRODUCT_ASSEMBLY, PROCESS_ASSEMBLY}:
            assembly_type = resolved_types.get(row.parent_id, PRODUCT_ASSEMBLY)
            row.assembly_type = assembly_type
            company_updates.append(row)
        resolved_types[row.id] = assembly_type

    if company_updates:
        CompanyItem.objects.using(database_alias).bulk_update(
            company_updates,
            ["assembly_type"],
        )

    project_rows = (
        ProjectItem.objects.using(database_alias)
        .filter(source_company_item__isnull=False)
        .select_related("source_company_item")
    )
    project_updates = []
    for row in project_rows:
        source_type = row.source_company_item.assembly_type
        if (
            row.assembly_type not in {PRODUCT_ASSEMBLY, PROCESS_ASSEMBLY}
            or row.assembly_type != source_type
        ):
            row.assembly_type = source_type
            project_updates.append(row)

    if project_updates:
        ProjectItem.objects.using(database_alias).bulk_update(
            project_updates,
            ["assembly_type"],
        )


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0171_company_assembly_production_links",
        ),
    ]

    operations = [
        migrations.RunPython(
            backfill_legacy_assembly_types,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="companysolutionspaceitem",
            name="assembly_type",
            field=models.CharField(
                blank=True,
                choices=[
                    (PRODUCT_ASSEMBLY, "Components"),
                    (PROCESS_ASSEMBLY, "Production Lines"),
                ],
                db_index=True,
                default=PRODUCT_ASSEMBLY,
                max_length=32,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="projectsolutionspaceitem",
            name="assembly_type",
            field=models.CharField(
                blank=True,
                choices=[
                    (PRODUCT_ASSEMBLY, "Components"),
                    (PROCESS_ASSEMBLY, "Production Lines"),
                ],
                db_index=True,
                default=PRODUCT_ASSEMBLY,
                max_length=32,
                null=True,
            ),
        ),
    ]
