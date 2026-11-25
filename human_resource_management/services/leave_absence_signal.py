"""LOT RH13: late-decision signaling -- a DURABLE, RH-visible flag that an
already-posted LeaveAccrualCredit would compute DIFFERENTLY under the
CURRENT state of absence decisions, created from the leave request
approve/reject (serializers.leave.LeaveRequestSerializer.update) and delete
(views.leave.LeaveRequestViewSet.destroy) paths. NEVER recomputes or writes
back to the credit itself -- "jamais recalcul automatique" is absolute; RH
reviews each signal and, if the entitlement's cycle is still open, uses the
PRE-EXISTING manual adjustment mechanism (LeaveEntitlement.quota in
monthly-accrual mode, with its own required justification) as a fully
separate, explicit action from marking this signal resolved.
"""
from django.utils import timezone

from human_resource_management.models.leave_entitlement import (
    LeaveAccrualAbsenceSignal,
    LeaveAccrualCredit,
    LeaveCycleClosure,
    LeaveTypeAbsenceImpactHistory,
)


def recompute_credit_quantity_preview(credit):
    """Independently recomputes what `credit`'s own quantity would be RIGHT
    NOW -- comparison only, never written back. Reuses the exact same
    rate/quotite/absence resolution as `credit_month`, scoped to this
    credit's own already-recorded [period_start, period_end] (its
    cycle-split boundaries are not re-derived -- only the INPUTS that could
    have changed since are re-resolved: the rate override, the quotite
    policy/segments, and the absence impact)."""
    from human_resource_management.services.leave_accrual import (
        QUANT, ROUND_HALF_UP, Decimal, _month_bounds, _weighted_days,
        resolve_historical_override, resolve_historical_policy,
        resolve_historical_rate, resolve_quotite_segments,
    )
    from human_resource_management.services.leave_absence_impact import (
        compute_weighted_presence_days, resolve_absence_impact_segments,
    )

    entitlement = credit.entitlement
    staff = entitlement.employee
    leave_type = entitlement.leave_type
    period_start, period_end = credit.period_start, credit.period_end
    month_start, month_end = _month_bounds(period_start.year, period_start.month)
    days_in_month = (month_end - month_start).days + 1

    historical_override = resolve_historical_override(entitlement, month_end)
    rate = historical_override if historical_override is not None else resolve_historical_rate(leave_type, month_end)

    policy_active = resolve_historical_policy(leave_type, month_end)
    if policy_active:
        window_segments = resolve_quotite_segments(staff, period_start, period_end)
    else:
        window_segments = [(period_start, period_end, Decimal("1"))]

    day_map, _contributing = resolve_absence_impact_segments(
        staff, leave_type, period_start, period_end, company_profile_id=staff.user.company_id,
    )
    weighted_days = compute_weighted_presence_days(
        window_segments, day_map, period_start, period_end, weighted_days_fn=_weighted_days,
    )
    return (Decimal(rate) * weighted_days / Decimal(days_in_month)).quantize(QUANT, rounding=ROUND_HALF_UP)


def check_late_decision_signal(leave_request, *, event_type, actor=None):
    """Called on approval, rejection, or deletion (of a still-pending
    request) of `leave_request`. Finds every already-POSTED
    `LeaveAccrualCredit` this request's own [start_date, end_date] overlaps,
    for every target leave type its own `leave_type` has a configured
    impact policy against, and creates AT MOST ONE signal per credit WHILE
    AN EARLIER ONE FOR THAT SAME CREDIT IS STILL UNRESOLVED -- a credit is
    permanently immutable once posted, so once it is stale every
    subsequent, otherwise-unrelated event touching it would also detect
    "a difference" against the current recomputation; gating on "no
    unresolved signal yet" (rather than on this exact request+event) avoids
    spuriously re-attributing an already-known discrepancy to a later,
    unrelated request. A new signal becomes possible again only after RH
    explicitly resolves the prior one. Never signals when the recomputed
    quantity is UNCHANGED ("ne pas signaler une modification sans
    incidence"). A recomputation that itself fails (e.g. a genuine
    quotite-coverage gap) is skipped silently -- a signal is a side effect,
    never allowed to break the primary approve/reject/delete action. A
    FURTHER event detected while a signal is already open for the same
    credit never creates a second row (see above) but is never silently
    lost either -- it is appended to that open signal's own
    `additional_events` trail, so RH can see every contributing event.

    A request this function could never have been triggered BY a real
    change in what's actually credited is a complete no-op, not merely a
    "no new row" -- reusing LeaveRequest's own `tracker` (pre-existing
    infrastructure, not introduced by this lot) to require that the
    request is APPROVED now or was approved a moment ago: a plain pending
    -> rejected or pending -> deleted transition was NEVER approved at any
    point, so it structurally never fed any day_map/credit and must never
    even be considered a "contributing event" on some OTHER already-stale
    credit it merely happens to overlap in time."""
    is_approved_now = leave_request.status == "approved"
    was_approved = leave_request.tracker.previous("status") == "approved"
    if not is_approved_now and not was_approved:
        return
    if leave_request.is_working_from_home or leave_request.leave_type_id is None:
        return
    target_type_ids = list(
        LeaveTypeAbsenceImpactHistory.objects.filter(source_leave_type_id=leave_request.leave_type_id)
        .values_list("target_leave_type_id", flat=True).distinct()
    )
    if not target_type_ids:
        return

    credits = LeaveAccrualCredit.objects.filter(
        entitlement__employee=leave_request.employee,
        entitlement__leave_type_id__in=target_type_ids,
        period_start__lte=leave_request.end_date,
        period_end__gte=leave_request.start_date,
    ).select_related("entitlement", "entitlement__leave_type")

    for credit in credits:
        try:
            new_quantity = recompute_credit_quantity_preview(credit)
        except Exception:
            continue
        if new_quantity == credit.quantity:
            continue
        reason = (
            f"Leave request #{leave_request.id} ({event_type}) affects an already-posted "
            f"credit for {credit.period_start}..{credit.period_end}: recorded "
            f"{credit.quantity}, now recomputes to {new_quantity}."
        )
        open_signal = LeaveAccrualAbsenceSignal.objects.filter(credit=credit, resolved=False).first()
        if open_signal is not None:
            # A relaunch of the EXACT SAME (request, event) -- already the
            # signal's own primary trigger, or already appended earlier --
            # is a duplicate of a fact already on record, not a new one.
            already_known = (
                open_signal.trigger_event == event_type
                and open_signal.trigger_leave_request_id == leave_request.id
            ) or any(
                e.get("trigger_event") == event_type and e.get("trigger_leave_request_id") == leave_request.id
                for e in open_signal.additional_events
            )
            if not already_known:
                open_signal.additional_events = list(open_signal.additional_events) + [{
                    "trigger_event": event_type,
                    "trigger_leave_request_id": leave_request.id,
                    "detected_at": timezone.now().isoformat(),
                    "reason": reason,
                }]
                open_signal.save(update_fields=["additional_events"])
            continue
        cycle_closed = LeaveCycleClosure.objects.filter(entitlement=credit.entitlement).exists()
        LeaveAccrualAbsenceSignal.objects.create(
            entitlement=credit.entitlement, credit=credit,
            trigger_event=event_type, trigger_leave_request_id=leave_request.id,
            reason=reason,
            cycle_closed=cycle_closed,
        )
