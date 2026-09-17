from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0106_actionitem_forecast_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="contactuser",
            name="mobile_number",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
    ]
