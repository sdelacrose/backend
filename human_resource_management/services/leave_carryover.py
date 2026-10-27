"""LOT RH11: capped carry-over + mandatory expiration of unused leave
balance into the immediately following cycle, compatible with both quota
(RH9) and monthly-accrual (RH10) modes.

Architecture, reusing existing precedents throughout:
  - `LeaveCycleClosure` / `LeaveCarryOver` / `LeaveExpiration` /
    `LeaveCarryOverAllocation` (models/leave_entitlement.py) are all
    IMMUTABLE, created exactly once -- same "never rewrite history"
    discipline as `LeaveAccrualCredit`/`LeaveTypeAccrualRateHistory`.
  - The allocation engine (`allocate_consumption_for_cycle`) is the ONE
    shared mechanism for balance checks, consultation, closure and
    expiration -- requests walked in a single deterministic pass, ordered
    by id ascending (the same order the per-employee `Staff.select_for_update()`
    lock already serializes creation in), each envelope decremented at
    most once. A day is never double-counted: within a request, its own
    days are also walked in chronological order, cut at cycle AND
    carry-over-expiry boundaries exactly like `resolve_cycle_periods_for_range`
    already cuts at cycle boundaries.
  - Before a carry-over's `expires_on`, everything is pure derivation
    (recomputed fresh every time, nothing persisted) -- a cancellation
    before that date is handled "for free": the cancelled request simply
    stops appearing in the walk. AFTER `expires_on`, the walk's outcome is
    frozen once into `LeaveCarryOverAllocation`/`LeaveExpiration` by
    `finalize_due_carryovers` -- never revised afterward, so a LATER
    cancellation can never retroactively reopen or reattribute an envelope
    that no longer exists.
  - `finalize_due_carryovers(entitlement)` is the ONE function called,
    under the SAME `Staff.select_for_update()` lock already taken by every
    balance-affecting write path, immediately before that write's own
    logic -- and by the scheduled job for entitlements not otherwise
    touched. Whichever caller gets the lock first finalizes (idempotent,
    guarded by `LeaveExpiration`'s OneToOne/unique constraint); the next
    one finds it already done and proceeds straight to its own operation.
  - The *mandatory* expiration-when-carry-over-is-enabled rule (model
    `clean()`) is a documented BUSINESS POLICY choice, not a technical
    requirement of this algorithm, which would work identically with a
    never-expiring envelope (it would simply always rank first in
    priority, forever) -- left explicit, not re-argued here.

Explicitly NOT implemented in this lot (left exactly as scoped): part-time
proration, absence-linked accrual suspension, retroactive override dating
beyond what RH10F already does, corrections to an already-closed cycle.
"""
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import connection, transaction
from django.utils import timezone

from ecpmpapp.constants import StatusType
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave_entitlement import (
    LeaveCarryOver,
    LeaveCarryOverAllocation,
    LeaveCycleClosure,
    LeaveEntitlement,
    LeaveExpiration,
)
from human_resource_management.services.leave_accrual import (
    LeaveAccrualError,
    resolve_company_timezone,
)
from human_resource_management.services.leave_balance import (
    compute_leave_request_days,
    get_organization_working_days,
)
from human_resource_management.services.leave_entitlement import (
    LeaveEntitlementError,
    entitlement_acquired,
    entitlement_available,
    resolve_cycle_period,
    sum_existing_requests,
)
from models.models import Staff

logger = logging.getLogger(__name__)

QUANT = Decimal("0.000001")

ACTIVE_STATUSES = (StatusType.PENDING.value, StatusType.APPROVED.value)

# Arbitrary, stable, distinct from leave_accrual's own advisory lock key.
_CARRYOVER_RUN_LOCK_KEY = 721004321


class LeaveCarryOverError(LeaveEntitlementError):
    """Carry-over/expiration/closure-specific failure."""


class LeaveCycleClosedError(LeaveCarryOverError):
    """This entitlement's cycle is already closed -- any write affecting its
    balance is refused outright. Consultation/history are never gated by
    this (callers of this function only ever guard WRITE paths)."""


class LeaveCycleClosurePendingError(LeaveCarryOverError):
    """Closure refused: a PENDING request still exists on this cycle."""


def _safe_date(year, month, day):
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


def _add_months(start, months):
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    return _safe_date(year, month, start.day)


def compute_expires_on(destination_cycle_start, expiration_months):
    """Last USABLE day (inclusive) for a carry-over into a cycle starting
    `destination_cycle_start`, given a `expiration_months`-month delay --
    e.g. 2027-01-01 + 3 months -> 2027-04-01, minus 1 day -> 2027-03-31.
    Reuses the exact same month-end clamping (`_safe_date`) the cycle
    resolver itself already uses, so a cycle starting on the 31st behaves
    identically to how this codebase already resolves recurring (month,
    day) patterns elsewhere."""
    return _add_months(destination_cycle_start, expiration_months) - timedelta(days=1)


def get_active_carry_overs(entitlement, as_of_date):
    """Non-expired carry-overs INTO `entitlement`, earliest-expiring first
    -- "active" as of `as_of_date` (expires_on >= as_of_date), independent
    of whether `finalize_due_carryovers` has run yet (pure date
    comparison, see module docstring). Answers "how much could a NEW
    request still draw from" -- correctly 0 once past `expires_on`, by
    design (a new request can never use an already-expired carry-over)."""
    return list(
        LeaveCarryOver.objects.filter(
            destination_entitlement=entitlement, expires_on__gte=as_of_date,
        ).order_by("expires_on", "id")
    )


def has_any_carry_over_history(entitlement):
    """True iff `entitlement` has EVER had a `LeaveCarryOver` into it --
    active, unresolved-but-expired, or already fully finalized. THE gating
    condition for callers deciding whether to route through the
    carry-over-aware engine at all (`_check_type_cumulative_or_raise`,
    `get_entitlement_breakdown`): once true, it stays true forever, because
    an existing request's frozen `LeaveCarryOverAllocation` must keep being
    excluded from "own" consumption for as long as that request exists,
    long after its carry-over itself is fully resolved -- the plain legacy
    path (`sum_existing_requests`/`entitlement_available`) has no notion of
    that exclusion and would otherwise silently double-count it against
    current rights."""
    return LeaveCarryOver.objects.filter(destination_entitlement=entitlement).exists()


def get_unresolved_carry_overs(entitlement):
    """Carry-overs INTO `entitlement` not yet finalized (no `LeaveExpiration`
    row), earliest-expiring first -- regardless of whether `expires_on` has
    already passed. THE gating question for "does this entitlement's
    history still need the carry-over-aware engine at all": a carry-over
    past its own `expires_on` but not yet swept by `finalize_due_carryovers`
    must still be walked (band-cut at its own `expires_on`, same as always)
    so that EXISTING requests keep their correct historical attribution --
    otherwise a live read/check taken in the gap between the real expiry
    date and whenever the job (or the next write) catches up would
    misreport that consumption as "own", silently inflating it (LOT RH11
    section 4: "reads exclude expired capacity immediately even without the
    job ... never artificially transfer a historical carry-over to current
    acquisition"). Once finalized, a carry-over drops out of this list for
    good -- its already-processed requests are then read via their own
    frozen `LeaveCarryOverAllocation` row instead (see
    `allocate_consumption_for_cycle`)."""
    return list(
        LeaveCarryOver.objects.filter(
            destination_entitlement=entitlement, expiration__isnull=True,
        ).order_by("expires_on", "id")
    )


def _cycle_sub_bands(cycle_period, carry_overs):
    """The cycle split into consecutive [start, end] bands, cut at every
    carry-over `expires_on` that falls strictly inside it -- mirrors how
    `resolve_cycle_periods_for_range` already cuts at cycle boundaries,
    one level finer. Each band carries the list of carry-overs still
    eligible for a day ending on/before that band's own end (earliest
    expiry first)."""
    cutoffs = sorted({
        co.expires_on for co in carry_overs
        if cycle_period.period_start <= co.expires_on < cycle_period.period_end
    })
    bounds = [cycle_period.period_start]
    bounds += [c + timedelta(days=1) for c in cutoffs]
    bounds += [cycle_period.period_end + timedelta(days=1)]
    bands = []
    for i in range(len(bounds) - 1):
        band_start = bounds[i]
        band_end = bounds[i + 1] - timedelta(days=1)
        if band_start > band_end:
            continue
        eligible = sorted(
            (co for co in carry_overs if co.expires_on >= band_end),
            key=lambda co: (co.expires_on, co.id),
        )
        bands.append((band_start, band_end, eligible))
    return bands


def allocate_consumption_for_cycle(
    entitlement, cycle_period, *, statuses, exclude_request_id=None,
    extra_probes=None,
):
    """THE single shared allocation engine (balance checks, consultation,
    closure, expiration all go through this). Walks every request of
    `entitlement`'s (employee, leave_type) overlapping `cycle_period` whose
    status is in `statuses`, ordered by id ascending, and within each
    request walks its days in chronological order (cut at every active
    carry-over's `expires_on`), consuming the earliest-expiring eligible
    envelope first, then this entitlement's own acquisition/quota -- each
    envelope decremented AT MOST ONCE per request, by construction of the
    single ordered pass.

    `extra_probes` (unsaved LeaveRequest-like objects, no id) are appended
    LAST, in the given order -- the correct way to check "would a NEW
    request (or batch of same-type items) fit": last in line for any
    scarce/expiring envelope, consistent with first-come-first-served (the
    same `Staff.select_for_update()` lock already serializes who actually
    gets created/approved first).

    Gathers `get_unresolved_carry_overs(entitlement)` -- every carry-over
    not yet finalized, WHETHER OR NOT its `expires_on` has already passed
    (punctual vs. delayed give the identical walk: each band is cut at a
    carry-over's own `expires_on` regardless of "today", so an
    already-past-due-but-not-yet-finalized carry-over is still walked
    exactly as `finalize_due_carryovers` itself will, moments later, freeze
    it). An already-finalized carry-over is correctly absent from this
    gathering -- its past requests are read via their own frozen
    `LeaveCarryOverAllocation` row instead (below), never re-derived.

    Returns (totals, per_request):
      totals: {carry_over_id: Decimal consumed, "own": Decimal consumed}
      per_request: [(leave_request_or_probe, {carry_over_id: Decimal, "own": Decimal}), ...]
    """
    company_profile_id = entitlement.employee.user.company_id
    working_days = get_organization_working_days(company_profile_id)
    carry_overs = get_unresolved_carry_overs(entitlement)
    remaining = {co.id: co.quantity for co in carry_overs}

    requests_qs = LeaveRequest.objects.filter(
        employee=entitlement.employee, leave_type=entitlement.leave_type,
        status__in=statuses,
        start_date__lte=cycle_period.period_end, end_date__gte=cycle_period.period_start,
    ).order_by("id").prefetch_related("carry_over_allocations")
    if exclude_request_id is not None:
        requests_qs = requests_qs.exclude(pk=exclude_request_id)

    sequence = list(requests_qs)
    if extra_probes:
        sequence.extend(extra_probes)

    bands = _cycle_sub_bands(cycle_period, carry_overs)
    totals = {co.id: Decimal(0) for co in carry_overs}
    totals["own"] = Decimal(0)
    per_request = []

    for leave_request in sequence:
        # LOT RH11: a request already swept by a PAST finalize_due_carryovers
        # carries its own immutable record (section 5) -- never re-derive it
        # live. Its carry-over may since have dropped out of `carry_overs`/
        # `bands` entirely (already expired), and re-deriving would dump its
        # whole consumption onto "own" ("never artificially transfer a
        # historical carry-over to current acquisition").
        frozen_allocations = list(leave_request.carry_over_allocations.all()) if leave_request.pk else []
        if frozen_allocations:
            request_alloc = {}
            frozen_total = Decimal(0)
            for alloc in frozen_allocations:
                request_alloc[alloc.carry_over_id] = alloc.quantity_allocated
                frozen_total += alloc.quantity_allocated
                if alloc.carry_over_id in totals:
                    totals[alloc.carry_over_id] += alloc.quantity_allocated
            total_days = compute_leave_request_days(
                leave_request, cycle_period.period_start, cycle_period.period_end,
                company_profile_id=company_profile_id, working_days=working_days,
            ).deductible
            own_share = total_days - frozen_total
            if own_share > 0:
                totals["own"] += own_share
                request_alloc["own"] = own_share
            per_request.append((leave_request, request_alloc))
            continue

        request_alloc = {}
        for band_start, band_end, eligible in bands:
            breakdown = compute_leave_request_days(
                leave_request, band_start, band_end,
                company_profile_id=company_profile_id, working_days=working_days,
            )
            days_left = breakdown.deductible
            if days_left <= 0:
                continue
            for co in eligible:
                if days_left <= 0:
                    break
                take = min(days_left, remaining[co.id])
                if take > 0:
                    remaining[co.id] -= take
                    totals[co.id] += take
                    request_alloc[co.id] = request_alloc.get(co.id, Decimal(0)) + take
                    days_left -= take
            if days_left > 0:
                totals["own"] += days_left
                request_alloc["own"] = request_alloc.get("own", Decimal(0)) + days_left
        per_request.append((leave_request, request_alloc))

    return totals, per_request


@dataclass
class CarryOverAwareBreakdown:
    own_available: Decimal
    carry_over_available: Decimal
    available: Decimal
    requested: Decimal = Decimal(0)
    per_request: list = field(default_factory=list)


def entitlement_available_with_carryover(
    entitlement, cycle_period, *, exclude_request_id=None, probes=None,
):
    """Carry-over-aware replacement for the plain `entitlement_available()`
    call, used ONLY when this entitlement has at least one active
    carry-over -- callers fall back to the EXISTING, unchanged
    `sum_existing_requests`/`entitlement_available` path otherwise (zero
    behavior change for every entitlement/company not using this feature).

    `probes`, if given, are unsaved LeaveRequest-like objects representing
    the NEW days being checked (a batch of same-type items is summed
    together, same semantics as `_check_type_cumulative_or_raise`) -- their
    combined requested amount is returned separately (`requested`), and
    does not count toward `available` itself (consistent with
    `get_entitlement_breakdown`'s existing "requested" semantics).

    `available` must reflect capacity BEFORE the probes' own consumption,
    never after -- `allocate_consumption_for_cycle` walks probes through
    the SAME shared pool as existing requests (so an existing request's
    own earlier claim on a scarce envelope is correctly accounted for),
    but its `totals` therefore also includes the probes' OWN hypothetical
    share. Subtracting each probe's own `per_request` entry back out of
    `totals` recovers "existing-only" consumption -- without this, a
    request for the LAST unit of truly available capacity would
    self-refuse (its own simulated consumption subtracted from the very
    capacity it is being checked against)."""
    probes = probes or []
    totals, per_request = allocate_consumption_for_cycle(
        entitlement, cycle_period, statuses=ACTIVE_STATUSES,
        exclude_request_id=exclude_request_id, extra_probes=probes,
    )
    probe_ids = {id(p) for p in probes}
    existing_totals = dict(totals)
    for leave_request, alloc in per_request:
        if id(leave_request) in probe_ids:
            for key, value in alloc.items():
                existing_totals[key] = existing_totals.get(key, Decimal(0)) - value
    carry_overs = get_active_carry_overs(entitlement, date.today())
    carry_over_total = sum((co.quantity for co in carry_overs), Decimal(0))
    carry_over_consumed = sum(
        (v for k, v in existing_totals.items() if k != "own"), Decimal(0),
    )
    own_available = entitlement_available(
        entitlement, existing_totals["own"], Decimal(0),
    ) if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL else (
        entitlement.quota - existing_totals["own"]
    )
    carry_over_available = carry_over_total - carry_over_consumed
    requested = sum(
        (sum(alloc.values(), Decimal(0)) for lr, alloc in per_request if id(lr) in probe_ids),
        Decimal(0),
    )
    return CarryOverAwareBreakdown(
        own_available=own_available,
        carry_over_available=carry_over_available,
        available=own_available + carry_over_available,
        requested=requested,
        per_request=per_request,
    )


def raise_if_cycle_closed(entitlement):
    """Refuses outright if `entitlement`'s cycle has already been closed --
    called at the START of every balance-affecting write (create, modify,
    decision, cancel, adjust, retroactive credit), under the per-employee
    lock. Consultation/history are never gated by this function."""
    if LeaveCycleClosure.objects.filter(entitlement=entitlement).exists():
        raise LeaveCycleClosedError(
            f"Entitlement {entitlement.pk}'s cycle is already closed -- no "
            f"write affecting its balance is accepted. Corrections to an "
            f"already-closed cycle are deferred, not supported in this lot."
        )


def finalize_due_carryovers(entitlement):
    """THE single mechanism for turning an overdue carry-over into a
    frozen, stable historical record -- called under the SAME
    `Staff.select_for_update()` lock and the SAME transaction as every
    balance-affecting write, immediately BEFORE that write's own logic,
    and by the scheduled job for entitlements not otherwise touched.
    Idempotent: `LeaveExpiration` is a OneToOneField on `LeaveCarryOver`,
    so a second caller (job or concurrent write) racing for the same lock
    simply finds it already done and is a no-op.

    Processes every `LeaveCarryOver` INTO `entitlement` whose `expires_on`
    has passed and has no `LeaveExpiration` yet, earliest-expiring first.
    For each: walks `allocate_consumption_for_cycle`, which bands by each
    carry-over's own `expires_on` regardless of "today" -- so a delayed run
    reproduces the IDENTICAL snapshot a punctual one would have (the only
    thing that could differ is a genuinely NEW pre-expiry-dated request
    created in the interim -- which is correctly included either way,
    exactly like `run_monthly_accrual`'s own catch-up philosophy)."""
    due = (
        LeaveCarryOver.objects.filter(
            destination_entitlement=entitlement,
            expires_on__lt=date.today(),
            expiration__isnull=True,
        ).order_by("expires_on", "id")
    )
    for carry_over in due:
        # Re-check under the lock already held by the caller: another
        # racing finalize (job or concurrent write) may have just created it.
        if LeaveExpiration.objects.filter(carry_over=carry_over).exists():
            continue
        cycle_period = carry_over.destination_entitlement.cycle_period
        totals, per_request = allocate_consumption_for_cycle(
            entitlement, cycle_period, statuses=ACTIVE_STATUSES,
        )
        consumed_from_this_co = Decimal(0)
        for leave_request, alloc in per_request:
            quantity = alloc.get(carry_over.id)
            if quantity:
                LeaveCarryOverAllocation.objects.create(
                    carry_over=carry_over, leave_request=leave_request,
                    original_request_id=leave_request.id, quantity_allocated=quantity,
                )
                consumed_from_this_co += quantity
        quantity_expired = (carry_over.quantity - consumed_from_this_co).quantize(
            QUANT, rounding=ROUND_HALF_UP
        )
        LeaveExpiration.objects.create(
            carry_over=carry_over, quantity_expired=quantity_expired,
        )
        logger.info(
            "finalize_due_carryovers: carry_over=%s expired, quantity_expired=%s",
            carry_over.id, quantity_expired,
        )


def close_cycle(entitlement, *, as_of=None, actor=None):
    """Closes `entitlement`'s cycle under the per-employee lock -- creates
    a `LeaveCycleClosure` UNCONDITIONALLY (even when carry-over is
    disabled or the eligible balance is zero), and, only when the type has
    carry-over enabled and a positive capped amount results, a
    `LeaveCarryOver` into the immediately following cycle. Both rows are
    created atomically in the same transaction.

    Blocks (raises, never silently decides) if:
      - a PENDING request still exists on this cycle;
      - a monthly-accrual month on this cycle could not be resolved
        (missing contract_start_date/rate history/etc).
    Never double-counts: the entitlement's OWN eligible balance already
    excludes any carry-over-IN this cycle may itself have received (a
    carry-over is never re-carried, see module docstring)."""
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=entitlement.employee_id)
        entitlement.refresh_from_db()
        raise_if_cycle_closed(entitlement)
        finalize_due_carryovers(entitlement)

        cycle_period = entitlement.cycle_period
        leave_type = entitlement.leave_type

        if LeaveRequest.objects.filter(
            employee=entitlement.employee, leave_type=leave_type,
            status=StatusType.PENDING.value,
            start_date__lte=cycle_period.period_end, end_date__gte=cycle_period.period_start,
        ).exists():
            raise LeaveCycleClosurePendingError(
                f"Entitlement {entitlement.pk} cannot be closed: a PENDING "
                f"request still exists on this cycle. Resolve it first -- "
                f"never decided automatically."
            )

        if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
            from human_resource_management.services.leave_accrual import (
                credit_month, resolve_company_timezone as _rct,
                last_complete_local_month,
            )
            try:
                company_timezone = _rct(leave_type.company_config)
            except LeaveAccrualError as exc:
                raise LeaveCarryOverError(
                    f"Entitlement {entitlement.pk} cannot be closed: {exc}"
                ) from exc
            last_year, last_month = last_complete_local_month(company_timezone, as_of)
            cycle_end = cycle_period.period_end
            if (last_year, last_month) < (cycle_end.year, cycle_end.month):
                raise LeaveCarryOverError(
                    f"Entitlement {entitlement.pk} cannot be closed: the monthly "
                    f"accrual for this cycle is not yet fully processed through "
                    f"{cycle_end} (last complete local month: {last_year}-{last_month:02d})."
                )

        consumed = sum_existing_requests(
            entitlement.employee, leave_type, cycle_period,
            status=StatusType.APPROVED.value, exclude_request_id=None,
            company_profile_id=entitlement.employee.user.company_id,
            working_days=get_organization_working_days(entitlement.employee.user.company_id),
        )
        if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
            eligible_balance = entitlement_acquired(entitlement) + entitlement.quota - consumed
        else:
            eligible_balance = entitlement.quota - consumed
        # Never double-count a carry-over this entitlement itself received
        # as part of ITS OWN eligible-to-carry-forward balance.
        incoming_carry_over = sum(
            (co.quantity for co in get_active_carry_overs(entitlement, cycle_period.period_end)),
            Decimal(0),
        )
        eligible_balance = max(eligible_balance - incoming_carry_over, Decimal(0))

        cap = leave_type.carry_over_cap
        enabled = bool(leave_type.carry_over_enabled)
        carried_over_quantity = Decimal(0)
        capped_quantity = Decimal(0)
        if enabled and eligible_balance > 0:
            carried_over_quantity = min(eligible_balance, cap) if cap is not None else eligible_balance
            capped_quantity = eligible_balance - carried_over_quantity

        closure = LeaveCycleClosure.objects.create(
            entitlement=entitlement,
            eligible_balance=eligible_balance,
            capped_quantity=capped_quantity,
            carried_over_quantity=carried_over_quantity,
            cap_applied=cap if enabled else None,
            expiration_months_applied=leave_type.carry_over_expiration_months if enabled else None,
            closed_by=actor,
        )

        if carried_over_quantity > 0:
            next_reference_date = cycle_period.period_end + timedelta(days=1)
            destination_period = resolve_cycle_period(leave_type, next_reference_date)
            destination_entitlement, _ = LeaveEntitlement.objects.get_or_create(
                employee=entitlement.employee, leave_type=leave_type,
                cycle_period=destination_period,
                defaults={
                    "mode": entitlement.mode, "quota": Decimal("0"),
                    "last_adjustment_reason": "",
                },
            )
            expires_on = compute_expires_on(
                destination_period.period_start, leave_type.carry_over_expiration_months,
            )
            LeaveCarryOver.objects.create(
                source_closure=closure, destination_entitlement=destination_entitlement,
                quantity=carried_over_quantity, expires_on=expires_on,
            )
        return closure


@dataclass
class CarryOverRunReport:
    closed: int = 0
    errors: list = field(default_factory=list)
    finalized_expirations: int = 0


def run_cycle_closure(*, company_profile_id=None, as_of=None, actor=None):
    """Batch entry point (management command) -- closes every ELIGIBLE,
    not-yet-closed entitlement whose cycle has genuinely ended in the
    company's own timezone (reuses `resolve_company_timezone`, no UTC
    fallback), company by company. Guarded by its OWN Postgres advisory
    lock (distinct key from `run_monthly_accrual`'s), so this job and a
    concurrent one (or a manual close) can never race. Per-entitlement
    isolation: one entitlement's error (missing config, pending request)
    is reported and skipped -- never aborts the rest of the run."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_CARRYOVER_RUN_LOCK_KEY])
        acquired = cursor.fetchone()[0]
    if not acquired:
        logger.warning("run_cycle_closure: refused -- another run already holds the advisory lock.")
        raise LeaveCarryOverError(
            "A cycle-closure run is already in progress on this database -- "
            "refusing to start a second, concurrent run."
        )
    try:
        report = CarryOverRunReport()
        entitlements = LeaveEntitlement.objects.filter(
            leave_type__company_config__leave_entitlement_mode_active=True,
            closure__isnull=True,
        ).select_related("leave_type__company_config", "cycle_period", "employee__user")
        if company_profile_id is not None:
            entitlements = entitlements.filter(
                leave_type__company_config__company_profile_id=company_profile_id
            )
        for entitlement in entitlements:
            # "Has this cycle genuinely ended" is answered in the company's
            # OWN explicit IANA timezone for EVERY entitlement, quota or
            # monthly-accrual alike -- never a silent fallback to the
            # server's/UTC date. `accrual_timezone` is a plain CompanyConfig
            # field, independent of any particular type's monthly_accrual_
            # rate: requiring it here never forces a quota-only company to
            # configure monthly accrual, it only asks every company to say
            # explicitly which calendar day it is operating in -- the same
            # principle `close_cycle`'s own monthly-accrual-specific
            # completeness check already applies one level deeper.
            try:
                company_timezone = resolve_company_timezone(entitlement.leave_type.company_config)
            except LeaveAccrualError as exc:
                report.errors.append((entitlement.id, str(exc)))
                continue
            today = timezone.localtime(as_of or timezone.now(), company_timezone).date()
            if today <= entitlement.cycle_period.period_end:
                continue  # cycle hasn't ended yet
            try:
                finalize_due_carryovers(entitlement)
                close_cycle(entitlement, as_of=as_of, actor=actor)
                report.closed += 1
            except LeaveCarryOverError as exc:
                report.errors.append((entitlement.id, str(exc)))
                logger.error("run_cycle_closure error: entitlement=%s: %s", entitlement.id, exc)
        logger.info(
            "run_cycle_closure: closed=%s errors=%s", report.closed, len(report.errors),
        )
        return report
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_CARRYOVER_RUN_LOCK_KEY])
