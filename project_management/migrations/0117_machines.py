# Generated manually to avoid unrelated pending model changes in other apps.

import django.db.models.deletion
import project_management.models.project
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0116_contactuser_availability_dates"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MachineConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("machine_columns", models.JSONField(blank=True, default=project_management.models.project.default_machine_columns)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_profile", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="machine_config", to="models.companyprofile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_machine_configs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_machine_configs", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="Machine",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("name", models.CharField(max_length=255)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="machines", to="models.companyprofile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_machines", to=settings.AUTH_USER_MODEL)),
                ("documents", models.ManyToManyField(blank=True, related_name="machines", to="project_management.document")),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_machines", to=settings.AUTH_USER_MODEL)),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_machines", to="project_management.machine")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="matrixcomment",
            name="machine",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.machine"),
        ),
        migrations.AlterField(
            model_name="matrixcomment",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("solution_demand", "Solution Demand"),
                    ("requirement", "Requirement"),
                    ("available_product_part", "Available Product Part"),
                    ("machine", "Machine"),
                    ("bom_part", "Bill of Material Part"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="machine",
            index=models.Index(fields=["company_profile", "parent", "sort_order"], name="machine_company_parent_idx"),
        ),
        migrations.AddIndex(
            model_name="machine",
            index=models.Index(fields=["company_profile", "level"], name="machine_company_level_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "machine", "created_at"], name="comment_machine_idx"),
        ),
    ]
