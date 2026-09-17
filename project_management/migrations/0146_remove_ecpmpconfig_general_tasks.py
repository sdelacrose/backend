from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0145_replace_industrial_project_types"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ecpmpconfig",
            name="general_tasks",
        ),
    ]
