import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0141_product_assembly_extraction"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductAttributeExtractionRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("queued", "Queued"), ("extracting", "Extracting"), ("review_ready", "Ready for review"), ("applying", "Applying"), ("applied", "Applied"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("stale", "Source documents or Customer Demands changed")], db_index=True, default="queued", max_length=32)),
                ("schema_version", models.CharField(default="1", max_length=16)),
                ("project_language", models.CharField(default="en", max_length=16)),
                ("source_snapshot_hash", models.CharField(max_length=64)),
                ("matrix_snapshot_hash", models.CharField(max_length=64)),
                ("hint_only", models.BooleanField(default=False)),
                ("raw_result", models.JSONField(blank=True, default=dict)),
                ("result", models.JSONField(blank=True, default=dict)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("error_message", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_product_attribute_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_product_attribute_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="product_attribute_extraction_runs", to="project_management.project")),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="ProductAttributeExtractionSource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content_hash", models.CharField(max_length=64)),
                ("parser_version", models.CharField(blank=True, default="", max_length=64)),
                ("segment_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("document", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_attribute_extraction_sources", to="project_management.document")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sources", to="project_management.productattributeextractionrun")),
                ("search_index", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_attribute_extraction_sources", to="project_management.documentsearchindex")),
            ],
            options={"ordering": ["document_id"]},
        ),
        migrations.CreateModel(
            name="ProductAttributeAppliedColumn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("proposal_key", models.CharField(max_length=128)),
                ("column_key", models.CharField(max_length=128)),
                ("applied_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="applied_columns", to="project_management.productattributeextractionrun")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="ProductAttributeAppliedValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.TextField()),
                ("applied_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("applied_column", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="applied_values", to="project_management.productattributeappliedcolumn")),
                ("solution_demand", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="product_attribute_extraction_values", to="project_management.solutiondemand")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddIndex(
            model_name="productattributeextractionrun",
            index=models.Index(fields=["project", "status", "created_at"], name="attribute_run_proj_status_idx"),
        ),
        migrations.AddConstraint(
            model_name="productattributeextractionsource",
            constraint=models.UniqueConstraint(fields=("run", "document"), name="attribute_run_unique_source_doc"),
        ),
        migrations.AddConstraint(
            model_name="productattributeappliedcolumn",
            constraint=models.UniqueConstraint(fields=("run", "proposal_key"), name="attribute_run_unique_proposal_key"),
        ),
        migrations.AddConstraint(
            model_name="productattributeappliedcolumn",
            constraint=models.UniqueConstraint(fields=("run", "column_key"), name="attribute_run_unique_column_key"),
        ),
        migrations.AddConstraint(
            model_name="productattributeappliedvalue",
            constraint=models.UniqueConstraint(fields=("applied_column", "solution_demand"), name="attribute_column_unique_demand"),
        ),
    ]
