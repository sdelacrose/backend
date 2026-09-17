from django.contrib.postgres.fields import ArrayField
from django.db import migrations, models

import project_management.models.project


DEFAULT_STATUS_OPTIONS = [
    "open:#99dce8",
    "in progress:#fff3ad",
    "closed:#bdefe0",
]


def populate_module_status_options(apps, schema_editor):
    ECPMPConfig = apps.get_model("project_management", "ECPMPConfig")
    ECPMPConfig.objects.all().update(
        demand_status_options=DEFAULT_STATUS_OPTIONS,
        requirement_status_options=DEFAULT_STATUS_OPTIONS,
        bom_status_options=DEFAULT_STATUS_OPTIONS,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0137_solution_demand_requirement_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="bom_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="ecpmpconfig",
            name="demand_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="ecpmpconfig",
            name="requirement_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=list,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="billofmaterialpart",
            name="status",
            field=models.CharField(
                blank=True,
                default=None,
                max_length=255,
                null=True,
            ),
        ),
        migrations.RunPython(
            populate_module_status_options,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="ecpmpconfig",
            name="bom_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=project_management.models.project.default_bom_status_options,
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="ecpmpconfig",
            name="demand_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=project_management.models.project.default_demand_status_options,
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="ecpmpconfig",
            name="requirement_status_options",
            field=ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=project_management.models.project.default_requirement_status_options,
                size=None,
            ),
        ),
    ]
