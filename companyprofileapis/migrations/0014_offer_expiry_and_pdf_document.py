import django.db.models.deletion
from django.db import migrations, models


OFFER_STATUSES = {
    "draft",
    "converted",
    "issued",
    "declined",
    "converted_to_project",
}


def backfill_offer_created_dates(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    HistoricalOpportunity = apps.get_model(
        "companyprofileapis",
        "HistoricalOpportunity",
    )

    offers = Opportunity.objects.filter(
        models.Q(status__in=OFFER_STATUSES)
        | models.Q(status="closed", reference__startswith="OFF-")
    ).only("id", "created_at", "offer_created_date")

    for offer in offers.iterator():
        if offer.offer_created_date:
            continue
        earliest_offer_history = (
            HistoricalOpportunity.objects.filter(id=offer.id)
            .filter(
                models.Q(status__in=OFFER_STATUSES)
                | models.Q(status="closed", reference__startswith="OFF-")
            )
            .order_by("history_date")
            .values_list("history_date", flat=True)
            .first()
        )
        base_date = (
            earliest_offer_history.date()
            if earliest_offer_history
            else offer.created_at.date()
        )
        Opportunity.objects.filter(pk=offer.pk).update(
            offer_created_date=base_date
        )
        HistoricalOpportunity.objects.filter(id=offer.id).filter(
            models.Q(status__in=OFFER_STATUSES)
            | models.Q(status="closed", reference__startswith="OFF-")
        ).update(offer_created_date=base_date)


def clear_offer_created_dates(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    HistoricalOpportunity = apps.get_model(
        "companyprofileapis",
        "HistoricalOpportunity",
    )
    Opportunity.objects.update(offer_created_date=None)
    HistoricalOpportunity.objects.update(offer_created_date=None)


class Migration(migrations.Migration):
    dependencies = [
        ("companyprofileapis", "0013_offer_line_items"),
        ("project_management", "0189_merge_contact_multiselect_and_workstation_approval"),
    ]

    operations = [
        migrations.AddField(
            model_name="historicalopportunity",
            name="offer_created_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalopportunity",
            name="offer_expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="historicalopportunity",
            name="offer_expiry_period",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="historicalopportunity",
            name="offer_pdf_document",
            field=models.ForeignKey(
                blank=True,
                db_constraint=False,
                null=True,
                on_delete=django.db.models.deletion.DO_NOTHING,
                related_name="+",
                to="project_management.document",
            ),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="offer_created_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="offer_expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="offer_expiry_period",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="offer_pdf_document",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="generated_offer_for",
                to="project_management.document",
            ),
        ),
        migrations.RunPython(
            backfill_offer_created_dates,
            clear_offer_created_dates,
        ),
    ]
