from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0136_update_ecpmp_invoice_status_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="requirement",
            name="status",
            field=models.CharField(
                blank=True,
                default=None,
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="solutiondemand",
            name="status",
            field=models.CharField(
                blank=True,
                default=None,
                max_length=255,
                null=True,
            ),
        ),
    ]
