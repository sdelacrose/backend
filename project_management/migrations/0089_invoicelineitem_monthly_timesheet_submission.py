from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0088_weekly_timesheet_schema"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoicelineitem",
            name="monthly_timesheet_submission",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="invoice_line_items",
                to="project_management.monthlytimesheetsubmission",
            ),
        ),
    ]
