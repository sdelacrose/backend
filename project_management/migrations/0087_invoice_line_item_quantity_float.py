from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0086_invoice_replacement_and_shared_reference_counter"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lineitem",
            name="quantity",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="invoicelineitem",
            name="quantity",
            field=models.FloatField(blank=True, null=True),
        ),
    ]
