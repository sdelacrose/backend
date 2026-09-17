import logging

from django.db import migrations, models


OFFER_STATUSES = {
    "draft",
    "converted",
    "issued",
    "declined",
    "converted_to_project",
}
OFFER_FOLDER_SYSTEM_KEY = "opportunities_and_offers"
OFFER_DOCUMENT_TYPE = "Offer"
logger = logging.getLogger(__name__)


def backfill_offer_pdf_documents(apps, schema_editor):
    Opportunity = apps.get_model("companyprofileapis", "Opportunity")
    Document = apps.get_model("project_management", "Document")

    offers = Opportunity.objects.filter(offer_pdf_document__isnull=True).filter(
        models.Q(status__in=OFFER_STATUSES)
        | models.Q(status="closed", reference__startswith="OFF-")
    )
    linked_count = 0
    missing_count = 0
    ambiguous_count = 0
    for offer in offers.iterator():
        if not offer.reference:
            continue

        owner_filter = {}
        if offer.company_profile_id and not offer.freelancer_id:
            owner_filter["folder__company_profile_id"] = offer.company_profile_id
        elif offer.freelancer_id and not offer.company_profile_id:
            owner_filter["folder__freelancer_id"] = offer.freelancer_id
        else:
            continue

        matches = list(
            Document.objects.filter(
                folder__system_key=OFFER_FOLDER_SYSTEM_KEY,
                document_type=OFFER_DOCUMENT_TYPE,
                identification_number=offer.reference,
                file_name=f"{offer.reference}.pdf",
                **owner_filter,
            )
            .exclude(file="")
            .values_list("id", flat=True)[:2]
        )
        if not matches:
            missing_count += 1
            continue
        if len(matches) != 1:
            ambiguous_count += 1
            logger.warning(
                "Skipping Offer PDF backfill for Opportunity %s (%s): "
                "multiple matching Documents were found.",
                offer.pk,
                offer.reference,
            )
            continue

        document_id = matches[0]
        Opportunity.objects.filter(pk=offer.pk).update(
            offer_pdf_document_id=document_id
        )
        offer.documents.add(document_id)
        linked_count += 1

    logger.info(
        "Offer PDF backfill complete: linked=%s, missing=%s, ambiguous=%s.",
        linked_count,
        missing_count,
        ambiguous_count,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("companyprofileapis", "0014_offer_expiry_and_pdf_document"),
    ]

    operations = [
        migrations.RunPython(
            backfill_offer_pdf_documents,
            migrations.RunPython.noop,
        ),
    ]
