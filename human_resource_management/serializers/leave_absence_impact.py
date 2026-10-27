"""LOT RH13: plain allowlist serialization, same convention as
serializers/leave_entitlement.py / serializers/staff_quotite_history.py --
never a raw ModelSerializer dump.
"""


def serialize_absence_impact_entry(entry):
    return {
        "id": entry.id,
        "source_leave_type": {"id": entry.source_leave_type_id, "name": entry.source_leave_type.name},
        "target_leave_type": {"id": entry.target_leave_type_id, "name": entry.target_leave_type.name},
        "effect": entry.effect,
        "reduction_factor": float(entry.reduction_factor) if entry.reduction_factor is not None else None,
        "effective_from": entry.effective_from.isoformat(),
        "created_by": (
            {"id": entry.created_by_id, "name": getattr(entry.created_by, "full_name", None)}
            if entry.created_by_id else None
        ),
        "created_at": entry.created_at.isoformat(),
    }


def serialize_absence_signal(signal):
    return {
        "id": signal.id,
        "entitlement_id": signal.entitlement_id,
        "employee": {
            "id": signal.entitlement.employee_id,
            "name": getattr(signal.entitlement.employee.user, "full_name", None),
        },
        "target_leave_type": {
            "id": signal.entitlement.leave_type_id, "name": signal.entitlement.leave_type.name,
        },
        "credit_id": signal.credit_id,
        "credit_period": (
            {"period_start": signal.credit.period_start.isoformat(), "period_end": signal.credit.period_end.isoformat()}
            if signal.credit_id else None
        ),
        "trigger_event": signal.trigger_event,
        "trigger_leave_request_id": signal.trigger_leave_request_id,
        "reason": signal.reason,
        "cycle_closed": signal.cycle_closed,
        "detected_at": signal.detected_at.isoformat(),
        "resolved": signal.resolved,
        "resolved_by": (
            {"id": signal.resolved_by_id, "name": getattr(signal.resolved_by, "full_name", None)}
            if signal.resolved_by_id else None
        ),
        "resolved_at": signal.resolved_at.isoformat() if signal.resolved_at else None,
        "resolution_note": signal.resolution_note,
        "additional_events": signal.additional_events,
    }
