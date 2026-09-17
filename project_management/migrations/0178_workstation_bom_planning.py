import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0177_workstation_revision_operations"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="billofmaterialpart",
            name="engineering_detached",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="engineering_quantity",
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=20, null=True),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="engineering_unit",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="is_purchasable",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="source_occurrence_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="source_workstation",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bom_parts", to="project_management.projectworkstation"),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="source_workstation_snapshot",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bom_parts", to="project_management.workstationsnapshot"),
        ),
        migrations.CreateModel(
            name="BomGenerationRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_hash", models.CharField(db_index=True, max_length=64)),
                ("target_hash", models.CharField(db_index=True, max_length=64)),
                ("status", models.CharField(choices=[("preview", "Preview"), ("applied", "Applied"), ("stale", "Stale"), ("failed", "Failed")], db_index=True, default="preview", max_length=16)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("error_details", models.JSONField(blank=True, default=dict)),
                ("idempotency_key", models.CharField(blank=True, max_length=128, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("applied_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="applied_bom_generation_runs", to=settings.AUTH_USER_MODEL)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_bom_generation_runs", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bom_generation_runs", to="project_management.project")),
                ("workstation_snapshots", models.ManyToManyField(blank=True, related_name="bom_generation_runs", to="project_management.workstationsnapshot")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="BomPlanOperation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("operation", models.CharField(choices=[("add", "Add"), ("update", "Update"), ("unchanged", "Unchanged"), ("detach", "Detach"), ("conflict", "Conflict")], db_index=True, max_length=16)),
                ("source_occurrence_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("before_values", models.JSONField(blank=True, default=dict)),
                ("after_values", models.JSONField(blank=True, default=dict)),
                ("resolution_state", models.CharField(blank=True, default="", max_length=32)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="operations", to="project_management.bomgenerationrun")),
                ("source_snapshot", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="bom_plan_operations", to="project_management.workstationsnapshot")),
                ("source_workstation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="bom_plan_operations", to="project_management.projectworkstation")),
                ("target_bom_part", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bom_plan_operations", to="project_management.billofmaterialpart")),
            ],
            options={"ordering": ["sort_order", "id"]},
        ),
        migrations.AddConstraint(
            model_name="bomgenerationrun",
            constraint=models.UniqueConstraint(condition=Q(idempotency_key__isnull=False), fields=("project", "idempotency_key"), name="unique_project_bom_run_idempotency_key"),
        ),
        migrations.AddIndex(
            model_name="billofmaterialpart",
            index=models.Index(fields=["project", "source_workstation", "source_occurrence_id"], name="bom_part_workstation_occ_idx"),
        ),
        migrations.AddIndex(
            model_name="bomplanoperation",
            index=models.Index(fields=["run", "operation", "sort_order"], name="bom_plan_run_op_order_idx"),
        ),
    ]
