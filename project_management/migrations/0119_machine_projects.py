from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0118_equipments"),
    ]

    operations = [
        migrations.AddField(
            model_name="machine",
            name="projects",
            field=models.ManyToManyField(
                blank=True,
                related_name="machines",
                to="project_management.project",
            ),
        ),
    ]
