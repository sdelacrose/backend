from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0109_contactcompany_company_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="timesheetactivity",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="timesheet_activities",
                to="project_management.purchaseorder",
            ),
        ),
        migrations.AlterField(
            model_name="timesheetactivity",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("company_action_item", "Company Action Item"),
                    ("company_calendar_event", "Company Calendar Event"),
                    ("project", "Project"),
                    ("project_purchase_order", "Project Purchase Order"),
                    ("project_phase", "Project Phase"),
                    ("project_action_item", "Project Action Item"),
                ],
                max_length=40,
            ),
        ),
    ]
