import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0124_purchaseorder_contact_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PurchaseOrderLineItem",
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
                ("item_name", models.CharField(blank=True, default="", max_length=255)),
                ("rate", models.DecimalField(decimal_places=2, max_digits=12)),
                ("quantity", models.DecimalField(decimal_places=4, default=1, max_digits=12)),
                ("total", models.DecimalField(decimal_places=2, max_digits=12)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "available_product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="purchase_order_line_items",
                        to="project_management.availableproductpart",
                    ),
                ),
                (
                    "purchase_order",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="line_items",
                        to="project_management.purchaseorder",
                    ),
                ),
            ],
            options={
                "ordering": ["line_number", "id"],
            },
        ),
    ]
