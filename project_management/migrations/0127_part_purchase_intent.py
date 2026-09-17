# Generated manually to avoid unrelated pending model changes in other apps.

import django.db.models.deletion
import project_management.models.project
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0126_purchaseorderlineitem_vat_rate"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PartPurchaseIntent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(default=project_management.models.project.part_purchase_reference, max_length=40, unique=True)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("code_sent", "Code sent"),
                            ("verified", "Verified"),
                            ("confirmed", "Confirmed"),
                            ("cancelled", "Cancelled"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=30,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("ai_chat", "AI chat"),
                            ("available_product", "Available product"),
                            ("bom_part", "Bill of material part"),
                            ("manual", "Manual"),
                        ],
                        default="manual",
                        max_length=40,
                    ),
                ),
                ("source_product_id", models.CharField(blank=True, max_length=255, null=True)),
                ("supplier", models.CharField(blank=True, max_length=255, null=True)),
                ("currency", models.CharField(default="EUR", max_length=10)),
                (
                    "price_status",
                    models.CharField(
                        choices=[
                            ("price_on_request", "Price on request"),
                            ("price_available", "Price available"),
                            ("not_available", "Not available"),
                        ],
                        default="price_on_request",
                        max_length=30,
                    ),
                ),
                ("unit_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("total_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("items", models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_items)),
                ("product_snapshot", models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_snapshot)),
                ("delivery_address", models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_address)),
                ("invoice_address", models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_address)),
                ("payment_snapshot", models.JSONField(blank=True, default=project_management.models.project.default_part_purchase_snapshot)),
                ("confirmation_code_hash", models.CharField(blank=True, max_length=255, null=True)),
                ("code_sent_at", models.DateTimeField(blank=True, null=True)),
                ("code_expires_at", models.DateTimeField(blank=True, null=True)),
                ("code_verified_at", models.DateTimeField(blank=True, null=True)),
                ("code_attempts", models.PositiveSmallIntegerField(default=0)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "buyer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="part_purchase_intents",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "buyer_company",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="part_purchase_intents",
                        to="models.companyprofile",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="partpurchaseintent",
            index=models.Index(fields=["buyer", "status", "created_at"], name="part_intent_buyer_status_idx"),
        ),
        migrations.AddIndex(
            model_name="partpurchaseintent",
            index=models.Index(fields=["buyer_company", "created_at"], name="part_intent_company_idx"),
        ),
        migrations.AddIndex(
            model_name="partpurchaseintent",
            index=models.Index(fields=["reference"], name="part_intent_reference_idx"),
        ),
    ]
