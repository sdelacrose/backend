# Generated manually to avoid unrelated pending model changes in other apps.

import django.db.models.deletion
import project_management.models.project
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0103_extend_requirement_skill_options"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AvailableProductPart",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("name", models.CharField(max_length=255)),
                ("part_number", models.CharField(blank=True, max_length=255, null=True)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_profile", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="available_product_parts", to="models.companyprofile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_available_product_parts", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_available_product_parts", to=settings.AUTH_USER_MODEL)),
                ("object_assessments", models.ManyToManyField(blank=True, related_name="available_product_parts", to="project_management.objectassessment")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_parts", to="project_management.availableproductpart")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.CreateModel(
            name="PartsListConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("available_product_columns", models.JSONField(blank=True, default=project_management.models.project.default_parts_columns)),
                ("bom_columns", models.JSONField(blank=True, default=project_management.models.project.default_parts_columns)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("company_profile", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="parts_list_configs", to="models.companyprofile")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_parts_list_configs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_parts_list_configs", to=settings.AUTH_USER_MODEL)),
                ("project", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="parts_list_config", to="project_management.project")),
            ],
        ),
        migrations.CreateModel(
            name="BillOfMaterialPart",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("part_number", models.CharField(blank=True, max_length=255, null=True)),
                ("available_product", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="bom_parts", to="project_management.availableproductpart")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_bom_parts", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_bom_parts", to=settings.AUTH_USER_MODEL)),
                ("object_assessments", models.ManyToManyField(blank=True, related_name="bom_parts", to="project_management.objectassessment")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_parts", to="project_management.billofmaterialpart")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="bom_parts", to="project_management.project")),
                ("requirements", models.ManyToManyField(blank=True, related_name="bom_parts", to="project_management.requirement")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.AddField(
            model_name="matrixcomment",
            name="available_product_part",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.availableproductpart"),
        ),
        migrations.AddField(
            model_name="matrixcomment",
            name="bom_part",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.billofmaterialpart"),
        ),
        migrations.AlterField(
            model_name="matrixcomment",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("solution_demand", "Solution Demand"),
                    ("requirement", "Requirement"),
                    ("available_product_part", "Available Product Part"),
                    ("bom_part", "Bill of Material Part"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="availableproductpart",
            index=models.Index(fields=["company_profile", "parent", "sort_order"], name="avail_part_company_parent_idx"),
        ),
        migrations.AddIndex(
            model_name="availableproductpart",
            index=models.Index(fields=["company_profile", "level"], name="avail_part_company_level_idx"),
        ),
        migrations.AddIndex(
            model_name="billofmaterialpart",
            index=models.Index(fields=["project", "parent", "sort_order"], name="bom_part_project_parent_idx"),
        ),
        migrations.AddIndex(
            model_name="billofmaterialpart",
            index=models.Index(fields=["project", "level"], name="bom_part_project_level_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "available_product_part", "created_at"], name="comment_available_part_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "bom_part", "created_at"], name="comment_bom_part_idx"),
        ),
        migrations.AddConstraint(
            model_name="partslistconfig",
            constraint=models.UniqueConstraint(
                condition=models.Q(project__isnull=True),
                fields=("company_profile",),
                name="unique_company_parts_config",
            ),
        ),
        migrations.AddConstraint(
            model_name="partslistconfig",
            constraint=models.UniqueConstraint(
                condition=models.Q(project__isnull=False),
                fields=("project",),
                name="unique_project_parts_config",
            ),
        ),
    ]
