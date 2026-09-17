from dateutil.relativedelta import relativedelta

from companyprofileapis.models import Opportunity
from models.models import CompanyConfig
from models.offer_validity_periods import (
    OfferValidityPeriodError,
    normalize_offer_validity_period,
    normalize_offer_validity_periods,
    parse_offer_validity_period,
)
from project_management.utils import get_product_name_from_request


class OfferValidityPeriodInUseError(ValueError):
    def __init__(self, values):
        self.values = tuple(sorted(values))
        super().__init__(
            "Cannot remove Offer validity period(s) currently used by Offers: "
            + ", ".join(self.values)
        )


class OfferValiditySelectionError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _opportunities_for_configuration(configuration):
    if configuration.user_id:
        return Opportunity.objects.filter(freelancer_id=configuration.user_id)
    return Opportunity.objects.filter(
        company_profile_id=configuration.company_profile_id
    )


def validate_offer_validity_period_removal(configuration, next_values):
    normalized_values = normalize_offer_validity_periods(next_values)
    if configuration is None:
        return normalized_values

    previous_values = set(
        normalize_offer_validity_periods(
            configuration.offer_validity_periods or []
        )
    )
    removed_values = previous_values - set(normalized_values)
    if not removed_values:
        return normalized_values

    used_values = set(
        _opportunities_for_configuration(configuration)
        .filter(offer_expiry_period__in=removed_values)
        .values_list("offer_expiry_period", flat=True)
    )
    if used_values:
        raise OfferValidityPeriodInUseError(used_values)

    return normalized_values


def calculate_offer_expiry_date(offer_created_date, validity_period):
    period = parse_offer_validity_period(validity_period)
    offsets = {
        "day": {"days": period.duration},
        "week": {"weeks": period.duration},
        "month": {"months": period.duration},
        "year": {"years": period.duration},
    }
    return offer_created_date + relativedelta(**offsets[period.unit])


def get_owner_offer_validity_periods(request_user, request=None):
    filters = {"solution": get_product_name_from_request(request)}
    if request_user.is_freelancer:
        filters["user"] = request_user
    else:
        filters["company_profile"] = request_user.company

    configuration = CompanyConfig.objects.filter(**filters).first()
    if not configuration:
        return []
    return normalize_offer_validity_periods(
        configuration.offer_validity_periods or []
    )


def validate_offer_validity_selection(request_user, request, value):
    if value in (None, ""):
        raise OfferValiditySelectionError(
            "required",
            "Offer Expiry Period is required.",
        )

    try:
        normalized = normalize_offer_validity_period(value)
    except OfferValidityPeriodError as exc:
        raise OfferValiditySelectionError("invalid", str(exc)) from exc

    configured_periods = get_owner_offer_validity_periods(
        request_user,
        request,
    )
    if normalized not in configured_periods:
        raise OfferValiditySelectionError(
            "not_configured",
            "Select an Offer Expiry Period from the company configuration.",
        )
    return normalized


def offer_created_date_for_record(opportunity):
    if opportunity.offer_created_date:
        return opportunity.offer_created_date
    if opportunity.created_at:
        return opportunity.created_at.date()
    return None
