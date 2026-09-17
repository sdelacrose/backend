from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0092_companypublicholiday_sync_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="timesheetactivity",
            name="is_invoiceable",
            field=models.BooleanField(default=True),
        ),
    ]
