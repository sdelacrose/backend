from django.db import migrations, models
import django.contrib.postgres.fields


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0089_purchaseorder_payment_milestones"),
    ]

    operations = [
        migrations.AddField(
            model_name="ecpmpconfig",
            name="incoming_purchase_order_status",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=["Received", "Invoiced", "Closed"],
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="ecpmpconfig",
            name="outgoing_purchase_order_status",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.CharField(max_length=255),
                blank=True,
                default=["Open", "Sent", "Closed"],
                size=None,
            ),
        ),
        migrations.AlterField(
            model_name="purchaseorder",
            name="status",
            field=models.CharField(max_length=255),
        ),
    ]
