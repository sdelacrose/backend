"""LOT RH13: absence-type -> target-type accrual impact, additive to RH12
(quotite proration), itself additive to RH10 (monthly accrual). Quota (RH9),
carry-over (RH11), request-day-counting and work-from-home are never
touched by this -- see LeaveTypeAbsenceImpactHistory's own docstring for the
effect/resolution semantics (NONE/SUSPEND/REDUCE, resolved PER DAY/HALF-DAY,
never once at month-end).
"""
from datetime import timedelta
from decimal import Decimal

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave_entitlement import LeaveTypeAbsenceImpactHistory
from human_resource_management.services.leave_balance import get_organization_working_days
from models.models import LeaveType


class LeaveAbsenceImpactError(Exception):
    """Blocks a credit BEFORE anything is persisted -- either a genuine
    configuration problem (bad effect/factor combination) or two APPROVED
    absences found covering the exact same day/half-day portion for the
    same employee/target type. The latter is structurally prevented by the
    normal submission-time overlap guard (serializers.leave._requests_
    conflict is type-agnostic); this is the defensive backstop for data
    introduced outside that path -- never silently summed, never guessed."""


def resolve_historical_absence_impact(source_leave_type, target_leave_type, as_of_date):
    """(effect, reduction_factor) in force for this (source, target) pair on
    `as_of_date` -- the LATEST row with effective_from <= as_of_date. (NONE,
    None) if nothing resolves: "aucune politique configuree = aucun effet"
    is a genuinely safe default, never a blocking error (unlike quotite
    coverage, there is no unsafe gap here)."""
    record = (
        LeaveTypeAbsenceImpactHistory.objects.filter(
            source_leave_type=source_leave_type, target_leave_type=target_leave_type,
            effective_from__lte=as_of_date,
        ).order_by("-effective_from", "-created_at").first()
    )
    if record is None:
        return LeaveTypeAbsenceImpactHistory.EFFECT_NONE, None
    return record.effect, record.reduction_factor


def record_absence_impact_change(
    source_leave_type, target_leave_type, effect, reduction_factor, *, effective_from, created_by=None,
):
    """The ONE write path for LeaveTypeAbsenceImpactHistory. Validates the
    effect/factor combination; the UNIQUE constraint on (source, target,
    effective_from) is the hard guarantee against two conflicting policies
    both claiming the same effective date for the same pair. A genuine
    no-op (identical effect+factor already the latest for this pair) adds
    no spurious row -- same convention as record_rate_change/record_policy_
    change. Deactivation is simply a new row with effect=NONE -- the
    history itself is never deleted."""
    if effect == LeaveTypeAbsenceImpactHistory.EFFECT_REDUCE:
        if reduction_factor is None:
            raise LeaveAbsenceImpactError("reduction_factor is required for REDUCE.")
        reduction_factor = Decimal(reduction_factor)
        if not (Decimal("0") <= reduction_factor <= Decimal("1")):
            raise LeaveAbsenceImpactError("reduction_factor must be between 0 and 1.")
    else:
        if reduction_factor is not None:
            raise LeaveAbsenceImpactError("reduction_factor must be null for NONE/SUSPEND.")
        reduction_factor = None

    latest = (
        LeaveTypeAbsenceImpactHistory.objects.filter(
            source_leave_type=source_leave_type, target_leave_type=target_leave_type,
        ).order_by("-effective_from", "-created_at").first()
    )
    if latest is not None and latest.effect == effect and latest.reduction_factor == reduction_factor:
        return latest
    if LeaveTypeAbsenceImpactHistory.objects.filter(
        source_leave_type=source_leave_type, target_leave_type=target_leave_type,
        effective_from=effective_from,
    ).exists():
        raise LeaveAbsenceImpactError(
            f"A policy for this pair is already recorded effective {effective_from} -- "
            f"ambiguous. Choose a different effective date."
        )
    return LeaveTypeAbsenceImpactHistory.objects.create(
        source_leave_type=source_leave_type, target_leave_type=target_leave_type,
        effect=effect, reduction_factor=reduction_factor,
        effective_from=effective_from, created_by=created_by,
    )


def resolve_absence_impact_segments(employee, target_leave_type, window_start, window_end, *, company_profile_id):
    """Half-day-aware absence impact for [window_start, window_end].

    Returns (day_map, contributing):
      - day_map: {date: {"full": Decimal} or {"morning": Decimal, "afternoon": Decimal}}
        -- the REDUCTION fraction (never the presence fraction) in force for
        that exact portion, resolved on that exact day (never a single
        month-end snapshot). A day/half absent from day_map has no absence
        impact at all (full presence implied).
      - contributing: [(LeaveRequest, effect, reduction_factor, clip_start,
        clip_end), ...] for traceability -- effect/factor are those resolved
        at the request's OWN first affected day (a request whose single span
        straddles a policy CHANGE is still computed exactly, per day, in
        day_map; this summary is for the audit breakdown's label only).

    Only APPROVED, non-WFH absences whose `leave_type` has at least one
    impact-policy row (any effect) targeting `target_leave_type` are
    considered -- WFH is structurally excluded regardless of which LeaveType
    it happens to be tied to (`is_working_from_home=False` filtered here,
    never configurable). A source type's OWN deduction_mode governs which
    days even count as "absent" at all -- an absence in working-days mode
    never touches a weekend/excluded day's weight (that day was never part
    of its own deductible count either, see services.leave_balance).

    Raises LeaveAbsenceImpactError if two approved absences would cover the
    EXACT SAME day/half portion -- never summed, never guessed which wins.
    """
    source_type_ids = list(
        LeaveTypeAbsenceImpactHistory.objects.filter(target_leave_type=target_leave_type)
        .values_list("source_leave_type_id", flat=True).distinct()
    )
    if not source_type_ids:
        return {}, []

    requests = list(
        LeaveRequest.objects.filter(
            employee=employee, leave_type_id__in=source_type_ids,
            status=StatusType.APPROVED.value, is_working_from_home=False,
            start_date__lte=window_end, end_date__gte=window_start,
        ).select_related("leave_type")
    )
    if not requests:
        return {}, []

    working_days = get_organization_working_days(company_profile_id)
    day_map = {}
    contributing = []

    for req in requests:
        clip_start = max(req.start_date, window_start)
        clip_end = min(req.end_date, window_end)
        if clip_start > clip_end:
            continue
        deduction_mode = req.leave_type.effective_value("deduction_mode")
        current = clip_start
        summary = None
        while current <= clip_end:
            if deduction_mode == LeaveType.DEDUCTION_WORKING_DAYS and current.isoweekday() not in working_days:
                current += timedelta(days=1)
                continue
            effect, factor = resolve_historical_absence_impact(req.leave_type, target_leave_type, current)
            if effect != LeaveTypeAbsenceImpactHistory.EFFECT_NONE:
                resolved_factor = (
                    Decimal("1") if effect == LeaveTypeAbsenceImpactHistory.EFFECT_SUSPEND else factor
                )
                key = (req.half_day_period or "full") if req.is_half_day else "full"
                slot = day_map.setdefault(current, {})
                conflict = (
                    key in slot
                    or ("full" in slot)
                    or (key == "full" and slot)
                )
                if conflict:
                    raise LeaveAbsenceImpactError(
                        f"Two approved absences overlap the same portion on {current.isoformat()} "
                        f"for employee {employee.pk}, target type '{target_leave_type.name}' -- "
                        f"refusing to guess which reduction applies."
                    )
                slot[key] = resolved_factor
                if summary is None:
                    summary = (effect, factor)
            current += timedelta(days=1)
        if summary is not None:
            contributing.append((req, summary[0], summary[1], clip_start, clip_end))
    return day_map, contributing


def compute_weighted_presence_days(quotite_segments, day_map, window_start, window_end, *, weighted_days_fn):
    """Decimal weighted-days sum over [window_start, window_end], combining
    quotite (resolved per day from `quotite_segments`) MULTIPLICATIVELY with
    any absence presence-fraction for that day from `day_map` -- half-day
    aware: a day with one or two half-day absence halves contributes the
    AVERAGE of its two half presence-fractions (each defaulting to full
    presence=1 for an untouched half), never summed/subtracted a second
    time. `weighted_days_fn` is RH12's own `_weighted_days` (segment-sum
    fast path), reused UNCHANGED and bit-identically whenever `day_map` is
    empty -- "aucune politique -> resultat RH12 inchange"."""
    if not day_map:
        clipped = [
            (max(seg_start, window_start), min(seg_end, window_end), quotite)
            for seg_start, seg_end, quotite in quotite_segments
            if seg_start <= window_end and seg_end >= window_start
        ]
        return weighted_days_fn(clipped)

    def _quotite_for_day(d):
        for seg_start, seg_end, quotite in quotite_segments:
            if seg_start <= d <= seg_end:
                return quotite
        return Decimal("1")

    total = Decimal("0")
    current = window_start
    while current <= window_end:
        quotite_d = _quotite_for_day(current)
        slot = day_map.get(current)
        if slot is None:
            presence = Decimal("1")
        elif "full" in slot:
            presence = Decimal("1") - slot["full"]
        else:
            morning_reduction = slot.get("morning", Decimal("0"))
            afternoon_reduction = slot.get("afternoon", Decimal("0"))
            morning_presence = Decimal("1") - morning_reduction
            afternoon_presence = Decimal("1") - afternoon_reduction
            presence = (morning_presence + afternoon_presence) / Decimal("2")
        total += quotite_d * presence
        current += timedelta(days=1)
    return total


def build_absence_impact_breakdown(contributing, sub_start, sub_end):
    """JSON-serializable, durable snapshot for LeaveAccrualCredit.absence_
    impact_breakdown -- plain request id (never a live FK: the request may
    later be deleted), source type id/name, effect/factor, the portion's
    OWN clipped dates, half-day flag, and its status AT CREDIT TIME (always
    'approved' by construction -- recorded explicitly anyway, for audit
    completeness)."""
    breakdown = []
    for req, effect, factor, clip_start, clip_end in contributing:
        portion_start = max(clip_start, sub_start)
        portion_end = min(clip_end, sub_end)
        if portion_start > portion_end:
            continue
        breakdown.append({
            "request_id": req.id,
            "source_leave_type_id": req.leave_type_id,
            "source_leave_type_name": req.leave_type.name,
            "effect": effect,
            "reduction_factor": str(factor) if factor is not None else "1",
            "start": portion_start.isoformat(),
            "end": portion_end.isoformat(),
            "is_half_day": req.is_half_day,
            "half_day_period": req.half_day_period,
            "status_at_credit": req.status,
        })
    return breakdown
