from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


def merge_invoice_reference_counters(apps, schema_editor):
    InvoiceReferenceCounter = apps.get_model(
        "project_management",
        "InvoiceReferenceCounter",
    )

    counters = list(
        InvoiceReferenceCounter.objects.all().order_by(
            "company_profile_id",
            "freelancer_id",
            "period",
            "id",
        )
    )

    keepers = {}
    for counter in counters:
        key = (
            counter.company_profile_id,
            counter.freelancer_id,
            counter.period,
        )

        keeper = keepers.get(key)
        if keeper is None:
            keepers[key] = counter
            continue

        if counter.last_sequence > keeper.last_sequence:
            keeper.last_sequence = counter.last_sequence
            keeper.save(update_fields=["last_sequence", "updated_at"])

        counter.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0085_ecpmpconfig_actionitem_labels"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="carried_forward_amount",
            field=models.FloatField(blank=True, default=0, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="replaced_invoice",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="replacement_invoice",
                to="project_management.invoice",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=Q(document_type="invoice") | Q(replaced_invoice__isnull=True),
                name="invoice_replaced_invoice_only_for_invoices",
            ),
        ),
        migrations.RunPython(
            merge_invoice_reference_counters,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="invoicereferencecounter",
            name="unique_invoice_reference_counter_company_period_prefix",
        ),
        migrations.RemoveConstraint(
            model_name="invoicereferencecounter",
            name="unique_invoice_reference_counter_freelancer_period_prefix",
        ),
        migrations.RemoveField(
            model_name="invoicereferencecounter",
            name="prefix",
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=False, freelancer__isnull=True),
                fields=("company_profile", "period"),
                name="unique_invoice_reference_counter_company_period",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=True, freelancer__isnull=False),
                fields=("freelancer", "period"),
                name="unique_invoice_reference_counter_freelancer_period",
            ),
        ),
    ]
