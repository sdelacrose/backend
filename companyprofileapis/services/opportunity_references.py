import re

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from companyprofileapis.models import Opportunity, OpportunityReferenceCounter


REFERENCE_PATTERN = re.compile(r"^(?:OPP|OFF)-\d{6}-(\d+)$")


def _lock_reference_owner(*, company_profile=None, freelancer=None):
    owner = company_profile or freelancer
    owner.__class__._default_manager.select_for_update().only("pk").get(
        pk=owner.pk,
    )


def _existing_max_sequence(*, company_profile=None, freelancer=None):
    queryset = Opportunity.objects.all()
    if company_profile is not None:
        queryset = queryset.filter(
            company_profile=company_profile,
            freelancer__isnull=True,
        )
    else:
        queryset = queryset.filter(
            company_profile__isnull=True,
            freelancer=freelancer,
        )

    maximum = 0
    for reference in queryset.exclude(reference__isnull=True).values_list(
        "reference",
        flat=True,
    ):
        match = REFERENCE_PATTERN.match(reference)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum


@transaction.atomic
def allocate_opportunity_reference(
    *,
    prefix,
    company_profile=None,
    freelancer=None,
    reference_date=None,
):
    if (company_profile is None) == (freelancer is None):
        raise serializers.ValidationError(
            "An Opportunity reference requires exactly one owner."
        )

    # Lock the stable owner row before looking up the counter. This also
    # serializes the very first allocation, when no counter row exists yet.
    _lock_reference_owner(
        company_profile=company_profile,
        freelancer=freelancer,
    )

    lookup = {
        "company_profile": company_profile,
        "freelancer": freelancer,
    }
    counter, _ = OpportunityReferenceCounter.objects.select_for_update().get_or_create(
        defaults={**lookup, "last_sequence": 0},
        **lookup,
    )
    next_sequence = max(
        counter.last_sequence,
        _existing_max_sequence(**lookup),
    ) + 1
    counter.last_sequence = next_sequence
    counter.save(update_fields=["last_sequence", "updated_at"])

    date_value = reference_date or timezone.localdate()
    return f"{prefix}-{date_value:%Y%m}-{next_sequence:05d}"


def convert_opportunity_reference(reference):
    if reference.startswith(f"{Opportunity.ReferencePrefix.OFFER}-"):
        return reference
    opportunity_prefix = f"{Opportunity.ReferencePrefix.OPPORTUNITY}-"
    if not reference.startswith(opportunity_prefix):
        raise serializers.ValidationError(
            "The Opportunity reference cannot be converted to an Offer reference."
        )
    return reference.replace(
        opportunity_prefix,
        f"{Opportunity.ReferencePrefix.OFFER}-",
        1,
    )
