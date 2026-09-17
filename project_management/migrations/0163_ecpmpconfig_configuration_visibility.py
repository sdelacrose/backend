from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        (
            "project_management",
            "0162_pluralize_time_and_material_options",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="configuration_visibility",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
