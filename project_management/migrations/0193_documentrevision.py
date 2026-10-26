import django.db.models.deletion
import project_management.models.document_revision
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0192_documentcontrol_approver"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentRevision",
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
                ("major_revision", models.CharField(max_length=20)),
                ("minor_revision", models.PositiveIntegerField(default=0)),
                (
                    "file",
                    models.FileField(
                        max_length=500,
                        upload_to=project_management.models.document_revision.get_revision_upload_path,
                    ),
                ),
                ("file_name", models.CharField(blank=True, max_length=500, null=True)),
                ("file_size", models.BigIntegerField(blank=True, null=True)),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_document_revisions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="revisions",
                        to="project_management.document",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddConstraint(
            model_name="documentrevision",
            constraint=models.UniqueConstraint(
                fields=("document", "major_revision", "minor_revision"),
                name="document_revision_unique_version",
            ),
        ),
    ]
