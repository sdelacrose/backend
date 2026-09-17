# Generated manually to avoid unrelated pending model changes in other apps.

import django.db.models.deletion
import project_management.models.project
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0101_actionitem_assignees"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectMatrixConfig",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("demand_columns", models.JSONField(blank=True, default=list)),
                ("requirement_columns", models.JSONField(blank=True, default=list)),
                ("requirement_skill_options", models.JSONField(blank=True, default=project_management.models.project.default_requirement_skill_options)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_project_matrix_configs", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_project_matrix_configs", to=settings.AUTH_USER_MODEL)),
                ("project", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="matrix_config", to="project_management.project")),
            ],
        ),
        migrations.CreateModel(
            name="SolutionDemand",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("subject", models.CharField(max_length=255)),
                ("demand_number", models.CharField(blank=True, max_length=255, null=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_solution_demands", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_solution_demands", to=settings.AUTH_USER_MODEL)),
                ("object_assessments", models.ManyToManyField(blank=True, related_name="solution_demands", to="project_management.objectassessment")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_demands", to="project_management.solutiondemand")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="solution_demands", to="project_management.project")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.CreateModel(
            name="Requirement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=255)),
                ("object_number", models.CharField(blank=True, max_length=255, null=True)),
                ("skill", models.CharField(blank=True, max_length=255, null=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_requirements", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_requirements", to=settings.AUTH_USER_MODEL)),
                ("object_assessments", models.ManyToManyField(blank=True, related_name="requirements", to="project_management.objectassessment")),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="sub_requirements", to="project_management.requirement")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="requirements", to="project_management.project")),
                ("solution_demands", models.ManyToManyField(blank=True, related_name="requirements", to="project_management.solutiondemand")),
            ],
            options={
                "ordering": ["sort_order", "id"],
            },
        ),
        migrations.CreateModel(
            name="MatrixComment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("target_type", models.CharField(choices=[("solution_demand", "Solution Demand"), ("requirement", "Requirement")], max_length=32)),
                ("content", models.TextField(blank=True, default="")),
                ("attachments", models.JSONField(blank=True, default=list)),
                ("linked_objects", models.JSONField(blank=True, default=list)),
                ("mentions", models.JSONField(blank=True, default=list)),
                ("is_deleted", models.BooleanField(default=False)),
                ("is_edited", models.BooleanField(default=False)),
                ("edited_at", models.DateTimeField(blank=True, null=True)),
                ("deleted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("deleted_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="deleted_matrix_comments", to=settings.AUTH_USER_MODEL)),
                ("replied_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replies", to="project_management.matrixcomment")),
                ("requirement", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.requirement")),
                ("sender", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="matrix_comments", to=settings.AUTH_USER_MODEL)),
                ("solution_demand", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="project_management.solutiondemand")),
            ],
        ),
        migrations.CreateModel(
            name="MatrixCommentReaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("comment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reactions", to="project_management.matrixcomment")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="matrix_comment_reactions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "unique_together": {("comment", "user", "key")},
            },
        ),
        migrations.AddIndex(
            model_name="solutiondemand",
            index=models.Index(fields=["project", "parent", "sort_order"], name="project_man_project_840fc1_idx"),
        ),
        migrations.AddIndex(
            model_name="solutiondemand",
            index=models.Index(fields=["project", "level"], name="project_man_project_4a7564_idx"),
        ),
        migrations.AddIndex(
            model_name="requirement",
            index=models.Index(fields=["project", "parent", "sort_order"], name="project_man_project_2102ee_idx"),
        ),
        migrations.AddIndex(
            model_name="requirement",
            index=models.Index(fields=["project", "level"], name="project_man_project_940ae4_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "solution_demand", "created_at"], name="project_man_target__59e5be_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["target_type", "requirement", "created_at"], name="project_man_target__7eea54_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["replied_to", "created_at"], name="project_man_replied_613d63_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcomment",
            index=models.Index(fields=["sender", "created_at"], name="project_man_sender__09258a_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcommentreaction",
            index=models.Index(fields=["comment", "key"], name="project_man_comment_302e6b_idx"),
        ),
        migrations.AddIndex(
            model_name="matrixcommentreaction",
            index=models.Index(fields=["user", "created_at"], name="project_man_user_id_4ea7ac_idx"),
        ),
    ]
