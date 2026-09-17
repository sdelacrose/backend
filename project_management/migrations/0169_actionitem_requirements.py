from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("project_management", "0168_project_content_foundation"),
    ]

    operations = [
        migrations.AddField(
            model_name="actionitem",
            name="requirements",
            field=models.ManyToManyField(
                blank=True,
                related_name="action_items",
                to="project_management.requirement",
            ),
        ),
    ]
