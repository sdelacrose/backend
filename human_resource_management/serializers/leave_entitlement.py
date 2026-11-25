"""LOT RH 9: plain allowlist serialization functions, same convention as
`serialize_leave_for_calendar`/`serialize_manager_opinion` in serializers/
leave.py -- never a raw ModelSerializer dump, so a confidential field can
never leak here just because it exists on the model.
"""
from datetime import date

from human_resource_management.models.leave_entitlement import LeaveEntitlement


def serialize_cycle_period(period):
    return {
        "id": period.id,
        "period_start": period.period_start.isoformat(),
        "period_end": period.period_end.isoformat(),
    }


def serialize_entitlement(entitlement, *, consumed=None, reserved=None):
    # Local import: avoids a module-level import cycle (services imports
    # from human_resource_management.models, which this module also imports).
    from human_resource_management.services.leave_entitlement import (
        entitlement_acquired,
        entitlement_available,
        entitlement_last_processed_at,
    )
    # LOT RH11: local import -- leave_carryover.py imports FROM
    # services.leave_entitlement at its own top level, so importing it back
    # here at module scope would be circular.
    from human_resource_management.services.leave_carryover import (
        entitlement_available_with_carryover, get_active_carry_overs,
        has_any_carry_over_history,
    )

    is_monthly = entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL
    available = own_available = carry_over_available = carry_over_expires_on = None
    closed = LeaveEntitlement.objects.filter(pk=entitlement.pk, closure__isnull=False).exists()
    if consumed is not None and reserved is not None:
        if has_any_carry_over_history(entitlement):
            carry_breakdown = entitlement_available_with_carryover(entitlement, entitlement.cycle_period)
            available = carry_breakdown.available
            own_available = carry_breakdown.own_available
            carry_over_available = carry_breakdown.carry_over_available
            active_carry_overs = get_active_carry_overs(entitlement, date.today())
            carry_over_expires_on = active_carry_overs[0].expires_on if active_carry_overs else None
        else:
            available = entitlement_available(entitlement, consumed, reserved)
            own_available = available
    effective_rate = entitlement.effective_accrual_rate() if is_monthly else None
    last_processed_at = entitlement_last_processed_at(entitlement) if is_monthly else None
    return {
        "id": entitlement.id,
        "employee": {
            "id": entitlement.employee_id,
            "name": getattr(entitlement.employee.user, "full_name", None),
        },
        "leave_type": {
            "id": entitlement.leave_type_id,
            "name": entitlement.leave_type.name,
        },
        "cycle_period": serialize_cycle_period(entitlement.cycle_period),
        "mode": entitlement.mode,
        "quota": float(entitlement.quota),
        "acquired": float(entitlement_acquired(entitlement)) if is_monthly else None,
        "accrual_rate_override": (
            float(entitlement.accrual_rate_override)
            if is_monthly and entitlement.accrual_rate_override is not None else None
        ),
        "accrual_override_effective_from": (
            entitlement.accrual_override_effective_from.isoformat()
            if is_monthly and entitlement.accrual_override_effective_from else None
        ),
        "effective_accrual_rate": float(effective_rate) if effective_rate is not None else None,
        "last_processed_at": last_processed_at.isoformat() if last_processed_at else None,
        "consumed": float(consumed) if consumed is not None else None,
        "reserved": float(reserved) if reserved is not None else None,
        "available": float(available) if available is not None else None,
        # LOT RH11: own (current-rights) vs. carry-over-financed split --
        # own_available falls back to `available` when there is no
        # carry-over history at all (own_available IS the whole figure).
        "own_available": float(own_available) if own_available is not None else None,
        "carry_over_available": float(carry_over_available) if carry_over_available is not None else None,
        "carry_over_expires_on": carry_over_expires_on.isoformat() if carry_over_expires_on else None,
        "closed": closed,
        "last_adjustment_reason": entitlement.last_adjustment_reason,
        "last_adjusted_by": (
            {"id": entitlement.last_adjusted_by_id, "name": getattr(entitlement.last_adjusted_by, "full_name", None)}
            if entitlement.last_adjusted_by_id else None
        ),
        "updated_at": entitlement.updated_at.isoformat(),
    }


def serialize_carryover_history(entitlement):
    """LOT RH11: this entitlement's own closure (if any), the carry-over it
    RECEIVED from the PREVIOUS cycle (and that carry-over's expiration,
    once resolved -- quantity_expired=0 means everything was consumed, not
    that nothing happened), and the carry-over it SENT to the FOLLOWING
    cycle (if its own closure produced one)."""
    closure = getattr(entitlement, "closure", None)
    incoming = getattr(entitlement, "carry_overs_in", None)
    incoming_co = incoming.select_related("source_closure").first() if incoming is not None else None
    outgoing_co = None
    if closure is not None:
        from human_resource_management.models.leave_entitlement import LeaveCarryOver
        outgoing_co = LeaveCarryOver.objects.filter(source_closure=closure).first()
    expiration = getattr(incoming_co, "expiration", None) if incoming_co else None

    def _serialize_closure(c):
        if c is None:
            return None
        return {
            "eligible_balance": float(c.eligible_balance),
            "capped_quantity": float(c.capped_quantity),
            "carried_over_quantity": float(c.carried_over_quantity),
            "cap_applied": float(c.cap_applied) if c.cap_applied is not None else None,
            "expiration_months_applied": c.expiration_months_applied,
            "closed_at": c.closed_at.isoformat(),
            "closed_by": (
                {"id": c.closed_by_id, "name": getattr(c.closed_by, "full_name", None)}
                if c.closed_by_id else None
            ),
        }

    def _serialize_carryover(co):
        if co is None:
            return None
        return {"quantity": float(co.quantity), "expires_on": co.expires_on.isoformat()}

    return {
        "closure": _serialize_closure(closure),
        "carry_over_received": _serialize_carryover(incoming_co),
        "carry_over_sent": _serialize_carryover(outgoing_co),
        "expiration": (
            {
                "quantity_expired": float(expiration.quantity_expired),
                "expired_at": expiration.expired_at.isoformat(),
            }
            if expiration is not None else None
        ),
    }


def serialize_entitlement_history_entry(record):
    return {
        "quota": float(record.quota),
        "accrual_rate_override": (
            float(record.accrual_rate_override)
            if record.accrual_rate_override is not None else None
        ),
        "accrual_override_effective_from": (
            record.accrual_override_effective_from.isoformat()
            if record.accrual_override_effective_from else None
        ),
        "last_adjustment_reason": record.last_adjustment_reason,
        "history_date": record.history_date.isoformat(),
        "history_type": record.history_type,
        "history_user": (
            {"id": record.history_user_id, "name": getattr(record.history_user, "full_name", None)}
            if record.history_user_id else None
        ),
    }


def serialize_cycle_breakdown(breakdown):
    return {
        "cycle_period": serialize_cycle_period(breakdown.cycle_period),
        "mode": breakdown.mode,
        "quota": float(breakdown.quota) if breakdown.quota is not None else None,
        "acquired": float(breakdown.acquired) if breakdown.acquired is not None else None,
        "adjustment": float(breakdown.adjustment) if breakdown.adjustment is not None else None,
        "consumed": float(breakdown.consumed),
        "reserved": float(breakdown.reserved),
        "available": float(breakdown.available) if breakdown.available is not None else None,
        "requested": float(breakdown.requested),
        "last_processed_at": (
            breakdown.last_processed_at.isoformat() if breakdown.last_processed_at else None
        ),
    }


def serialize_accrual_credit(credit):
    return {
        "id": credit.id,
        "period_start": credit.period_start.isoformat(),
        "period_end": credit.period_end.isoformat(),
        "rate_applied": float(credit.rate_applied),
        "proration_fraction": float(credit.proration_fraction),
        "quantity": float(credit.quantity),
        "created_by": (
            {"id": credit.created_by_id, "name": getattr(credit.created_by, "full_name", None)}
            if credit.created_by_id else None
        ),
        "created_at": credit.created_at.isoformat(),
        # LOT RH12: null for every credit posted before this lot (never
        # confused with "policy was inactive" -- see the model's own
        # docstring); explicit True/False for every credit posted by this
        # lot's code. `quotite_breakdown` is the real sequence of
        # [start, end, quotite] sub-periods used for THIS credit, never a
        # single averaged figure -- null when the policy was inactive.
        "quotite_policy_active": credit.quotite_policy_active,
        "quotite_breakdown": credit.quotite_breakdown,
        # LOT RH13: same minimal-extension convention -- null for every
        # credit posted before this lot (never confused with "no absence
        # applied", see the model's own docstring).
        "absence_impact_applied": credit.absence_impact_applied,
        "absence_impact_breakdown": credit.absence_impact_breakdown,
    }
