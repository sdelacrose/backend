import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0123_transports"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseorder",
            name="emitting_company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="purchase_orders_as_emitting_company",
                to="project_management.contactcompany",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="emitting_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="purchase_orders_as_emitting_user",
                to="project_management.contactuser",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="receiving_company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="purchase_orders_as_receiving_company",
                to="project_management.contactcompany",
            ),
        ),
        migrations.AddField(
            model_name="purchaseorder",
            name="receiving_user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="purchase_orders_as_receiving_user",
                to="project_management.contactuser",
            ),
        ),
    ]
