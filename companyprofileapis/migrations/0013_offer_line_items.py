from decimal import Decimal, ROUND_HALF_UP

import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


OFFER_STATUSES = {
    "draft",
    "converted",
    "issued",
    "declined",
    "converted_to_project",
}


def _money(value):
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def backfill_offer_line_items(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    OfferLineItem = apps.get_model("companyprofileapis", "OfferLineItem")

    offers = Opportunity.objects.filter(
        Q(status__in=OFFER_STATUSES)
        | Q(status="closed", reference__startswith="OFF-")
    ).order_by("id")

    for offer in offers.iterator():
        value = _money(offer.value)
        OfferLineItem.objects.get_or_create(
            opportunity_id=offer.id,
            line_number=1,
            defaults={
                "item_name": offer.title,
                "rate": value,
                "quantity": Decimal("1.0000"),
                "vat_rate": Decimal("0.00"),
                "total": value,
            },
        )
        if offer.value != value:
            Opportunity.objects.filter(pk=offer.pk).update(value=value)


def remove_backfilled_offer_line_items(apps, schema_editor):
    OfferLineItem = apps.get_model("companyprofileapis", "OfferLineItem")
    OfferLineItem.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("companyprofileapis", "0012_opportunity_offer_lifecycle_references"),
        ("project_management", "0104_parts_lists"),
    ]

    operations = [
        migrations.AlterField(
            model_name="opportunity",
            name="value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="historicalopportunity",
            name="value",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=12,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="OfferLineItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("line_number", models.PositiveIntegerField(default=1)),
                (
                    "item_name",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("rate", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "quantity",
                    models.DecimalField(
                        decimal_places=4,
                        default=1,
                        max_digits=12,
                    ),
                ),
                (
                    "vat_rate",
                    models.DecimalField(
                        decimal_places=2,
                        default=12,
                        max_digits=5,
                    ),
                ),
                ("total", models.DecimalField(decimal_places=2, max_digits=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "available_product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="offer_line_items",
                        to="project_management.availableproductpart",
                    ),
                ),
                (
                    "opportunity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="offer_line_items",
                        to="companyprofileapis.opportunity",
                    ),
                ),
            ],
            options={"ordering": ["line_number", "id"]},
        ),
        migrations.AddConstraint(
            model_name="offerlineitem",
            constraint=models.UniqueConstraint(
                fields=("opportunity", "line_number"),
                name="unique_offer_line_number",
            ),
        ),
        migrations.AddConstraint(
            model_name="offerlineitem",
            constraint=models.CheckConstraint(
                check=Q(rate__gte=0),
                name="offer_line_item_rate_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="offerlineitem",
            constraint=models.CheckConstraint(
                check=Q(quantity__gte=0),
                name="offer_line_item_quantity_nonnegative",
            ),
        ),
        migrations.AddConstraint(
            model_name="offerlineitem",
            constraint=models.CheckConstraint(
                check=Q(vat_rate__gte=0, vat_rate__lte=100),
                name="offer_line_item_vat_range",
            ),
        ),
        migrations.RunPython(
            backfill_offer_line_items,
            remove_backfilled_offer_line_items,
        ),
    ]
