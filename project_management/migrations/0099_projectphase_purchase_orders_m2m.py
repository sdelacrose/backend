# Replace ProjectPhase.purchase_order FK with M2M purchase_orders

from django.db import migrations, models


def copy_purchase_order_fk_to_m2m(apps, schema_editor):
    ProjectPhase = apps.get_model("project_management", "ProjectPhase")
    PurchaseOrder = apps.get_model("project_management", "PurchaseOrder")
    for phase in ProjectPhase.objects.all():
        fk_id = getattr(phase, "purchase_order_id", None)
        if not fk_id:
            continue
        po = PurchaseOrder.objects.filter(pk=fk_id).first()
        if po:
            phase.purchase_orders.add(po)


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0098_invoice_reference_counter_continuous_idempotent"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectphase",
            name="purchase_orders",
            field=models.ManyToManyField(
                blank=True,
                related_name="linked_payment_project_phases_m2m",
                to="project_management.purchaseorder",
            ),
        ),
        migrations.RunPython(copy_purchase_order_fk_to_m2m, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="projectphase",
            name="purchase_order",
        ),
        migrations.AlterField(
            model_name="projectphase",
            name="purchase_orders",
            field=models.ManyToManyField(
                blank=True,
                related_name="linked_payment_project_phases",
                to="project_management.purchaseorder",
            ),
        ),
    ]
