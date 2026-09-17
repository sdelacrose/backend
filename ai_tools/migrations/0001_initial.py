# Generated manually for the AI business data tools foundation.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("models", "0069_globalpublicholiday"),
    ]

    operations = [
        migrations.CreateModel(
            name="AIToolAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("tool_name", models.CharField(max_length=255)),
                ("request_payload", models.JSONField(blank=True, default=dict)),
                ("result_count", models.PositiveIntegerField(default=0)),
                ("source_object_refs", models.JSONField(blank=True, default=list)),
                (
                    "status",
                    models.CharField(
                        choices=[("success", "Success"), ("error", "Error")],
                        default="success",
                        max_length=32,
                    ),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                ("latency_ms", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "company_profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_tool_audit_logs",
                        to="models.companyprofile",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ai_tool_audit_logs",
                        to="models.user",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(fields=["user", "created_at"], name="ai_tools_ai_user_id_55e8cc_idx"),
                    models.Index(fields=["company_profile", "created_at"], name="ai_tools_ai_company_f0d745_idx"),
                    models.Index(fields=["tool_name", "created_at"], name="ai_tools_ai_tool_na_3a87c5_idx"),
                    models.Index(fields=["status", "created_at"], name="ai_tools_ai_status_f018c9_idx"),
                ],
            },
        ),
    ]
