import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0190_workstation_compliance"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DocumentControl",
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
                ("reference", models.CharField(blank=True, max_length=255, null=True)),
                (
                    "document_type",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Plan", "Plan"),
                            ("Procedure", "Procedure"),
                            ("Specification", "Specification"),
                            ("Technical Note", "Technical Note"),
                            ("Report", "Report"),
                            ("Minutes", "Minutes"),
                            ("Contract", "Contract"),
                            ("Quality Document", "Quality Document"),
                            ("Supplier Document", "Supplier Document"),
                            ("Calculation", "Calculation"),
                            ("Datasheet", "Datasheet"),
                            ("Manual", "Manual"),
                            ("Other", "Other"),
                        ],
                        max_length=64,
                        null=True,
                    ),
                ),
                (
                    "discipline",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("Project Management", "Project Management"),
                            ("General", "General"),
                            ("Process", "Process"),
                            ("Mechanical", "Mechanical"),
                            ("Piping", "Piping"),
                            ("Electrical", "Electrical"),
                            ("Instrumentation", "Instrumentation"),
                            ("Civil", "Civil"),
                            ("Structural", "Structural"),
                            ("HVAC", "HVAC"),
                            ("Quality", "Quality"),
                            ("HSE", "HSE"),
                            ("Procurement", "Procurement"),
                            ("Construction", "Construction"),
                            ("Commissioning", "Commissioning"),
                            ("Contract", "Contract"),
                            ("Finance", "Finance"),
                            ("Other", "Other"),
                        ],
                        max_length=64,
                        null=True,
                    ),
                ),
                ("revision", models.CharField(default="A", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("ACTIVE", "Active"),
                            ("ARCHIVED", "Archived"),
                        ],
                        default="DRAFT",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "document",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="control",
                        to="project_management.document",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="owned_document_controls",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
