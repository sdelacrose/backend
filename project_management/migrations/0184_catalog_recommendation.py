from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0183_bom_internal_run_namespace"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CatalogRecommendationRun",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("running", "Running"),
                            ("review_ready", "Ready for review"),
                            ("applied", "Confirmed"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                            ("stale", "Inputs changed"),
                        ],
                        default="queued",
                        max_length=32,
                    ),
                ),
                ("project_language", models.CharField(default="en", max_length=16)),
                ("input_hash", models.CharField(max_length=64)),
                ("input_snapshot", models.JSONField(default=dict)),
                ("result", models.JSONField(default=dict)),
                ("selected_ids", models.JSONField(default=list)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("error_message", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(null=True)),
                ("completed_at", models.DateTimeField(null=True)),
                ("applied_at", models.DateTimeField(null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="project_management.project",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="catalog_recommendation_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "confirmed_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="confirmed_catalog_recommendation_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-id"],
                "indexes": [
                    models.Index(
                        fields=["project", "status"], name="catalog_run_project_status"
                    )
                ],
            },
        ),
    ]
