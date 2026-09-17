from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
from django.db.models import Q


class Migration(migrations.Migration):

    dependencies = [
        ("project_management", "0077_historicalobjectassessment_revision_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="document_type",
            field=models.CharField(
                choices=[("invoice", "Invoice"), ("credit_note", "Credit Note")],
                default="invoice",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="reference_number",
            field=models.CharField(blank=True, db_index=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="invoice",
            name="source_invoice",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="credit_note",
                to="project_management.invoice",
            ),
        ),
        migrations.AddField(
            model_name="invoicelineitem",
            name="description",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.CreateModel(
            name="InvoiceReferenceCounter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("prefix", models.CharField(choices=[("INV", "Invoice"), ("CN", "Credit Note")], max_length=10)),
                ("period", models.CharField(max_length=6)),
                ("last_sequence", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "company_profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invoice_reference_counters",
                        to="models.companyprofile",
                    ),
                ),
                (
                    "freelancer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invoice_reference_counters",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                check=Q(document_type="invoice", source_invoice__isnull=True)
                | Q(document_type="credit_note", source_invoice__isnull=False),
                name="invoice_source_invoice_matches_document_type",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.CheckConstraint(
                check=(
                    Q(company_profile__isnull=False, freelancer__isnull=True)
                    | Q(company_profile__isnull=True, freelancer__isnull=False)
                ),
                name="invoice_reference_counter_single_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=False, freelancer__isnull=True),
                fields=("company_profile", "prefix", "period"),
                name="unique_invoice_reference_counter_company_period_prefix",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicereferencecounter",
            constraint=models.UniqueConstraint(
                condition=Q(company_profile__isnull=True, freelancer__isnull=False),
                fields=("freelancer", "prefix", "period"),
                name="unique_invoice_reference_counter_freelancer_period_prefix",
            ),
        ),
    ]
