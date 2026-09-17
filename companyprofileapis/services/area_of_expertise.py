from rest_framework import serializers

from ecpmpapp.constants import ENERGY_CONSULTANT_SOLUTION
from models.hierarchical_configurations import (
    HierarchicalConfigurationError,
    validate_area_of_expertise_selection,
)
from models.models import CompanyConfig


AREA_OF_EXPERTISE_FIELDS = (
    "main_area_of_expertise",
    "sub_area_of_expertise",
)


class SubAreaOfExpertiseListField(serializers.ListField):
    child = serializers.CharField(max_length=255)

    def to_internal_value(self, data):
        # Keep older clients compatible while the UI moves from one value to many.
        if data in (None, ""):
            data = []
        elif isinstance(data, str):
            data = [data]
        return super().to_internal_value(data)


def get_company_config_for_request(request):
    if not request:
        return None

    user = getattr(request, "user", None)
    if not user:
        return None

    query_params = getattr(request, "query_params", None)
    if query_params is None:
        query_params = getattr(request, "GET", {})
    solution = query_params.get("solution", ENERGY_CONSULTANT_SOLUTION)

    filters = {"solution": solution}
    if getattr(user, "is_freelancer", False):
        filters["user"] = user
    else:
        company = getattr(user, "company", None)
        if company is None:
            return None
        filters["company_profile"] = company

    return CompanyConfig.objects.filter(**filters).only(
        "company_roles",
        "nested_configurations",
        "configuration_visibility",
    ).first()


def _normalize_multiple_sub_areas(value):
    if value in (None, ""):
        return []

    raw_values = value if isinstance(value, (list, tuple)) else [value]
    normalized_values = []
    seen = set()
    for raw_value in raw_values:
        normalized_value = str(raw_value or "").strip()
        if normalized_value and normalized_value not in seen:
            normalized_values.append(normalized_value)
            seen.add(normalized_value)
    return normalized_values


def validate_area_of_expertise_fields(
    attrs,
    *,
    instance=None,
    company_config=None,
    allow_multiple_sub_areas=False,
):
    if not any(field in attrs for field in AREA_OF_EXPERTISE_FIELDS):
        return attrs

    main_area = attrs.get(
        "main_area_of_expertise",
        getattr(instance, "main_area_of_expertise", None),
    )
    sub_area = attrs.get(
        "sub_area_of_expertise",
        getattr(instance, "sub_area_of_expertise", None),
    )
    nested_configurations = getattr(
        company_config,
        "nested_configurations",
        None,
    )
    configuration_visibility = getattr(
        company_config,
        "configuration_visibility",
        None,
    )

    try:
        if allow_multiple_sub_areas:
            sub_areas = _normalize_multiple_sub_areas(sub_area)
            main_area, _ = validate_area_of_expertise_selection(
                main_area,
                None,
                nested_configurations=nested_configurations,
                configuration_visibility=configuration_visibility,
            )
            for selected_sub_area in sub_areas:
                validate_area_of_expertise_selection(
                    main_area,
                    selected_sub_area,
                    nested_configurations=nested_configurations,
                    configuration_visibility=configuration_visibility,
                )
            sub_area = sub_areas
        else:
            main_area, sub_area = validate_area_of_expertise_selection(
                main_area,
                sub_area,
                nested_configurations=nested_configurations,
                configuration_visibility=configuration_visibility,
            )
    except HierarchicalConfigurationError as exc:
        raise serializers.ValidationError({"area_of_expertise": str(exc)}) from exc

    attrs["main_area_of_expertise"] = main_area
    attrs["sub_area_of_expertise"] = sub_area
    return attrs
