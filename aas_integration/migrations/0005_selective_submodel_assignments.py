import uuid

import aas_integration.models
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_active_template_assignments(apps, schema_editor):
    ProductAASProfile = apps.get_model("aas_integration", "ProductAASProfile")
    AASSubmodelTemplate = apps.get_model("aas_integration", "AASSubmodelTemplate")
    ProductAASSubmodelAssignment = apps.get_model(
        "aas_integration",
        "ProductAASSubmodelAssignment",
    )

    assignments = []
    for template in AASSubmodelTemplate.objects.filter(status="active").iterator():
        profiles = ProductAASProfile.objects.filter(
            product__company_profile_id=template.company_id,
        )
        if template.profile_keys:
            profiles = profiles.filter(profile_key__in=template.profile_keys)
        assignments.extend(
            ProductAASSubmodelAssignment(
                profile_id=profile_id,
                template_id=template.id,
                template_identifier=template.template_identifier,
            )
            for profile_id in profiles.values_list("pk", flat=True).iterator()
        )
    ProductAASSubmodelAssignment.objects.bulk_create(
        assignments,
        ignore_conflicts=True,
    )


def remove_backfilled_assignments(apps, schema_editor):
    ProductAASSubmodelAssignment = apps.get_model(
        "aas_integration",
        "ProductAASSubmodelAssignment",
    )
    ProductAASSubmodelAssignment.objects.filter(assigned_by__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("aas_integration", "0004_submodel_template_registry"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="aassubmodeltemplate",
            name="concept_descriptions",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.CreateModel(
            name="AASSubmodelTemplateSupplementaryFile",
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
                ("path", models.CharField(max_length=1000)),
                ("content_type", models.CharField(max_length=255)),
                ("size", models.PositiveBigIntegerField()),
                ("sha256", models.CharField(max_length=64)),
                (
                    "file",
                    models.FileField(
                        max_length=1000,
                        upload_to=aas_integration.models.aas_submodel_template_file_path,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="supplementary_files",
                        to="aas_integration.aassubmodeltemplate",
                    ),
                ),
            ],
            options={"ordering": ["path"]},
        ),
        migrations.CreateModel(
            name="ProductAASSubmodelAssignment",
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
                ("template_identifier", models.CharField(max_length=1000)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="assigned_aas_product_submodels",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="submodel_assignments",
                        to="aas_integration.productaasprofile",
                    ),
                ),
                (
                    "template",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="product_assignments",
                        to="aas_integration.aassubmodeltemplate",
                    ),
                ),
            ],
            options={"ordering": ["template__name", "template_identifier"]},
        ),
        migrations.AddConstraint(
            model_name="aassubmodeltemplatesupplementaryfile",
            constraint=models.UniqueConstraint(
                fields=("template", "path"),
                name="aas_submodel_template_file_path_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="productaassubmodelassignment",
            constraint=models.UniqueConstraint(
                fields=("profile", "template_identifier"),
                name="aas_product_submodel_assignment_unique",
            ),
        ),
        migrations.AddIndex(
            model_name="productaassubmodelassignment",
            index=models.Index(
                fields=["template", "profile"],
                name="aas_product_sm_assignment_idx",
            ),
        ),
        migrations.RunPython(
            backfill_active_template_assignments,
            remove_backfilled_assignments,
        ),
    ]
