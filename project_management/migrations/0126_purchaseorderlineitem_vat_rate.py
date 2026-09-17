from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0125_purchaseorderlineitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorderlineitem",
            name="vat_rate",
            field=models.DecimalField(decimal_places=2, default=12, max_digits=5),
        ),
    ]
