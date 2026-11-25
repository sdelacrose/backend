"""LOT RH 10: monthly accrual, additive to the LOT RH 9 quota mode.

Scope, as specified: credit a fraction of a per-leave-type monthly rate at
the end of each local calendar month, proratized for a partial month by the
intersection of [calendar month] x [employee contract period] x [leave
type's cycle period] x [leave type's accrual effective date onward]. No
anticipation (never credits a future/incomplete month), no carry-over/
expiration, no absence-linked accrual suspension, no full cycle closure --
all explicitly deferred, not invented here.

LOT RH12: part-time quotite proration, ADDITIVE and OPT-IN per leave type
(`LeaveType.prorate_accrual_by_quotite`, default False -- every type's
behaviour is bit-identical to before unless explicitly activated). See
`resolve_historical_policy`/`resolve_quotite_segments`/`create_quotite_
history_entry` below, and `credit_month`'s own docstring for the exact
calculation. Quota (RH9), carry-over (RH11) and request-day-counting
(services.leave_balance) are never touched by this -- quotite is applied
exactly once, to monthly accrual only.

LOT RH10 follow-up: "end of local month" now means exactly that --
`CompanyConfig.accrual_timezone` (an explicit, RH-set IANA zone name, e.g.
"Europe/Paris") is mandatory for automatic processing. There is no fallback
to Django's global TIME_ZONE and the zone is never inferred from the
company's country -- a company with no (or an invalid) timezone configured
is explicitly reported as blocked, company by company, never silently
defaulted to UTC.

`credit_month` reuses the exact same `Staff.objects.select_for_update()`
lock (inside `transaction.atomic()`) as every other RH9/RH10 write path, so
a monthly credit run can never race a concurrent reservation, approval or
manual adjustment for the same employee. Each individual
`LeaveAccrualCredit.objects.create()` additionally runs inside its own
savepoint (nested `transaction.atomic()`): if the unique constraint on
(entitlement, period_start, period_end) rejects it (a concurrent or retried
run already credited that exact period), only that savepoint rolls back --
the surrounding Staff-locked transaction and any sibling cycle-split credit
already written in this same call keep committing normally. That constraint
is the final backstop; the lock above already prevents same-employee races
in the normal case.
"""
import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import IntegrityError, connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from human_resource_management.models.leave_entitlement import (
    LeaveAccrualCredit,
    LeaveEntitlement,
    LeaveTypeAccrualRateHistory,
    LeaveTypeQuotiteProrationHistory,
    StaffQuotiteHistory,
)
from human_resource_management.services.leave_absence_impact import (
    build_absence_impact_breakdown,
    compute_weighted_presence_days,
    resolve_absence_impact_segments,
)
from human_resource_management.services.leave_entitlement import (
    LeaveEntitlementError,
    resolve_cycle_periods_for_range,
)
from models.models import LeaveType, Staff

QUANT = Decimal("0.000001")


class LeaveAccrualError(LeaveEntitlementError):
    """Monthly-accrual-specific failure (missing contract date, missing
    rate, quota-mode row already occupying the slot, ...). Subclasses
    LeaveEntitlementError so existing broad handlers still catch it."""


class LeaveAccrualTimezoneError(LeaveAccrualError):
    """A company has no (or an invalid) `accrual_timezone` configured --
    automatic monthly-accrual processing is blocked for that company until
    RH sets one explicitly. Never a silent fallback to Django's global
    TIME_ZONE, never inferred from the company's country."""


class LeaveAccrualQuotiteError(LeaveAccrualError):
    """LOT RH12: a monthly-accrual-eligible window for an employee under an
    ACTIVE quotite-proration policy has no `StaffQuotiteHistory` row
    covering its start -- blocked explicitly, never a silent 100% fallback
    (per this lot's own requirement). Only raised when the policy is
    active; an inactive policy never requires quotite data at all."""


class LeaveAccrualConcurrentRunError(LeaveAccrualError):
    """Another `run_monthly_accrual` call already holds this job's Postgres
    advisory lock -- refused outright rather than risk two overlapping runs
    (e.g. a manual run and a scheduled one) double-processing the same
    (employee, leave type, month) slot. Session-scoped
    (`pg_try_advisory_lock`/`pg_advisory_unlock`), deliberately NOT
    transaction-scoped: each (employee, leave_type, month)'s own short
    `credit_month` transaction keeps committing independently exactly as
    before, so a crash mid-run still preserves whatever was already credited
    (idempotent resume) -- the lock itself is simply released by Postgres
    the moment the holding connection drops, even on a hard crash."""


# Arbitrary, stable 32-bit key identifying ONLY this job's advisory lock --
# shares the DB-wide `pg_try_advisory_lock` keyspace, so this value must stay
# unique among any other advisory locks this codebase may ever take.
_ACCRUAL_RUN_LOCK_KEY = 721004210


def first_of_next_month(today=None):
    """The 1st of the month after `today` (defaults to the real current
    date). Shared by every write path that defaults a rate/override
    effective date to "the next month" when the caller doesn't supply one
    explicitly -- LeaveType.monthly_accrual_rate changes (serializers.
    leave_type) and LeaveEntitlement.accrual_rate_override changes
    (views.leave_entitlement.adjust)."""
    today = today or date.today()
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


def _days_in_month(year, month):
    return calendar.monthrange(year, month)[1]


def _month_bounds(year, month):
    last_day = _days_in_month(year, month)
    return date(year, month, 1), date(year, month, last_day)


def resolve_company_timezone(company_config):
    """The explicit, RH-configured IANA zone for this company's monthly
    accrual processing -- reuses the same `zoneinfo.ZoneInfo` validation
    this codebase already applies to a per-request timezone parameter
    elsewhere (ai_tools.services.request_timezone), applied here to a
    persisted, per-company value instead of an ad-hoc request param."""
    tz_name = company_config.accrual_timezone
    if not tz_name:
        raise LeaveAccrualTimezoneError(
            f"CompanyConfig {company_config.pk} has no accrual_timezone configured. "
            f"Automatic monthly-accrual processing is blocked for this company until "
            f"RH sets an explicit IANA timezone name."
        )
    try:
        return ZoneInfo(str(tz_name))
    except ZoneInfoNotFoundError:
        raise LeaveAccrualTimezoneError(
            f"CompanyConfig {company_config.pk}'s accrual_timezone '{tz_name}' is not "
            f"a valid IANA timezone name. Automatic monthly-accrual processing is "
            f"blocked until RH corrects it."
        )


def last_complete_local_month(company_timezone, as_of=None):
    """(year, month) of the last calendar month that has fully elapsed IN
    THE GIVEN COMPANY TIMEZONE. As of any moment within month M (local to
    that zone), that is M-1 -- this is the hard "never before month-end
    closure" boundary every caller here is built around. `company_timezone`
    is mandatory (see resolve_company_timezone) -- there is no default
    zone. `as_of`, if given as a plain `date` (not a `datetime`), is taken
    as already being that local calendar date (the convention this
    module's own deterministic tests use); an aware `datetime` (or None,
    meaning "now") is converted into `company_timezone` first."""
    if as_of is not None and not isinstance(as_of, datetime):
        local_date = as_of
    else:
        moment = as_of or timezone.now()
        local_date = timezone.localtime(moment, company_timezone).date()
    first_of_this_month = date(local_date.year, local_date.month, 1)
    last_day_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_day_of_prev_month.year, last_day_of_prev_month.month


def record_rate_change(leave_type, new_rate, *, effective_from=None, created_by=None):
    """Appends a `LeaveTypeAccrualRateHistory` row for `leave_type` if
    `new_rate` genuinely differs from the latest recorded one (a no-op save
    that doesn't actually change the rate -- e.g. editing an unrelated field
    -- never adds a spurious row). Call this from wherever `LeaveType.
    monthly_accrual_rate` is written (the LeaveType serializer), never from
    `credit_month` itself. `effective_from` defaults to today; for the
    type's very FIRST recorded rate, the caller should pass the type's
    `accrual_effective_date` instead so the whole history starting from that
    date resolves without a gap (see LeaveTypeViewSet)."""
    if new_rate is None:
        return
    latest = (
        LeaveTypeAccrualRateHistory.objects.filter(leave_type=leave_type)
        .order_by("-effective_from", "-created_at")
        .first()
    )
    if latest is not None and latest.rate == Decimal(new_rate):
        return
    LeaveTypeAccrualRateHistory.objects.create(
        leave_type=leave_type,
        rate=new_rate,
        effective_from=effective_from or date.today(),
        created_by=created_by,
    )


def resolve_historical_rate(leave_type, as_of_date):
    """The rate that governs a month whose LAST day is `as_of_date` --
    LOT RH10 follow-up: resolved from `LeaveTypeAccrualRateHistory`, never
    from the type's CURRENT `monthly_accrual_rate` directly, so a rate
    change made after that month elapsed (but before it was actually
    credited, e.g. a delayed catch-up) cannot silently apply the new rate to
    history. The LATEST row with `effective_from <= as_of_date` wins --
    deterministic and justified by "crediting only ever happens after
    month-end closure", so "whichever rate was in force when the month
    closed" governs that whole month, without inventing a within-month
    split. Falls back to the inherited (admin-template) type's own history
    when this type has none of its own, mirroring `effective_accrual_rate()`
    -- but the admin side has no write path wired to `record_rate_change` in
    this pass (every configured rate seen in this project's own data is set
    directly on the company-scope type, never purely via inheritance), a
    documented, untested boundary, not a silent assumption. Raises if
    NOTHING resolves anywhere in the chain -- blocked, never guessed."""
    record = (
        LeaveTypeAccrualRateHistory.objects.filter(
            leave_type=leave_type, effective_from__lte=as_of_date,
        )
        .order_by("-effective_from", "-created_at")
        .first()
    )
    if record is not None:
        return record.rate
    if leave_type.inherits_from_id:
        return resolve_historical_rate(leave_type.inherits_from, as_of_date)
    raise LeaveAccrualError(
        f"No accrual rate history resolves '{leave_type.name}' for the period "
        f"ending {as_of_date} -- this month cannot be credited without guessing "
        f"a rate. RH must ensure a rate was recorded effective on/before this date."
    )
    return record.rate


def record_policy_change(leave_type, new_value, *, effective_from=None, created_by=None):
    """LOT RH12: appends a `LeaveTypeQuotiteProrationHistory` row for
    `leave_type` if `new_value` genuinely differs from the latest recorded
    one (a no-op save adds no spurious row) -- exact mirror of
    `record_rate_change` for the boolean policy. Call this from wherever
    `LeaveType.prorate_accrual_by_quotite` is written (the LeaveType
    serializer), never from `credit_month` itself."""
    latest = (
        LeaveTypeQuotiteProrationHistory.objects.filter(leave_type=leave_type)
        .order_by("-effective_from", "-created_at")
        .first()
    )
    if latest is not None and latest.is_active == bool(new_value):
        return
    LeaveTypeQuotiteProrationHistory.objects.create(
        leave_type=leave_type,
        is_active=bool(new_value),
        effective_from=effective_from or date.today(),
        created_by=created_by,
    )


def resolve_historical_policy(leave_type, as_of_date):
    """LOT RH12: whether quotite proration was ACTIVE for `leave_type` as of
    the CLOSE of the month ending `as_of_date` -- resolved from
    `LeaveTypeQuotiteProrationHistory`, never from the type's CURRENT
    `prorate_accrual_by_quotite` directly, same reasoning as
    `resolve_historical_rate`. Unlike the rate, False (no proration) is a
    perfectly safe, correct default when NOTHING resolves -- a type that
    never recorded any policy history was, by construction, never activated
    at any point in its past, so this never raises."""
    record = (
        LeaveTypeQuotiteProrationHistory.objects.filter(
            leave_type=leave_type, effective_from__lte=as_of_date,
        )
        .order_by("-effective_from", "-created_at")
        .first()
    )
    if record is not None:
        return record.is_active
    if leave_type.inherits_from_id:
        return resolve_historical_policy(leave_type.inherits_from, as_of_date)
    return False


def create_quotite_history_entry(
    staff, *, contractual_hours, full_time_reference_hours, effective_from, created_by=None,
):
    """LOT RH12: the ONE write path for `StaffQuotiteHistory` -- validates
    both hours strictly positive, computes `quotite` server-side (never
    accepted as independent input, so hours and quotite can never silently
    disagree), and refuses outright (never clamps to 100%) when contractual
    hours exceed the confirmed full-time reference. Opportunistically syncs
    `Staff.weekly_hours` to this same `contractual_hours` value IF this entry
    is already in effect (`effective_from <= today`) -- a programmed FUTURE
    change never prematurely overwrites the currently-effective figure; the
    authoritative "current" quotite for calculation/display is always
    resolved from history as-of-today, `weekly_hours` is kept in sync purely
    for cross-module display convenience, never a second independent value."""
    contractual_hours = Decimal(contractual_hours)
    full_time_reference_hours = Decimal(full_time_reference_hours)
    if contractual_hours <= 0:
        raise LeaveAccrualQuotiteError("Contractual hours must be strictly positive.")
    if full_time_reference_hours <= 0:
        raise LeaveAccrualQuotiteError("Full-time reference hours must be strictly positive.")
    if contractual_hours > full_time_reference_hours:
        raise LeaveAccrualQuotiteError(
            f"Contractual hours ({contractual_hours}) exceed the full-time reference "
            f"({full_time_reference_hours}) -- this is never treated as a quotite above "
            f"100%. Correct the reference or the hours before recording this entry."
        )
    quotite = (contractual_hours / full_time_reference_hours).quantize(
        QUANT, rounding=ROUND_HALF_UP
    )
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=staff.pk)
        entry = StaffQuotiteHistory.objects.create(
            staff=staff,
            contractual_hours=contractual_hours,
            full_time_reference_hours=full_time_reference_hours,
            quotite=quotite,
            effective_from=effective_from,
            created_by=created_by,
        )
        if effective_from <= date.today():
            staff.weekly_hours = contractual_hours
            staff.save(update_fields=["weekly_hours"])
    return entry


def resolve_current_quotite_entry(staff, as_of=None):
    """LOT RH12 follow-up: the `StaffQuotiteHistory` row genuinely in effect
    for `staff` as of `as_of` (defaults to today) -- the ONE authoritative
    source for "current contractual hours/quotite", resolved live on every
    read rather than trusted from `Staff.weekly_hours` directly (which is
    only ever an opportunistic mirror, see `create_quotite_history_entry`).
    None if the employee has no quotite history at all yet (never a guess).

    This is what lets "situation A now, B scheduled next month" become "B"
    automatically the day B's effective_from arrives -- purely by being
    re-resolved on the next read, with NO scheduled job or new planning
    infrastructure: the day one UPDATE/GET of the Staff record on/after
    that date simply resolves a different row."""
    as_of = as_of or date.today()
    return (
        StaffQuotiteHistory.objects.filter(staff=staff, effective_from__lte=as_of)
        .order_by("-effective_from", "-created_at")
        .first()
    )


def resolve_quotite_segments(staff, window_start, window_end):
    """LOT RH12: the employee's quotite, split into ordered, non-overlapping
    [seg_start, seg_end, quotite] segments exactly covering
    [window_start, window_end] -- one segment per genuine quotite CHANGE
    inside the window (see `credit_month`'s worked examples: 15 days at
    100% then 15 at 80% is two segments, never one averaged figure).

    Raises `LeaveAccrualQuotiteError` if the EARLIEST `StaffQuotiteHistory`
    row for this employee starts AFTER `window_start` -- a genuine coverage
    gap at the start of the window, which would otherwise require guessing
    a quotite for the uncovered days. Only ever called when the type's
    quotite-proration policy is ACTIVE (see credit_month) -- an inactive
    policy never looks at this data at all."""
    records = list(
        StaffQuotiteHistory.objects.filter(
            staff=staff, effective_from__lte=window_end,
        ).order_by("effective_from", "created_at")
    )
    if not records or records[0].effective_from > window_start:
        raise LeaveAccrualQuotiteError(
            f"Staff {staff.pk} has no quotite history covering {window_start} -- "
            f"quotite proration is active for this leave type but RH has not "
            f"recorded a contractual situation effective on/before this date. "
            f"No credit is posted for this window until this is resolved."
        )
    segments = []
    cursor = window_start
    for idx, record in enumerate(records):
        next_effective_from = (
            records[idx + 1].effective_from if idx + 1 < len(records) else None
        )
        seg_end = (
            min(window_end, next_effective_from - timedelta(days=1))
            if next_effective_from is not None else window_end
        )
        if cursor > seg_end:
            continue
        segments.append((cursor, seg_end, record.quotite))
        cursor = seg_end + timedelta(days=1)
        if cursor > window_end:
            break
    return segments


def _weighted_days(segments):
    """Exact Decimal sum of quotite x days-in-segment across `segments` --
    never rounded until the single, final quantize step in `credit_month`
    (per this lot's own "sommer sans arrondi intermediaire" requirement)."""
    total = Decimal("0")
    for seg_start, seg_end, quotite in segments:
        total += quotite * Decimal((seg_end - seg_start).days + 1)
    return total


def resolve_historical_override(entitlement, as_of_date):
    """LOT RH10F closure: the per-employee `accrual_rate_override` value in
    effect at the CLOSE of the month ending `as_of_date` -- resolved from
    `LeaveEntitlement.history`'s own `accrual_override_effective_from` field
    (a genuine BUSINESS date, mirrored into every historical snapshot by
    `simple_history` like any other model field), never from `history_date`
    (simple_history's own audit timestamp: when the row was actually saved,
    by whom via `last_adjusted_by` -- that trail must stay accurate and is
    never repurposed as a business date, or adjustments made on the SAME
    day would become impossible to tell apart by real save order).

    A row's override is "in force" as of `as_of_date` if its
    `accrual_override_effective_from` is None (the entitlement's very
    first, creation-time override -- unconditional, no queued future start)
    or falls on/before `as_of_date`. Among every row satisfying that, the
    one with the LATEST effective moment wins -- None is treated as the
    earliest possible moment (`date.min`) for this comparison only, so an
    explicit, later-dated change always supersedes the unconditional
    creation-time value once its date arrives, and a row not yet in force
    (future `accrual_override_effective_from`) is simply never a candidate
    for a cutoff before that date -- `history_date` only breaks ties
    between rows sharing the exact same effective date.

    Returns None (meaning "no override at that point -- use the leave
    type's own rate") both when nothing has an override set yet, AND when
    the entitlement itself didn't exist yet at that point (e.g. created
    fresh by this very catch-up run for a past month) -- a normal,
    resolvable state: a not-yet-existing override is exactly equivalent to
    no override. There is no "unresolvable" case to block on here, unlike
    the type-level rate, which has no safe default."""
    best = None  # (effective_key, history_date, override_value)
    for record in entitlement.history.all():
        effective_key = record.accrual_override_effective_from or date.min
        if effective_key > as_of_date:
            continue
        candidate_key = (effective_key, record.history_date)
        if best is None or candidate_key > (best[0], best[1]):
            best = (effective_key, record.history_date, record.accrual_rate_override)
    return best[2] if best is not None else None


def _iter_months(start_year, start_month, end_year, end_month):
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def compute_presence_window(staff, leave_type, month_start, month_end):
    """Intersection of [month_start, month_end] with the employee's contract
    period and the leave type's accrual-effective-date-onward window.
    Returns (start, end) or None if the employee had no presence at all
    during this month (not yet hired / already departed / before the type's
    effective date). Raises LeaveAccrualError if a date needed to answer
    that question is simply not configured -- never guessed."""
    if staff.contract_start_date is None:
        raise LeaveAccrualError(
            f"Staff {staff.pk} has no contract_start_date configured. RH must set it "
            f"explicitly before monthly accrual can be processed for this employee -- "
            f"it is never inferred from the account creation date."
        )
    effective_date = leave_type.effective_accrual_effective_date()
    if effective_date is None:
        raise LeaveAccrualError(
            f"Leave type '{leave_type.name}' has no accrual_effective_date configured."
        )
    start = max(month_start, staff.contract_start_date, effective_date)
    end = month_end
    if staff.contract_end_date is not None:
        end = min(end, staff.contract_end_date)
    if start > end:
        return None
    return start, end


def credit_month(staff, leave_type, month_start, month_end, *, created_by=None):
    """Credits one calendar month of monthly accrual for (staff, leave_type).
    Splits across cycle periods if the month straddles a cycle boundary --
    each sub-period is credited to its own (lazily-resolved) LeaveEntitlement
    row. Idempotent: an already-credited (entitlement, period_start,
    period_end) is detected (by a pre-check, backstopped by the unique
    constraint for genuine concurrent races) and returned as not-newly-
    created, never duplicated -- including when only SOME of a split
    month's sub-periods were credited by an earlier, interrupted run.

    LOT RH10 follow-up -- precision: the authoritative amount for the whole
    presence window is computed ONCE from the exact (unrounded) rate x
    window_days / days_in_month and quantized only at that final step (never
    from an intermediate rounded fraction -- 16/30 x 2.4 now yields exactly
    1.280000, not 1.279999). `proration_fraction` is still stored for audit,
    computed independently, but is never the arithmetic source of
    `quantity`. When the window is split across cycle periods sharing the
    SAME effective rate, every sub-period except the chronologically LAST
    is rounded independently and the last one absorbs whatever residual
    remains (`window_total - sum of the others` -- including others that
    were already on record from a prior partial run, not a fresh
    recomputation of them), so the parts always reconcile EXACTLY to the
    one authoritative total. If sub-periods resolve to DIFFERENT effective
    rates (only possible via a per-entitlement rate override set between
    cycles -- rare, but not impossible), there is no single meaningful
    "whole" to reconcile against, so each is rounded independently instead;
    this is the one case where the parts may not sum to a single projected
    total, and it is a deliberate, documented choice, not an oversight.

    LOT RH12: when the type's quotite-proration policy is active for this
    month (`resolve_historical_policy`), the window is ALSO split at every
    genuine quotite change (`resolve_quotite_segments`) -- the quantity
    formula becomes rate x (sum of quotite_i x days_i) / days_in_month,
    computed as one exact Decimal sum before the single final quantize
    (never per-segment rounding). When inactive, every segment's quotite is
    exactly 1, making this bit-identical to the pre-RH12 formula. A missing
    `StaffQuotiteHistory` row covering the window's start raises
    `LeaveAccrualQuotiteError` BEFORE any entitlement or credit is touched
    -- never a silent 100% fallback.

    LOT RH13: an APPROVED, non-WFH absence whose own leave type has a
    configured impact policy targeting THIS type further reduces each
    affected day/half-day's weight -- `weight = quotite x (1 - reduction x
    absence_fraction)`, resolved per day/half (see resolve_absence_impact_
    segments), multiplicatively combined with quotite on the SAME day,
    never summed/subtracted twice. Only PENDING-at-credit-time absences are
    ignored (nothing yet to apply); a later decision never rewrites this
    already-posted credit -- see services.leave_absence_signal for the
    late-decision signal this raises instead. A credit of exactly 0 (e.g. a
    full suspension) is still created and auditable, never confused with a
    month genuinely not yet processed.

    Returns a list of (entitlement, credit, created: bool) tuples -- empty if
    the employee had no presence at all this month (see
    compute_presence_window).
    """
    window = compute_presence_window(staff, leave_type, month_start, month_end)
    if window is None:
        return []
    start, end = window
    days_in_month = (month_end - month_start).days + 1

    results = []
    with transaction.atomic():
        Staff.objects.select_for_update().get(pk=staff.pk)

        # LOT RH12: resolved under the SAME lock, BEFORE any entitlement or
        # credit is touched, so a missing-coverage refusal never leaves a
        # partial credit behind -- and so a concurrent
        # create_quotite_history_entry (which takes this identical lock)
        # can never race this read. `policy_active` is resolved once per
        # (leave_type, month), same convention as the rate itself.
        policy_active = resolve_historical_policy(leave_type, month_end)
        if policy_active:
            window_segments = resolve_quotite_segments(staff, start, end)
        else:
            window_segments = [(start, end, Decimal("1"))]

        # LOT RH13: resolved under the SAME lock, BEFORE any entitlement or
        # credit is touched -- a genuine same-portion conflict blocks the
        # whole window, never a partial credit. Empty day_map (no approved,
        # non-WFH absence configured to impact this target type) is the
        # overwhelmingly common case and keeps the EXACT pre-RH13 fast path
        # (see compute_weighted_presence_days).
        day_map, absence_contributing = resolve_absence_impact_segments(
            staff, leave_type, start, end, company_profile_id=staff.user.company_id,
        )
        absence_policy_applied = bool(day_map)

        sub_periods = []
        for period in resolve_cycle_periods_for_range(leave_type, start, end):
            sub_start = max(start, period.period_start)
            sub_end = min(end, period.period_end)
            if sub_start > sub_end:
                continue
            sub_periods.append((period, sub_start, sub_end))
        if not sub_periods:
            return []

        resolved = []
        for period, sub_start, sub_end in sub_periods:
            entitlement, _ = LeaveEntitlement.objects.get_or_create(
                employee=staff, leave_type=leave_type, cycle_period=period,
                defaults={
                    "mode": LeaveEntitlement.MODE_MONTHLY_ACCRUAL,
                    "quota": Decimal("0"),
                    "last_adjustment_reason": "",
                },
            )
            if entitlement.mode != LeaveEntitlement.MODE_MONTHLY_ACCRUAL:
                raise LeaveAccrualError(
                    f"Entitlement {entitlement.pk} for employee {staff.pk}/'{leave_type.name}'/"
                    f"{period.period_start}-{period.period_end} is in quota mode -- an existing "
                    f"quota counter is never auto-converted to monthly accrual."
                )
            # LOT RH11: a retroactive credit is never written into an
            # already-closed cycle -- local import avoids a circular
            # dependency (leave_carryover.py imports FROM this module).
            from human_resource_management.services.leave_carryover import (
                LeaveCarryOverError, raise_if_cycle_closed,
            )
            try:
                raise_if_cycle_closed(entitlement)
            except LeaveCarryOverError as exc:
                raise LeaveAccrualError(str(exc)) from exc
            # LOT RH10F: the per-employee override, like the type's own rate,
            # is resolved HISTORICALLY by this month's closing date -- a
            # later `adjust` changing the override must never be applied
            # back onto an earlier, not-yet-credited month (see
            # resolve_historical_override). None means "no override was in
            # force at that point" -- falls through to the type's own
            # historically-resolved rate, same as always.
            historical_override = resolve_historical_override(entitlement, month_end)
            if historical_override is not None:
                rate = historical_override
            else:
                rate = resolve_historical_rate(leave_type, month_end)
            resolved.append((entitlement, sub_start, sub_end, Decimal(rate)))

        rates_used = {r for *_, r in resolved}
        single_rate = len(rates_used) == 1
        window_total = None
        if single_rate:
            # LOT RH12/RH13: `compute_weighted_presence_days` replaces the
            # plain calendar day count here -- when neither the quotite
            # policy nor any absence impact applies, this is bit-identical
            # to the pre-RH12 `window_days` sum (no regression).
            weighted_window_days = compute_weighted_presence_days(
                window_segments, day_map, start, end, weighted_days_fn=_weighted_days,
            )
            window_total = (
                resolved[0][3] * weighted_window_days / Decimal(days_in_month)
            ).quantize(QUANT, rounding=ROUND_HALF_UP)

        running_sum = Decimal("0")
        last_idx = len(resolved) - 1
        for idx, (entitlement, sub_start, sub_end, rate) in enumerate(resolved):
            sub_days = (sub_end - sub_start).days + 1
            # Audit-only: independent of, and never used to derive, `quantity` below.
            fraction = (Decimal(sub_days) / Decimal(days_in_month)).quantize(
                QUANT, rounding=ROUND_HALF_UP
            )
            # LOT RH12: this sub-period's own slice of the window's quotite
            # segments -- the real sequence of changes within it (never
            # collapsed into one averaged quotite), stored verbatim below for
            # traceability. Weighted days used for the quantity itself.
            sub_segments = [
                (max(seg_start, sub_start), min(seg_end, sub_end), quotite)
                for seg_start, seg_end, quotite in window_segments
                if seg_start <= sub_end and seg_end >= sub_start
            ]
            sub_weighted_days = compute_weighted_presence_days(
                window_segments, day_map, sub_start, sub_end, weighted_days_fn=_weighted_days,
            )

            existing = LeaveAccrualCredit.objects.filter(
                entitlement=entitlement, period_start=sub_start, period_end=sub_end,
            ).first()
            if existing is not None:
                results.append((entitlement, existing, False))
                running_sum += existing.quantity
                continue

            if single_rate and idx == last_idx:
                quantity = window_total - running_sum
            else:
                quantity = (rate * sub_weighted_days / Decimal(days_in_month)).quantize(
                    QUANT, rounding=ROUND_HALF_UP
                )

            quotite_breakdown = (
                [
                    {
                        "start": seg_start.isoformat(),
                        "end": seg_end.isoformat(),
                        "quotite": str(quotite),
                    }
                    for seg_start, seg_end, quotite in sub_segments
                ]
                if policy_active else None
            )
            absence_breakdown = (
                build_absence_impact_breakdown(absence_contributing, sub_start, sub_end)
                if day_map else None
            )

            try:
                with transaction.atomic():  # savepoint: an IntegrityError here must
                    credit = LeaveAccrualCredit.objects.create(  # not poison the outer,
                        entitlement=entitlement, period_start=sub_start, period_end=sub_end,
                        rate_applied=rate, proration_fraction=fraction, quantity=quantity,
                        created_by=created_by,
                        quotite_policy_active=policy_active,
                        quotite_breakdown=quotite_breakdown,
                        absence_impact_applied=absence_policy_applied,
                        absence_impact_breakdown=absence_breakdown,
                    )
                results.append((entitlement, credit, True))
                running_sum += quantity
            except IntegrityError:
                # Genuine concurrent race (two callers past the pre-check above at
                # once) -- the Staff lock already prevents this for the same
                # employee; this is the final backstop, not the primary path.
                existing = LeaveAccrualCredit.objects.get(
                    entitlement=entitlement, period_start=sub_start, period_end=sub_end,
                )
                results.append((entitlement, existing, False))
                running_sum += existing.quantity
    return results


def eligible_leave_types(company_profile_id=None):
    """Company-scope LeaveType rows configured for monthly accrual (an
    effective rate is set) that still consume balance, for a company that
    has EXPLICITLY activated the entitlement mechanism (LOT RH10 follow-up:
    "activation explicite" -- crediting a type in a non-activated company
    would itself be an implicit activation by another name, so the batch job
    honours the exact same `leave_entitlement_mode_active` gate as the
    request-time checks in `serializers.leave._check_balance_or_raise`).
    Pulling the (small, per-company) candidate set into Python to call
    effective_accrual_rate() keeps the override/inheritance resolution in
    one place rather than duplicating it as a query -- acceptable for a
    batch job, not a request-path query."""
    queryset = LeaveType.objects.filter(
        company_config__isnull=False,
        company_config__leave_entitlement_mode_active=True,
    ).select_related("company_config", "inherits_from", "inherits_from__global_config")
    if company_profile_id is not None:
        queryset = queryset.filter(company_config__company_profile_id=company_profile_id)
    return [
        leave_type for leave_type in queryset
        if leave_type.effective_accrual_rate() is not None
        and leave_type.effective_value("consumes_balance")
    ]


@dataclass
class AccrualRunReport:
    processed: int = 0
    credited: int = 0
    already_done: int = 0
    skipped: list = field(default_factory=list)
    errors: list = field(default_factory=list)


def run_monthly_accrual(*, company_profile_id=None, as_of=None, dry_run=False, actor=None):
    """Top-level batch entry point (see management command
    run_monthly_leave_accrual). Catches up every month from a leave type's
    accrual_effective_date through the last fully-completed LOCAL month --
    local to that company's own `accrual_timezone`, resolved once per
    CompanyConfig and cached for the run, since different companies may
    have different zones (and different "last complete month" boundaries as
    a result). A company with no/invalid timezone configured is reported as
    blocked (once per company, not once per type) and every one of its
    eligible types is skipped for this run -- never defaulted to UTC. Per
    (employee, leave_type, month) isolation otherwise, so one employee's
    missing configuration or one month's error never aborts the rest of the
    run. No PII beyond numeric employee/leave type ids is ever placed in the
    report (no names/emails).

    LOT RH10F: the entire run is guarded by a Postgres session-level advisory
    lock (see LeaveAccrualConcurrentRunError) so two overlapping invocations
    (a manual run started while the scheduled one is still going, or two
    scheduler ticks firing back to back) can never double-process the same
    slot -- raises immediately, before touching anything, if another run
    already holds it."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [_ACCRUAL_RUN_LOCK_KEY])
        acquired = cursor.fetchone()[0]
    if not acquired:
        logger.warning(
            "run_monthly_accrual: refused -- another run already holds the advisory lock."
        )
        raise LeaveAccrualConcurrentRunError(
            "A monthly accrual run is already in progress on this database -- "
            "refusing to start a second, concurrent run."
        )
    try:
        report = _run_monthly_accrual_locked(
            company_profile_id=company_profile_id, as_of=as_of, dry_run=dry_run, actor=actor,
        )
        logger.info(
            "run_monthly_accrual: processed=%s credited=%s already_done=%s "
            "skipped=%s errors=%s",
            report.processed, report.credited, report.already_done,
            len(report.skipped), len(report.errors),
        )
        for staff_id, leave_type_id, month_label, message in report.errors:
            logger.error(
                "run_monthly_accrual error: staff=%s leave_type=%s month=%s: %s",
                staff_id, leave_type_id, month_label, message,
            )
        return report
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [_ACCRUAL_RUN_LOCK_KEY])


def _run_monthly_accrual_locked(*, company_profile_id=None, as_of=None, dry_run=False, actor=None):
    report = AccrualRunReport()
    timezone_cache = {}  # company_config_id -> ZoneInfo | LeaveAccrualTimezoneError
    blocked_company_configs = set()

    for leave_type in eligible_leave_types(company_profile_id):
        company_config = leave_type.company_config
        if company_config.id not in timezone_cache:
            try:
                timezone_cache[company_config.id] = resolve_company_timezone(company_config)
            except LeaveAccrualTimezoneError as exc:
                timezone_cache[company_config.id] = exc
        resolved_timezone = timezone_cache[company_config.id]
        if isinstance(resolved_timezone, LeaveAccrualTimezoneError):
            if company_config.id not in blocked_company_configs:
                report.errors.append((None, None, None, str(resolved_timezone)))
                blocked_company_configs.add(company_config.id)
            continue

        last_year, last_month = last_complete_local_month(resolved_timezone, as_of)

        effective_date = leave_type.effective_accrual_effective_date()
        if effective_date is None:
            report.skipped.append(
                (None, leave_type.id, None, "no accrual_effective_date configured")
            )
            continue
        if (effective_date.year, effective_date.month) > (last_year, last_month):
            continue  # nothing has closed yet since the effective date

        staff_qs = Staff.objects.filter(user__company_id=leave_type.company_config.company_profile_id)
        for staff in staff_qs:
            if staff.contract_start_date is None:
                report.skipped.append(
                    (staff.id, leave_type.id, None, "no contract_start_date configured")
                )
                continue

            for year, month in _iter_months(
                effective_date.year, effective_date.month, last_year, last_month
            ):
                month_start, month_end = _month_bounds(year, month)
                report.processed += 1
                if dry_run:
                    continue
                month_label = f"{year}-{month:02d}"
                try:
                    results = credit_month(staff, leave_type, month_start, month_end, created_by=actor)
                except LeaveAccrualError as exc:
                    report.errors.append((staff.id, leave_type.id, month_label, str(exc)))
                    continue
                if not results:
                    report.skipped.append(
                        (staff.id, leave_type.id, month_label, "no presence this month")
                    )
                    continue
                for _entitlement, _credit, created in results:
                    if created:
                        report.credited += 1
                    else:
                        report.already_done += 1
    return report
