import django.db.models.deletion
import project_content.models
import uuid

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_content", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="companyprojectdocumenttemplate",
            name="template_file",
            field=models.FileField(
                max_length=500,
                upload_to=project_content.models.project_template_upload_path,
            ),
        ),
        migrations.CreateModel(
            name="ProjectContentAuditEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("event_type", models.CharField(max_length=64)),
                ("object_type", models.CharField(max_length=64)),
                ("object_id", models.CharField(max_length=64)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="project_content_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="project_content_audit_events",
                        to="project_management.project",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="projectcontentauditevent",
            index=models.Index(
                fields=["project", "object_type", "object_id", "created_at"],
                name="project_content_audit_obj_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="projectcontentauditevent",
            index=models.Index(
                fields=["project", "event_type", "created_at"],
                name="project_content_audit_evt_idx",
            ),
        ),
    ]
