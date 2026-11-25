import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("human_resource_management", "0024_seed_manage_leave_reminders_permission"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="leavereminderdelivery",
            name="unique_leave_reminder_delivery",
        ),
        migrations.AddField(
            model_name="leavereminderdelivery",
            name="recipient",
            # No existing rows in this environment (confirmed before writing
            # this migration) -- a straight non-nullable FK, no default needed.
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="leave_reminder_deliveries_received",
                to=settings.AUTH_USER_MODEL,
                default=None,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="leavereminderdelivery",
            name="is_cc",
            field=models.BooleanField(
                default=False,
                help_text="True for a CC'd HR recipient's own delivery row; False for the subject staff's own.",
            ),
        ),
        migrations.AddConstraint(
            model_name="leavereminderdelivery",
            constraint=models.UniqueConstraint(
                fields=("rule", "staff", "deadline_date", "channel", "recipient"),
                name="unique_leave_reminder_delivery",
            ),
        ),
    ]
