from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0091_companypublicholiday"),
    ]

    operations = [
        migrations.AddField(
            model_name="companypublicholiday",
            name="synced_global_public_holiday_id",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="companypublicholiday",
            name="source",
            field=models.CharField(
                choices=[
                    ("manual", "Manual"),
                    ("imported", "Imported"),
                    ("global_sync", "Global Sync"),
                ],
                default="manual",
                max_length=20,
            ),
        ),
    ]
