"""LOT RH12: plain allowlist serialization, same convention as
serializers/leave_entitlement.py -- never a raw ModelSerializer dump.
"""


def serialize_quotite_history_entry(entry):
    return {
        "id": entry.id,
        "staff_id": entry.staff_id,
        "contractual_hours": float(entry.contractual_hours),
        "full_time_reference_hours": float(entry.full_time_reference_hours),
        "quotite": float(entry.quotite),
        "effective_from": entry.effective_from.isoformat(),
        "created_by": (
            {"id": entry.created_by_id, "name": getattr(entry.created_by, "full_name", None)}
            if entry.created_by_id else None
        ),
        "created_at": entry.created_at.isoformat(),
    }
