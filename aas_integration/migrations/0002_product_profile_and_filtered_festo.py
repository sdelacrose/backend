from django.db import migrations, models
import django.db.models.deletion


def create_product_profiles(apps, schema_editor):
    Product = apps.get_model("project_management", "AvailableProductPart")
    Profile = apps.get_model("aas_integration", "ProductAASProfile")
    profiles = []
    for product in Product.objects.all().iterator(chunk_size=500):
        fields = product.custom_fields or {}
        supplier = str(fields.get("supplier") or "").strip()
        manufacturer = str(fields.get("manufacturer") or supplier).strip()
        supplier_aas_id = str(fields.get("supplier_aas_id") or fields.get("aas_id") or "").strip()
        profiles.append(
            Profile(
                product_id=product.id,
                origin="unclassified",
                manufacturer_name=manufacturer,
                supplier_name=supplier,
                supplier_aas_id=supplier_aas_id,
                global_asset_id=str(fields.get("global_asset_id") or fields.get("asset_id") or "").strip(),
                order_code=str(fields.get("order_code") or fields.get("article_number") or product.part_number or "").strip(),
            )
        )
    Profile.objects.bulk_create(profiles, batch_size=500, ignore_conflicts=True)


def migrate_handover_statuses(apps, schema_editor):
    Handover = apps.get_model("aas_integration", "FestoHandover")
    Handover.objects.filter(status="processing").update(status="publishing")
    Handover.objects.filter(status="complete").update(status="launched")


class Migration(migrations.Migration):
    dependencies = [("aas_integration", "0001_initial")]

    operations = [
        migrations.RemoveConstraint(
            model_name="aasresourceidentity",
            name="aas_identity_exact_source",
        ),
        migrations.AddField(
            model_name="aasresourceidentity",
            name="festo_handover",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="aas_identity",
                to="aas_integration.festohandover",
            ),
        ),
        migrations.AlterField(
            model_name="aasresourceidentity",
            name="source_type",
            field=models.CharField(
                choices=[
                    ("project", "Project"),
                    ("company_product", "Company product"),
                    ("festo_handover", "Festo handover"),
                ],
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="aasresourceidentity",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        source_type="project",
                        project__isnull=False,
                        company_product__isnull=True,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="company_product",
                        project__isnull=True,
                        company_product__isnull=False,
                        festo_handover__isnull=True,
                    )
                    | models.Q(
                        source_type="festo_handover",
                        project__isnull=True,
                        company_product__isnull=True,
                        festo_handover__isnull=False,
                    )
                ),
                name="aas_identity_exact_source",
            ),
        ),
        migrations.CreateModel(
            name="ProductAASProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "origin",
                    models.CharField(
                        choices=[
                            ("manufactured", "Manufactured"),
                            ("purchased", "Purchased"),
                            ("unclassified", "Unclassified"),
                        ],
                        db_index=True,
                        default="unclassified",
                        max_length=24,
                    ),
                ),
                ("manufacturer_name", models.CharField(blank=True, max_length=255)),
                ("supplier_name", models.CharField(blank=True, max_length=255)),
                ("supplier_aas_id", models.CharField(blank=True, max_length=1000)),
                ("global_asset_id", models.CharField(blank=True, max_length=1000)),
                ("order_code", models.CharField(blank=True, max_length=255)),
                ("profile_key", models.CharField(default="idta-product", max_length=100)),
                ("profile_version", models.CharField(default="1", max_length=32)),
                ("standard_fields", models.JSONField(blank=True, default=dict)),
                ("source_payload_hash", models.CharField(blank=True, max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "product",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="aas_profile",
                        to="project_management.availableproductpart",
                    ),
                ),
            ],
            options={"indexes": [models.Index(fields=["origin", "updated_at"], name="aas_integra_origin_96420c_idx")]},
        ),
        migrations.AddField(
            model_name="externalaasclient",
            name="oauth_client_id",
            field=models.CharField(blank=True, max_length=500, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="festohandover",
            name="aas_endpoint",
            field=models.URLField(blank=True, max_length=1000),
        ),
        migrations.AddField(
            model_name="festohandover",
            name="context_snapshot",
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="festohandover",
            name="grant_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="festohandover",
            name="launched_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="festohandover",
            name="status",
            field=models.CharField(
                choices=[
                    ("previewed", "Previewed"),
                    ("publishing", "Publishing"),
                    ("ready", "Ready"),
                    ("launched", "Launched"),
                    ("expired", "Expired"),
                    ("failed", "Failed"),
                    ("cancelled", "Cancelled"),
                ],
                default="previewed",
                max_length=24,
            ),
        ),
        migrations.RunPython(migrate_handover_statuses, migrations.RunPython.noop),
        migrations.RunPython(create_product_profiles, migrations.RunPython.noop),
    ]
