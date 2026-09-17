from django.db import migrations, models
from django.db.models import Q


def merge_invoice_reference_counters_per_owner(apps, schema_editor):
    InvoiceReferenceCounter = apps.get_model(
        "project_management",
        "InvoiceReferenceCounter",
    )

    counters = list(InvoiceReferenceCounter.objects.all().order_by("id"))
    keepers = {}

    for counter in counters:
        key = (counter.company_profile_id, counter.freelancer_id)
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
        ("project_management", "0087_invoice_line_item_quantity_float"),
    ]

    operations = [
        migrations.RunPython(
            merge_invoice_reference_counters_per_owner,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="invoicereferencecounter",
            name="unique_invoice_reference_counter_company_period",
        ),
        migrations.RemoveConstraint(
            model_name="invoicereferencecounter",
            name="unique_invoice_reference_counter_freelancer_period",
        ),
        migrations.RemoveField(
            model_name="invoicereferencecounter",
            name="period",
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=False, freelancer__isnull=True),
                fields=("company_profile",),
                name="unique_invoice_reference_counter_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=True, freelancer__isnull=False),
                fields=("freelancer",),
                name="unique_invoice_reference_counter_freelancer",
            ),
        ),
    ]
