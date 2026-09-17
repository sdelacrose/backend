# Generated manually — PO incoming/outgoing status lists are fixed in code, not ECPMP config.

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0099_projectphase_purchase_orders_m2m"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="ecpmpconfig",
            name="incoming_purchase_order_status",
        ),
        migrations.RemoveField(
            model_name="ecpmpconfig",
            name="outgoing_purchase_order_status",
        ),
    ]
