from django.db import migrations, models
import django.db.models.deletion


def backfill_source_snapshots(apps, schema_editor):
    for model_name in (
        "ProductAssemblyExtractionSource",
        "ProductAttributeExtractionSource",
    ):
        source_model = apps.get_model("project_management", model_name)
        for source in source_model.objects.select_related("document").iterator(
            chunk_size=500
        ):
            source.document_id_snapshot = source.document_id
            source.search_index_id_snapshot = source.search_index_id
            source.document_name = source.document.file_name or ""
            source.save(
                update_fields=[
                    "document_id_snapshot",
                    "search_index_id_snapshot",
                    "document_name",
                ]
            )


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0168_project_content_foundation"),
    ]

    operations = [
        migrations.AddField(
            model_name="productassemblyextractionrun",
            name="source_selection_mode",
            field=models.CharField(
                choices=[
                    ("all_current", "All current eligible documents"),
                    ("explicit", "Explicitly selected documents"),
                ],
                default="all_current",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="productattributeextractionrun",
            name="source_selection_mode",
            field=models.CharField(
                choices=[
                    ("all_current", "All current eligible documents"),
                    ("explicit", "Explicitly selected documents"),
                ],
                default="all_current",
                max_length=16,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="productassemblyextractionsource",
            name="assembly_run_unique_source_doc",
        ),
        migrations.RemoveConstraint(
            model_name="productattributeextractionsource",
            name="attribute_run_unique_source_doc",
        ),
        migrations.AddField(
            model_name="productassemblyextractionsource",
            name="document_id_snapshot",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="productassemblyextractionsource",
            name="document_name",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="productassemblyextractionsource",
            name="search_index_id_snapshot",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="productattributeextractionsource",
            name="document_id_snapshot",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="productattributeextractionsource",
            name="document_name",
            field=models.CharField(blank=True, default="", max_length=500),
        ),
        migrations.AddField(
            model_name="productattributeextractionsource",
            name="search_index_id_snapshot",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AlterField(
            model_name="productassemblyextractionsource",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_assembly_extraction_sources",
                to="project_management.document",
            ),
        ),
        migrations.AlterField(
            model_name="productassemblyextractionsource",
            name="search_index",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_assembly_extraction_sources",
                to="project_management.documentsearchindex",
            ),
        ),
        migrations.AlterField(
            model_name="productattributeextractionsource",
            name="document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_attribute_extraction_sources",
                to="project_management.document",
            ),
        ),
        migrations.AlterField(
            model_name="productattributeextractionsource",
            name="search_index",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="product_attribute_extraction_sources",
                to="project_management.documentsearchindex",
            ),
        ),
        migrations.RunPython(backfill_source_snapshots, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="productassemblyextractionsource",
            name="document_id_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AlterField(
            model_name="productassemblyextractionsource",
            name="search_index_id_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AlterField(
            model_name="productattributeextractionsource",
            name="document_id_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AlterField(
            model_name="productattributeextractionsource",
            name="search_index_id_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AddConstraint(
            model_name="productassemblyextractionsource",
            constraint=models.UniqueConstraint(
                fields=("run", "document_id_snapshot"),
                name="assembly_run_unique_source_doc",
            ),
        ),
        migrations.AddConstraint(
            model_name="productattributeextractionsource",
            constraint=models.UniqueConstraint(
                fields=("run", "document_id_snapshot"),
                name="attribute_run_unique_source_doc",
            ),
        ),
        migrations.AlterModelOptions(
            name="productassemblyextractionsource",
            options={"ordering": ["document_id_snapshot"]},
        ),
        migrations.AlterModelOptions(
            name="productattributeextractionsource",
            options={"ordering": ["document_id_snapshot"]},
        ),
    ]
