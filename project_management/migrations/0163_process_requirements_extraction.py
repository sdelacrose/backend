from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0162_pluralize_time_and_material_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessAssemblyExtractionRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("queued", "Queued"), ("extracting", "Extracting"), ("review_ready", "Ready for review"), ("applying", "Applying"), ("applied", "Applied"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("stale", "Demand, Solution Space, or Requirements changed")], db_index=True, default="queued", max_length=32)),
                ("schema_version", models.CharField(default="1", max_length=16)),
                ("project_language", models.CharField(default="en", max_length=16)),
                ("input_snapshot_hash", models.CharField(max_length=64)),
                ("input_snapshot", models.JSONField(blank=True, default=dict)),
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
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_process_assembly_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_process_assembly_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="process_assembly_extraction_runs", to="project_management.project")),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="ProcessAttributeExtractionRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("queued", "Queued"), ("extracting", "Extracting"), ("review_ready", "Ready for review"), ("applying", "Applying"), ("applied", "Applied"), ("failed", "Failed"), ("cancelled", "Cancelled"), ("stale", "Requirements, Demands, or Solution Space changed")], db_index=True, default="queued", max_length=32)),
                ("schema_version", models.CharField(default="1", max_length=16)),
                ("project_language", models.CharField(default="en", max_length=16)),
                ("input_snapshot_hash", models.CharField(max_length=64)),
                ("input_snapshot", models.JSONField(blank=True, default=dict)),
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
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_process_attribute_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_process_attribute_extraction_runs", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="process_attribute_extraction_runs", to="project_management.project")),
            ],
            options={"ordering": ["-id"]},
        ),
        migrations.CreateModel(
            name="ProcessAssemblyAppliedRow",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("proposal_key", models.CharField(max_length=128)),
                ("applied_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("requirement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="process_assembly_extraction_rows", to="project_management.requirement")),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="applied_rows", to="project_management.processassemblyextractionrun")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="ProcessAttributeAppliedColumn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("proposal_key", models.CharField(max_length=128)),
                ("column_key", models.CharField(max_length=128)),
                ("applied_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="applied_columns", to="project_management.processattributeextractionrun")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.CreateModel(
            name="ProcessAttributeAppliedValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.TextField()),
                ("applied_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("applied_column", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="applied_values", to="project_management.processattributeappliedcolumn")),
                ("requirement", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="process_attribute_extraction_values", to="project_management.requirement")),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddIndex(model_name="processassemblyextractionrun", index=models.Index(fields=["project", "status", "created_at"], name="process_assembly_run_idx")),
        migrations.AddIndex(model_name="processattributeextractionrun", index=models.Index(fields=["project", "status", "created_at"], name="process_attribute_run_idx")),
        migrations.AddConstraint(model_name="processassemblyappliedrow", constraint=models.UniqueConstraint(fields=("run", "proposal_key"), name="process_run_unique_proposal")),
        migrations.AddConstraint(model_name="processassemblyappliedrow", constraint=models.UniqueConstraint(fields=("run", "requirement"), name="process_run_unique_requirement")),
        migrations.AddConstraint(model_name="processattributeappliedcolumn", constraint=models.UniqueConstraint(fields=("run", "proposal_key"), name="process_attr_unique_proposal")),
        migrations.AddConstraint(model_name="processattributeappliedcolumn", constraint=models.UniqueConstraint(fields=("run", "column_key"), name="process_attr_unique_column")),
        migrations.AddConstraint(model_name="processattributeappliedvalue", constraint=models.UniqueConstraint(fields=("applied_column", "requirement"), name="process_attr_unique_value")),
    ]
