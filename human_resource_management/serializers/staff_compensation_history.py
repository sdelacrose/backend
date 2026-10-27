"""L00-B3.1: plain allowlist serialization, same convention as
serializers/staff_quotite_history.py -- never a raw ModelSerializer dump
(this model carries compensation figures, gated by view_compensation).
"""


def serialize_compensation_event(entry):
    return {
        "id": entry.id,
        "staff_id": entry.staff_id,
        "gross_monthly_salary": float(entry.gross_monthly_salary) if entry.gross_monthly_salary is not None else None,
        "hourly_rate": float(entry.hourly_rate) if entry.hourly_rate is not None else None,
        "currency": entry.currency,
        "basis": entry.basis,
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
