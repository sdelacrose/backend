import django.contrib.postgres.fields
import project_management.models.project
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0167_add_customer_demand_project_document_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="acceptance_status_options",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=project_management.models.project.default_acceptance_status_options,
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="folder",
            name="system_key",
            field=models.CharField(blank=True, db_index=True, max_length=64, null=True),
        ),
        migrations.AddConstraint(
            model_name="folder",
            constraint=models.UniqueConstraint(
                condition=models.Q(project__isnull=False, system_key__isnull=False),
                fields=("project", "system_key"),
                name="folder_unique_project_system_key",
            ),
        ),
    ]
