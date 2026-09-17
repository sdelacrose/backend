import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


OFFER_STATUSES = {"draft", "converted", "issued", "declined", "converted_to_project"}


def _legacy_reference_prefix(opportunity):
    if opportunity.status in OFFER_STATUSES:
        return "OFF"
    if opportunity.status == "closed" and (
        opportunity.value is not None
        or opportunity.sent_date is not None
        or opportunity.approver_id is not None
    ):
        return "OFF"
    return "OPP"


def backfill_opportunity_references(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    HistoricalOpportunity = apps.get_model(
        "companyprofileapis",
        "HistoricalOpportunity",
    )
    OpportunityReferenceCounter = apps.get_model(
        "companyprofileapis",
        "OpportunityReferenceCounter",
    )

    owner_filters = []
    company_ids = (
        Opportunity.objects.filter(
            company_profile__isnull=False,
            freelancer__isnull=True,
        )
        .values_list("company_profile_id", flat=True)
        .distinct()
    )
    owner_filters.extend(
        ("company_profile_id", company_id) for company_id in company_ids
    )
    freelancer_ids = (
        Opportunity.objects.filter(
            company_profile__isnull=True,
            freelancer__isnull=False,
        )
        .values_list("freelancer_id", flat=True)
        .distinct()
    )
    owner_filters.extend(
        ("freelancer_id", freelancer_id) for freelancer_id in freelancer_ids
    )

    for owner_field, owner_id in owner_filters:
        owner_lookup = {
            owner_field: owner_id,
            (
                "freelancer_id"
                if owner_field == "company_profile_id"
                else "company_profile_id"
            ): None,
        }
        opportunities = Opportunity.objects.filter(**owner_lookup).order_by(
            "created_at",
            "id",
        )
        last_sequence = 0
        for opportunity in opportunities.iterator():
            last_sequence += 1
            prefix = _legacy_reference_prefix(opportunity)
            period = opportunity.created_at.strftime("%Y%m")
            reference = f"{prefix}-{period}-{last_sequence:05d}"
            Opportunity.objects.filter(pk=opportunity.pk).update(
                reference=reference,
            )
            HistoricalOpportunity.objects.filter(id=opportunity.pk).update(
                reference=reference,
            )

        counter_defaults = {"last_sequence": last_sequence}
        OpportunityReferenceCounter.objects.update_or_create(
            defaults=counter_defaults,
            **owner_lookup,
        )


def clear_opportunity_references(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    HistoricalOpportunity = apps.get_model(
        "companyprofileapis",
        "HistoricalOpportunity",
    )
    OpportunityReferenceCounter = apps.get_model(
        "companyprofileapis",
        "OpportunityReferenceCounter",
    )
    Opportunity.objects.update(reference=None)
    HistoricalOpportunity.objects.update(reference=None)
    OpportunityReferenceCounter.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("companyprofileapis", "0011_opportunity_linked_folders"),
        ("models", "0124_display_configuration_registry_version_16"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OpportunityReferenceCounter",
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
                ("last_sequence", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField(
            model_name="historicalopportunity",
            name="reference",
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="opportunity",
            name="reference",
            field=models.CharField(
                blank=True,
                db_index=True,
                editable=False,
                max_length=255,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="historicalopportunity",
            name="status",
            field=models.TextField(
                choices=[
                    ("open", "Open"),
                    ("closed", "Closed"),
                    ("draft", "Draft"),
                    ("converted", "Converted To Offer"),
                    ("issued", "Offer Issued"),
                    ("declined", "Offer Declined"),
                    ("converted_to_project", "Converted To Project"),
                ],
                default="open",
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="opportunity",
            name="status",
            field=models.TextField(
                choices=[
                    ("open", "Open"),
                    ("closed", "Closed"),
                    ("draft", "Draft"),
                    ("converted", "Converted To Offer"),
                    ("issued", "Offer Issued"),
                    ("declined", "Offer Declined"),
                    ("converted_to_project", "Converted To Project"),
                ],
                default="open",
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name="opportunityreferencecounter",
            name="company_profile",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="opportunity_reference_counters",
                to="models.companyprofile",
            ),
        ),
        migrations.AddField(
            model_name="opportunityreferencecounter",
            name="freelancer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="opportunity_reference_counters",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(
            backfill_opportunity_references,
            clear_opportunity_references,
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                fields=("company_profile", "reference"),
                name="unique_opportunity_reference_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunity",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                fields=("freelancer", "reference"),
                name="unique_opportunity_reference_freelancer",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunityreferencecounter",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        company_profile__isnull=False,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        company_profile__isnull=True,
                        freelancer__isnull=False,
                    )
                ),
                name="opportunity_reference_counter_single_owner",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunityreferencecounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                fields=("company_profile",),
                name="unique_opportunity_counter_company",
            ),
        ),
        migrations.AddConstraint(
            model_name="opportunityreferencecounter",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                fields=("freelancer",),
                name="unique_opportunity_counter_freelancer",
            ),
        ),
    ]
