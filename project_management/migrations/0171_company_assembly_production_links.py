from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("project_management", "0170_remove_unused_project_configuration_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="CompanyAssemblyProductionLink",
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
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_company_assembly_production_links",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "process_assembly",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="product_assembly_links",
                        to="project_management.companysolutionspaceitem",
                    ),
                ),
                (
                    "product_assembly",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="process_assembly_links",
                        to="project_management.companysolutionspaceitem",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="companyassemblyproductionlink",
            constraint=models.UniqueConstraint(
                fields=("product_assembly", "process_assembly"),
                name="company_assembly_unique_production_link",
            ),
        ),
        migrations.AddConstraint(
            model_name="companyassemblyproductionlink",
            constraint=models.CheckConstraint(
                condition=~models.Q(
                    ("product_assembly", models.F("process_assembly"))
                ),
                name="company_assembly_distinct_production_link",
            ),
        ),
        migrations.AddIndex(
            model_name="companyassemblyproductionlink",
            index=models.Index(
                fields=["product_assembly", "process_assembly"],
                name="company_assembly_prod_link_idx",
            ),
        ),
        migrations.AlterField(
            model_name="projectsolutionspaceitem",
            name="source_company_item",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="project_copies",
                to="project_management.companysolutionspaceitem",
            ),
        ),
    ]
