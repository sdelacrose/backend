from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0093_timesheetactivity_is_invoiceable"),
    ]

    operations = [
        migrations.AddField(
            model_name="timecard",
            name="is_invoiceable_from_timesheet",
            field=models.BooleanField(blank=True, null=True),
        ),
    ]
