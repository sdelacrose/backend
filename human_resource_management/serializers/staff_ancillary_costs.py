"""L00-B3.2: plain allowlist serialization for the event-sourced history
models (same convention as staff_compensation_history.py/
staff_quotite_history.py), plus a standard ModelSerializer for the plain,
mutable CompanyCostItemType catalog.
"""
from rest_framework import serializers

from human_resource_management.models.staff import CompanyCostItemType


def serialize_charge_rate_event(entry):
    return {
        "id": entry.id,
        "company_profile_id": entry.company_profile_id,
        "employee_rate_percent": float(entry.employee_rate_percent),
        "employer_rate_percent": float(entry.employer_rate_percent),
        "event_type": entry.event_type,
        "supersedes_id": entry.supersedes_id,
        "effective_from": entry.effective_from.isoformat(),
        "note": entry.note or "",
        "created_by": (
            {"id": entry.created_by_id, "name": getattr(entry.created_by, "full_name", None)}
            if entry.created_by_id else None
        ),
        "created_at": entry.created_at.isoformat(),
    }


def serialize_ancillary_cost_event(entry):
    return {
        "id": entry.id,
        "staff_id": entry.staff_id,
        "cost_item_type_id": entry.cost_item_type_id,
        "cost_item_type_name": entry.cost_item_type.name,
        "amount": float(entry.amount) if entry.amount is not None else None,
        "currency": entry.currency,
        "frequency": entry.frequency,
        "included_in_consolidated_cost": entry.included_in_consolidated_cost,
        "nature": entry.nature,
        "event_type": entry.event_type,
        "supersedes_id": entry.supersedes_id,
        "effective_from": entry.effective_from.isoformat(),
        "note": entry.note or "",
        "created_by": (
            {"id": entry.created_by_id, "name": getattr(entry.created_by, "full_name", None)}
            if entry.created_by_id else None
        ),
        "created_at": entry.created_at.isoformat(),
    }


class CompanyCostItemTypeSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyCostItemType
        fields = [
            "id", "name", "default_frequency", "default_included_in_consolidated_cost",
            "social_tax_treatment", "is_active", "created_at",
        ]
        read_only_fields = ["id", "created_at"]
