from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0083_merge_20260413_0830"),
    ]

    operations = [
        migrations.AlterField(
            model_name="observation",
            name="rectification",
            field=models.CharField(blank=True, max_length=1000, null=True),
        ),
    ]
