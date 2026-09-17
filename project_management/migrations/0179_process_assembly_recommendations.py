import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0178_workstation_bom_planning"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProcessAssemblyRecommendationRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("input_hash", models.CharField(db_index=True, max_length=64)),
                ("policy_version", models.CharField(default="1", max_length=32)),
                ("policy", models.JSONField(default=dict)),
                ("ranked_candidates", models.JSONField(default=list)),
                ("selection_outcome", models.CharField(choices=[("review_required", "Review required"), ("automatic", "Automatically selected"), ("manual", "Manually selected")], default="review_required", max_length=32)),
                ("decision_reason", models.CharField(blank=True, default="", max_length=64)),
                ("override_history", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_process_assembly_recommendation_runs", to=settings.AUTH_USER_MODEL)),
                ("created_workstation", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recommendation_runs", to="project_management.projectworkstation")),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="process_assembly_recommendation_runs", to="project_management.project")),
                ("selected_candidate", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="recommendation_selections", to="project_management.companysolutionspaceitem")),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="processassemblyrecommendationrun",
            constraint=models.UniqueConstraint(fields=("project", "input_hash", "policy_version"), name="unique_process_recommendation_input_policy"),
        ),
        migrations.AddIndex(
            model_name="processassemblyrecommendationrun",
            index=models.Index(fields=["project", "selection_outcome", "created_at"], name="proc_rec_proj_outcome_idx"),
        ),
    ]
