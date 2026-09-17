import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("aas_integration", "0002_product_profile_and_filtered_festo"),
        ("project_management", "0166_merge_configuration_visibility_and_aas"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="aasresourceidentity",
            name="aas_identity_exact_source",
        ),
        migrations.AddField(
            model_name="aasresourceidentity",
            name="company_assembly",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="aas_identity",
                to="project_management.companysolutionspaceitem",
            ),
        ),
        migrations.AddField(
            model_name="aasresourceidentity",
            name="project_assembly",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="aas_identity",
                to="project_management.projectsolutionspaceitem",
            ),
        ),
        migrations.AlterField(
            model_name="aasresourceidentity",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("project", "Project"),
                    ("company_product", "Company product"),
                    ("company_assembly", "Company assembly"),
                    ("project_assembly", "Project assembly"),
                    ("festo_handover", "Festo handover"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="aasresourceidentity",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        source_type="project",
                        project__isnull=False,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="company_product",
                        project__isnull=True,
                        company_product__isnull=False,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="company_assembly",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=False,
                        project_assembly__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="project_assembly",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=False,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="festo_handover",
                        project__isnull=True,
                        company_product__isnull=True,
                        company_assembly__isnull=True,
                        project_assembly__isnull=True,
                        festo_handover__isnull=False,
                    )
                ),
                name="aas_identity_exact_source",
            ),
        ),
    ]
