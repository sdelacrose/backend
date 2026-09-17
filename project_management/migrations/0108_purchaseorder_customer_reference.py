from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0107_contactuser_mobile_number"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="customer_reference",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
