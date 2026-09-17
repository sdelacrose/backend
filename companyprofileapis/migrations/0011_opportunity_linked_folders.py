from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("companyprofileapis", "0010_opportunity_projects"),
        ("project_management", "0121_linked_folders"),
    ]

    operations = [
        migrations.AddField(
            model_name="opportunity",
            name="linked_folders",
            field=models.ManyToManyField(
                blank=True,
                related_name="opportunity_linked_folders",
                to="project_management.folder",
            ),
        ),
    ]
