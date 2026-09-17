# Replace ProjectPhase.purchase_orders M2M with single purchase_order FK

import datetime

import django.db.models.deletion
from django.db import migrations, models


def _pick_purchase_order_for_phase(pos):
    """Keep one PO when a milestone was linked to multiple (outgoing root preferred)."""
    root_outgoing = [
        po
        for po in pos
        if po.po_type == "outgoing"
        and po.document_type == "purchase_order"
    ]
    candidates = root_outgoing or list(pos)
    return max(
        candidates,
        key=lambda po: (
            po.po_date or datetime.date.min,
            po.id,
        ),
    )


def copy_m2m_to_purchase_order_fk(apps, schema_editor):
    ProjectPhase = apps.get_model("project_management", "ProjectPhase")
    for phase in ProjectPhase.objects.prefetch_related("purchase_orders").all():
        pos = list(phase.purchase_orders.all())
        if not pos:
            continue
        chosen = pos[0] if len(pos) == 1 else _pick_purchase_order_for_phase(pos)
        phase.purchase_order_id = chosen.id
        phase.save(update_fields=["purchase_order_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0113_invoice_excluded_auto_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectphase",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_payment_project_phases_fk",
                to="project_management.purchaseorder",
            ),
        ),
        migrations.RunPython(copy_m2m_to_purchase_order_fk, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="projectphase",
            name="purchase_orders",
        ),
        migrations.AlterField(
            model_name="projectphase",
            name="purchase_order",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_payment_project_phases",
                to="project_management.purchaseorder",
            ),
        ),
    ]
