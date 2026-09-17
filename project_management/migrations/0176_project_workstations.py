import decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("project_management", "0169_actionitem_requirements"),
        ("project_management", "0175_billofmaterialpart_source_project_assembly"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProjectWorkstation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(db_index=True, max_length=255)),
                ("status", models.CharField(choices=[("active", "Active"), ("archived", "Archived")], db_index=True, default="active", max_length=16)),
                ("template_hash", models.CharField(max_length=64)),
                ("active_revision", models.PositiveIntegerField(default=1)),
                ("idempotency_key", models.CharField(blank=True, max_length=128, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_project_workstations", to=settings.AUTH_USER_MODEL)),
                ("modified_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="modified_project_workstations", to=settings.AUTH_USER_MODEL)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="project_workstations", to="project_management.project")),
                ("source_process_assembly", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="project_workstations", to="project_management.companysolutionspaceitem")),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.CreateModel(
            name="WorkstationRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("number", models.PositiveIntegerField()),
                ("reason", models.TextField(blank=True, default="")),
                ("source_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_workstation_revisions", to=settings.AUTH_USER_MODEL)),
                ("workstation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="project_management.projectworkstation")),
            ],
            options={"ordering": ["-number", "-id"]},
        ),
        migrations.CreateModel(
            name="WorkstationSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("content", models.JSONField(default=dict)),
                ("content_hash", models.CharField(db_index=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("revision", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="snapshot", to="project_management.workstationrevision")),
            ],
        ),
        migrations.CreateModel(
            name="ProjectWorkstationNode",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=255)),
                ("assembly_type", models.CharField(blank=True, max_length=32, null=True)),
                ("role", models.CharField(choices=[("baseline", "Baseline"), ("added", "Added"), ("replacement", "Replacement"), ("excluded", "Excluded")], default="baseline", max_length=16)),
                ("is_active", models.BooleanField(default=True)),
                ("quantity", models.DecimalField(decimal_places=6, default=decimal.Decimal("1"), max_digits=20)),
                ("unit", models.CharField(blank=True, default="", max_length=64)),
                ("level", models.PositiveSmallIntegerField(default=1)),
                ("sort_order", models.PositiveIntegerField(db_index=True, default=0)),
                ("required_skills", models.JSONField(blank=True, default=list)),
                ("provided_skills", models.JSONField(blank=True, default=list)),
                ("custom_fields", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_workstation_nodes", to=settings.AUTH_USER_MODEL)),
                ("parent", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="children", to="project_management.projectworkstationnode")),
                ("revision", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="nodes", to="project_management.workstationrevision")),
                ("source_company_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="workstation_occurrences", to="project_management.companysolutionspaceitem")),
                ("source_project_item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="workstation_occurrences", to="project_management.projectsolutionspaceitem")),
                ("supersedes", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="replacements", to="project_management.projectworkstationnode")),
                ("workstation", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="nodes", to="project_management.projectworkstation")),
            ],
            options={"ordering": ["level", "sort_order", "id"]},
        ),
        migrations.AddIndex(
            model_name="projectworkstation",
            index=models.Index(fields=["project", "status", "name"], name="project_ws_status_name_idx"),
        ),
        migrations.AddConstraint(
            model_name="projectworkstation",
            constraint=models.UniqueConstraint(condition=models.Q(("idempotency_key__isnull", False)), fields=("project", "idempotency_key"), name="unique_project_workstation_idempotency_key"),
        ),
        migrations.AddConstraint(
            model_name="workstationrevision",
            constraint=models.UniqueConstraint(fields=("workstation", "number"), name="unique_workstation_revision_number"),
        ),
        migrations.AddIndex(
            model_name="projectworkstationnode",
            index=models.Index(fields=["workstation", "revision", "parent", "sort_order"], name="workstation_node_tree_idx"),
        ),
        migrations.AddConstraint(
            model_name="projectworkstationnode",
            constraint=models.CheckConstraint(condition=models.Q(("quantity__gt", 0)), name="workstation_node_positive_quantity"),
        ),
    ]
