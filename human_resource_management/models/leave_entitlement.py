"""LOT RH 9: per-employee/per-leave-type/per-cycle entitlements ("droits"),
replacing the historical global `Staff.annual_leaves_allowed`/
`wfh_leaves_allowed` balance -- but only for a company/solution that has
explicitly opted in (`CompanyConfig.leave_entitlement_mode_active`). Two
models:

  - `LeaveCyclePeriod`: one concrete [period_start, period_end] instance of
    a `LeaveType`'s recurring annual cycle (e.g. "2026-06-01..2027-05-31"
    for a June-May cycle). Rows are created lazily, on first need, by
    `services.leave_entitlement.resolve_cycle_period` -- never user-editable
    directly, so two rows for the same LeaveType can never overlap (the
    resolver always computes the exact same [start, end] for a given
    (LeaveType, reference date), so concurrent resolution races only ever
    retry into the SAME row via the unique constraint below, they never
    create a conflicting one). If a LeaveType's configured cycle dates
    change later, already-resolved periods are never moved or regenerated --
    new resolutions simply compute a different period and create a new row.
  - `LeaveEntitlement`: the actual quota row for one (employee, leave_type,
    cycle_period). `quota` is the CURRENT total (the initial attribution and
    every subsequent adjustment are all just updates to this same field);
    `simple_history` keeps every prior value, its author and its date, so an
    adjustment is never a silent overwrite -- `last_adjustment_reason` is a
    real field on the row (not a separate ledger model), so it is captured
    in every historical snapshot right alongside the `quota` value it
    explains.

LOT RH 10 adds the second mode anticipated above: `MODE_MONTHLY_ACCRUAL`. For
a monthly-accrual entitlement, `quota` is repurposed to mean the CURRENT NET
MANUAL ADJUSTMENT total (defaults to 0.00, RH-adjustable via the same
`adjust` endpoint/lock/negative-balance-guard as quota mode) -- it is never
an absolute cap for this mode. The actual "acquired" balance is the sum of
that entitlement's `LeaveAccrualCredit` rows (see below), and
`available = acquired + quota(=adjustment) - consumed - reserved`, computed
in `services.leave_entitlement`. Reusing `quota` this way avoids a schema
change and keeps the existing adjust lock/guard/history machinery working
identically for both modes -- see that field's per-mode docstring below.
"""
from datetime import date

from django.db import models
from simple_history.models import HistoricalRecords

from models.models import LeaveType, Staff, User

from human_resource_management.models.leave import LeaveRequest

QUANTITY_FIELD_KWARGS = {"max_digits": 12, "decimal_places": 6}


class LeaveCyclePeriod(models.Model):
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="cycle_periods"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["leave_type", "period_start", "period_end"],
                name="unique_leave_cycle_period",
            ),
        ]
        ordering = ["leave_type_id", "period_start"]

    def __str__(self):
        return f"{self.leave_type_id}: {self.period_start}..{self.period_end}"


class LeaveEntitlement(models.Model):
    MODE_QUOTA = "quota"
    MODE_MONTHLY_ACCRUAL = "monthly_accrual"
    MODE_CHOICES = [
        (MODE_QUOTA, "Quota at cycle start"),
        (MODE_MONTHLY_ACCRUAL, "Monthly accrual"),
    ]

    employee = models.ForeignKey(
        Staff, on_delete=models.CASCADE, related_name="leave_entitlements"
    )
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.PROTECT, related_name="entitlements"
    )
    cycle_period = models.ForeignKey(
        LeaveCyclePeriod, on_delete=models.PROTECT, related_name="entitlements"
    )
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_QUOTA)
    # Quota mode: the absolute current quota. Monthly-accrual mode: the net manual
    # adjustment total (see module docstring) -- same field, different meaning per mode.
    quota = models.DecimalField(max_digits=6, decimal_places=2)
    last_adjustment_reason = models.TextField()
    last_adjusted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    # Monthly-accrual mode only: an RH override of the leave type's effective monthly
    # rate for this one employee ("surcharge individuelle RH"). None = use
    # leave_type.effective_accrual_rate(). Ignored for quota-mode rows.
    accrual_rate_override = models.DecimalField(
        max_digits=8, decimal_places=4, null=True, blank=True
    )
    # LOT RH10F closure: the BUSINESS date this override takes effect --
    # deliberately a SEPARATE, genuine field from `history`'s own
    # `history_date` (which is `simple_history`'s real audit timestamp: when
    # THIS row was actually saved, by whom -- `last_adjusted_by` -- never to
    # be repurposed as a business date, or the audit trail itself becomes
    # unreliable). None means "in effect immediately/unconditionally" (the
    # entitlement's very first, creation-time override); a set date means
    # "not in effect before this date", validated to the 1st of a month by
    # the `adjust` view, same rule as LeaveTypeAccrualRateHistory.
    # `simple_history` mirrors this field into every historical snapshot
    # exactly like any other model field, so `resolve_historical_override`
    # can resolve "what override was ACTUALLY in effect as of a given past
    # date" from genuine business dates, never from when someone happened
    # to click save.
    accrual_override_effective_from = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["employee", "leave_type", "cycle_period"],
                name="unique_leave_entitlement_per_employee_type_cycle",
            ),
        ]

    def __str__(self):
        return f"{self.employee_id}/{self.leave_type_id}/{self.cycle_period_id}: {self.quota}"

    def effective_accrual_rate(self):
        """LOT RH10F closure: the "current" rate shown to RH must itself
        respect the override's effective date -- reading the live
        `accrual_rate_override` field directly would show a value that's
        been SAVED but not yet actually in force (e.g. scheduled for next
        month), or miss an OLDER override that's still the one genuinely
        active today while a future change is queued. Delegates to the
        exact same historical resolution `credit_month` itself uses, as of
        today, so "what rate would a catch-up apply right now" and "what
        rate does RH see as current" can never silently disagree."""
        from human_resource_management.services.leave_accrual import (
            resolve_historical_override,
        )
        override = resolve_historical_override(self, date.today())
        if override is not None:
            return override
        return self.leave_type.effective_accrual_rate()


class LeaveAccrualCredit(models.Model):
    """One immutable, traced monthly credit against a monthly-accrual
    `LeaveEntitlement`. Rows are created exclusively by
    `services.leave_accrual.credit_month` under the same
    `Staff.objects.select_for_update()` lock used for reservations/approvals/
    adjustments, and are never edited after creation -- a rate change on the
    LeaveType/LeaveEntitlement only affects credits computed AFTER the change;
    existing rows keep the `rate_applied` that was actually used at the time.

    The unique constraint on (entitlement, period_start, period_end) is the
    hard guarantee that "the same acquisition period can never be credited
    twice", even under concurrent/retried processing -- combined with the
    Staff-level lock, a retry after a partial failure simply hits this
    constraint and is treated as already-done (see credit_month).
    """

    entitlement = models.ForeignKey(
        LeaveEntitlement, on_delete=models.CASCADE, related_name="accrual_credits"
    )
    period_start = models.DateField()
    period_end = models.DateField()
    # Precision: DecimalField(12, 6) for rate/fraction/quantity. The fraction
    # (days-present / days-in-month) is generally not decimal-exact (e.g. 15/31), so
    # storing it is a documented, deliberate rounding point: credit_month() rounds the
    # fraction and the final quantity ONCE, deterministically (ROUND_HALF_UP to 6
    # decimal places), from the exact rate and day counts -- never from an
    # already-rounded intermediate value, and never re-derived or re-rounded for
    # display (display-only rounding happens in the serializer/UI layer).
    rate_applied = models.DecimalField(max_digits=12, decimal_places=6)
    proration_fraction = models.DecimalField(max_digits=12, decimal_places=6)
    quantity = models.DecimalField(max_digits=12, decimal_places=6)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        help_text="Null for system/batch-generated credits.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # LOT RH12: minimal extension, both nullable so every credit posted
    # BEFORE this lot keeps displaying exactly as before (null = "quotite
    # proration did not exist yet for this row", never confused with
    # "policy was inactive" -- see serialize_accrual_credit). For a credit
    # posted by this lot's code, `quotite_policy_active` is always set
    # (True or False, never null), and `quotite_breakdown` is populated only
    # when the policy was active for that credit -- a list of this row's own
    # [start, end] sub-periods (cycle-clipped) with the quotite applied to
    # each, so an intra-month quotite change is traceable as the real
    # sequence of segments, never collapsed into one averaged figure.
    quotite_policy_active = models.BooleanField(null=True, blank=True)
    quotite_breakdown = models.JSONField(null=True, blank=True)
    # LOT RH13: same minimal-extension convention -- both nullable so every
    # credit posted before this lot keeps displaying exactly as before.
    # `absence_impact_applied` is True/False (never null) for a credit
    # posted by this lot's code; `absence_impact_breakdown` is a list of
    # plain dicts, one per contributing APPROVED absence, each carrying a
    # DURABLE snapshot (request id, source type id/name, effect, factor,
    # the portion's own dates/half-day flag, and its status AT CREDIT TIME)
    # -- never a live FK to LeaveRequest, so a later physical deletion of
    # that (by-then-always-approved-only-contributes, so never actually
    # deletable) request can never cascade into or silently blank this
    # audit trail, same precedent as LeaveCarryOverAllocation.original_
    # request_id.
    absence_impact_applied = models.BooleanField(null=True, blank=True)
    absence_impact_breakdown = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["entitlement", "period_start", "period_end"],
                name="unique_accrual_credit_per_entitlement_period",
            ),
        ]
        ordering = ["entitlement_id", "period_start"]

    def __str__(self):
        return f"{self.entitlement_id}: {self.period_start}..{self.period_end} = {self.quantity}"


class LeaveTypeAccrualRateHistory(models.Model):
    """LOT RH10 follow-up: one (rate, effective_from) record every time a
    LeaveType's `monthly_accrual_rate` is set or changed. `LeaveType` itself
    carries no history (no `simple_history` on it) -- without this, crediting
    a NOT-YET-processed past month during a delayed catch-up would silently
    read today's CURRENT rate, which may not be the rate that was actually in
    effect while that month elapsed (the old value is simply gone once the
    field is overwritten). `credit_month` resolves the rate for a given month
    by the LATEST row whose `effective_from` is on/before that month's LAST
    day -- a deliberate, documented choice: crediting itself never happens
    before month-end closure, so "the rate in effect at the close of the
    month" is the one coherent, always-resolvable rule, without inventing a
    separate within-month-split-by-rate-change behaviour. A month with NO
    resolvable row (effective_from <= month_end) is blocked, never guessed.
    Already-posted `LeaveAccrualCredit` rows are never rewritten by a later
    change here -- each one already carries its own `rate_applied`."""

    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="accrual_rate_history"
    )
    rate = models.DecimalField(max_digits=8, decimal_places=4)
    effective_from = models.DateField()
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Null for a system-recorded change (e.g. a data migration).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["leave_type_id", "effective_from", "created_at"]

    def __str__(self):
        return f"{self.leave_type_id}: {self.rate} from {self.effective_from}"


class LeaveCycleClosure(models.Model):
    """LOT RH11: one immutable row per entitlement marking that its cycle's
    closure has been processed -- created UNCONDITIONALLY by
    `services.leave_carryover.close_cycle`, even when nothing is carried
    over (carry-over disabled for the type, or the eligible balance is
    zero), so idempotence never depends on a `LeaveCarryOver` row existing.
    Never rewritten afterward -- a later change to the type's carry-over
    config never revisits an already-closed cycle (same "never rewrite
    history" precedent as `LeaveAccrualCredit`/`LeaveTypeAccrualRateHistory`).

    `eligible_balance` is this entitlement's OWN balance at closure (already
    excluding any `LeaveCarryOver` carried IN from a prior cycle -- a
    carry-over is never itself re-carried, see module-level docstring in
    services.leave_carryover). `cap_applied`/`expiration_months_applied`
    mirror the type's carry-over config AS IT WAS at this exact closure
    (None/None when carry-over was disabled for the type at that moment) --
    never re-derived from the type's current config on read.
    """

    entitlement = models.OneToOneField(
        LeaveEntitlement, on_delete=models.PROTECT, related_name="closure"
    )
    eligible_balance = models.DecimalField(**QUANTITY_FIELD_KWARGS)
    capped_quantity = models.DecimalField(**QUANTITY_FIELD_KWARGS, default=0)
    carried_over_quantity = models.DecimalField(**QUANTITY_FIELD_KWARGS, default=0)
    cap_applied = models.DecimalField(**QUANTITY_FIELD_KWARGS, null=True, blank=True)
    expiration_months_applied = models.PositiveIntegerField(null=True, blank=True)
    closed_at = models.DateTimeField(auto_now_add=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"closure({self.entitlement_id}): eligible={self.eligible_balance} carried={self.carried_over_quantity}"


class LeaveCarryOver(models.Model):
    """LOT RH11: immutable -- the amount carried from one entitlement's
    cycle into the entitlement of the IMMEDIATELY FOLLOWING cycle
    (`destination_entitlement`, lazily resolved/created exactly like any
    other entitlement via `resolve_cycle_period`). One row per
    `LeaveCycleClosure` (never more than one source cycle feeds a given
    closure). `expires_on` is computed and frozen here at creation time --
    a later change to the type's `carry_over_expiration_months` never
    revises an existing row."""

    source_closure = models.OneToOneField(
        LeaveCycleClosure, on_delete=models.PROTECT, related_name="carry_over"
    )
    destination_entitlement = models.ForeignKey(
        LeaveEntitlement, on_delete=models.PROTECT, related_name="carry_overs_in"
    )
    quantity = models.DecimalField(**QUANTITY_FIELD_KWARGS)
    expires_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"carryover->{self.destination_entitlement_id}: {self.quantity} expires {self.expires_on}"


class LeaveExpiration(models.Model):
    """LOT RH11: immutable -- the final accounting of one `LeaveCarryOver`
    once its `expires_on` has passed. Created exactly once, by
    `services.leave_carryover.finalize_due_carryovers`, never recomputed
    afterward (a later cancellation of a request counted in
    `LeaveCarryOverAllocation` never changes `quantity_expired`)."""

    carry_over = models.OneToOneField(
        LeaveCarryOver, on_delete=models.PROTECT, related_name="expiration"
    )
    quantity_expired = models.DecimalField(**QUANTITY_FIELD_KWARGS)
    expired_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"expiration({self.carry_over_id}): {self.quantity_expired}"


class LeaveCarryOverAllocation(models.Model):
    """LOT RH11: immutable -- how much of ONE `LeaveCarryOver` was imputed
    to ONE specific `LeaveRequest`, frozen at the exact moment that
    carry-over expired (`finalize_due_carryovers`, deterministic walk by
    request id ascending). Never revised while the request survives, even
    across a (non-balance-field) edit or an approval -- this is the
    historical truth of what was allocated at expiration time, independent
    of what happens to the request afterward.

    `leave_request` is SET_NULL (not PROTECT, not CASCADE): cancellation
    physically deletes a pending LeaveRequest (see
    views.leave.LeaveRequestViewSet.destroy), and a request with a frozen
    allocation must still be cancellable -- but the allocation ROW itself
    is the audit trail and must survive that deletion, so it is never
    dropped along with it. `original_request_id` is set once at creation
    (mirrors `leave_request_id` at that moment) and never changes -- a
    durable identification of which request this was, even once
    `leave_request` itself is nulled out. Deleting a request this way
    never touches `LeaveExpiration.quantity_expired` (a separate,
    already-frozen aggregate) or any other request's own allocation row."""

    carry_over = models.ForeignKey(
        LeaveCarryOver, on_delete=models.PROTECT, related_name="allocations"
    )
    leave_request = models.ForeignKey(
        LeaveRequest, on_delete=models.SET_NULL, null=True,
        related_name="carry_over_allocations",
    )
    original_request_id = models.PositiveIntegerField()
    quantity_allocated = models.DecimalField(**QUANTITY_FIELD_KWARGS)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["carry_over", "leave_request"],
                name="unique_carryover_allocation_per_request",
            ),
        ]

    def __str__(self):
        return f"allocation({self.carry_over_id}->{self.leave_request_id}): {self.quantity_allocated}"


class LeaveTypeQuotiteProrationHistory(models.Model):
    """LOT RH12: one (is_active, effective_from) record every time a
    LeaveType's `prorate_accrual_by_quotite` policy is set or changed --
    same convention as `LeaveTypeAccrualRateHistory` for monthly_accrual_rate
    (RH10 follow-up), reused unchanged. Without this, crediting a
    not-yet-processed past month during a delayed catch-up would silently
    read today's CURRENT policy value, which may not be the one actually in
    force while that month elapsed. `resolve_historical_policy` resolves the
    policy for a given month by the LATEST row whose `effective_from` is
    on/before that month's LAST day -- never before month-end closure, same
    rule as the rate history. A month with NO resolvable row simply means
    "policy was never activated as of that point" -- False, a safe default,
    unlike the rate (there is no safe default rate to fall back on)."""

    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="quotite_proration_history"
    )
    is_active = models.BooleanField()
    effective_from = models.DateField()
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Null for a system-recorded change (e.g. a data migration).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["leave_type_id", "effective_from", "created_at"]

    def __str__(self):
        return f"{self.leave_type_id}: prorate={self.is_active} from {self.effective_from}"


class StaffQuotiteHistory(models.Model):
    """LOT RH12: the minimal, dedicated contractual source of truth for an
    employee's quotite (part-time ratio) -- NOT a full staff-record history
    (weekly_hours/contract_start_date/contract_end_date stay as plain,
    unhistorized Staff fields; this lot only historizes quotite itself).

    `quotite` is ALWAYS derived server-side from `contractual_hours` /
    `full_time_reference_hours` at creation time (see
    services.leave_accrual.create_quotite_history_entry) -- never accepted
    as independent client input, so the two can never silently disagree.
    `full_time_reference_hours` is captured on EVERY row (not just read live
    from CompanyConfig/StaffConfiguration at use time): the company's
    reference may itself change later, and a past row must stay interpretable
    exactly as it was confirmed, never reinterpreted against a new baseline.

    `effective_from` is a genuine BUSINESS date (when this contractual
    situation actually starts applying), deliberately distinct from
    `created_at` (the real audit timestamp of the entry itself, e.g. a
    change entered today but scheduled to take effect next month). Rows are
    never edited or deleted by this lot -- a correction is a new row with
    its own effective_from, same "never rewrite history" precedent as
    LeaveAccrualCredit/LeaveTypeAccrualRateHistory. `resolve_quotite_segments`
    (services.leave_accrual) is the one place these rows are read back for
    computation."""

    staff = models.ForeignKey(
        Staff, on_delete=models.CASCADE, related_name="quotite_history"
    )
    contractual_hours = models.DecimalField(max_digits=5, decimal_places=2)
    full_time_reference_hours = models.DecimalField(max_digits=5, decimal_places=2)
    quotite = models.DecimalField(max_digits=9, decimal_places=6)
    effective_from = models.DateField()
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Null for a system-recorded change (e.g. a data migration).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_id", "effective_from", "created_at"]
        verbose_name_plural = "Staff quotite histories"

    def __str__(self):
        return f"{self.staff_id}: {self.quotite} from {self.effective_from}"


class LeaveTypeAbsenceImpactHistory(models.Model):
    """LOT RH13: the explicit, historized policy linking a SOURCE absence
    leave type to a TARGET leave type's monthly accrual -- "aucune politique
    configuree = aucun effet" is the hard default (no row = EFFECT_NONE for
    any day, never guessed from either type's name/label).

    `effect`:
      - NONE: no impact at all (the structural default, same as no row).
      - SUSPEND: accrual nulled out ONLY for the exact day(s)/half-day(s)
        the source absence actually covers -- never the whole month/cycle
        by default (see services.leave_absence_impact). Mathematically
        identical to REDUCE at reduction_factor=1, kept as its own explicit
        choice purely for RH's own audit clarity of INTENT.
      - REDUCE: `reduction_factor` (required, 0-1 inclusive) is the
        fraction of each covered day/half-day treated as NOT present for
        accrual purposes (0.25 = 25% reduction, matching the business
        spec's own convention).

    Resolved PER DAY/HALF-DAY of the absence window (never once at
    month-end like the rate/quotite-policy booleans above) -- see
    `resolve_absence_impact_segments`, because the factor itself (not just
    whether the pair is configured at all) can validly change mid-absence.
    A CHANGE is never implicitly backdated (no accrual_effective_date-style
    anchor exists for a relationship between two already-existing types,
    unlike a type's own first rate/policy) -- always defaults to the 1st of
    NEXT month, unless RH explicitly supplies an earlier `effective_from`.
    Never deleted on "deactivation" -- a deactivation is simply a NEW row
    with effect=NONE and its own effective_from, same "never rewrite
    history" precedent as every other *History model here."""

    EFFECT_NONE = "none"
    EFFECT_SUSPEND = "suspend"
    EFFECT_REDUCE = "reduce"
    EFFECT_CHOICES = [
        (EFFECT_NONE, "No effect"),
        (EFFECT_SUSPEND, "Suspend accrual for the absence's own days"),
        (EFFECT_REDUCE, "Reduce accrual by an explicit factor"),
    ]

    source_leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="absence_impact_as_source"
    )
    target_leave_type = models.ForeignKey(
        LeaveType, on_delete=models.CASCADE, related_name="absence_impact_as_target"
    )
    effect = models.CharField(max_length=10, choices=EFFECT_CHOICES)
    reduction_factor = models.DecimalField(
        max_digits=7, decimal_places=6, null=True, blank=True,
        help_text="Required (0-1) for REDUCE only; must be null for NONE/SUSPEND.",
    )
    effective_from = models.DateField()
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        help_text="Null for a system-recorded change (e.g. a data migration).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source_leave_type", "target_leave_type", "effective_from"],
                name="unique_absence_impact_per_pair_per_effective_date",
            ),
        ]
        ordering = ["source_leave_type_id", "target_leave_type_id", "effective_from", "created_at"]

    def __str__(self):
        return f"{self.source_leave_type_id}->{self.target_leave_type_id}: {self.effect} from {self.effective_from}"


class LeaveAccrualAbsenceSignal(models.Model):
    """LOT RH13: a durable, visible-to-RH flag that an already-POSTED
    `LeaveAccrualCredit` would compute DIFFERENTLY under the CURRENT state
    of absence decisions/policy -- created by `services.leave_absence_
    signal.check_late_decision_signal`, called from the leave request
    approve/reject (LeaveRequestSerializer.update) and delete (LeaveRequest
    ViewSet.destroy) paths. NEVER triggers an automatic recompute or
    balance adjustment -- "jamais recalcul automatique" is absolute; RH
    reviews each signal and, if the entitlement's cycle is still open, uses
    the PRE-EXISTING manual adjustment mechanism (LeaveEntitlement.quota in
    monthly-accrual mode) with its own required justification -- applying
    that adjustment is a SEPARATE, explicit RH action from marking this
    signal resolved (`resolved_at`/`resolved_by`/`resolution_note` are only
    ever set by a dedicated, explicit endpoint, never implicitly).

    Deliberately NOT a FK to `LeaveRequest` for the trigger request --
    `trigger_leave_request_id` is a durable plain id (same precedent as
    LeaveCarryOverAllocation.original_request_id): the triggering request
    may itself later be deleted (e.g. a once-pending request now rejected
    and cleared), and this signal's own trace must survive that."""

    TRIGGER_APPROVED = "approved"
    TRIGGER_REJECTED = "rejected"
    TRIGGER_DELETED = "deleted"
    TRIGGER_CHOICES = [
        (TRIGGER_APPROVED, "Leave request approved"),
        (TRIGGER_REJECTED, "Leave request rejected"),
        (TRIGGER_DELETED, "Leave request deleted (was pending)"),
    ]

    entitlement = models.ForeignKey(
        LeaveEntitlement, on_delete=models.CASCADE, related_name="absence_signals"
    )
    credit = models.ForeignKey(
        LeaveAccrualCredit, on_delete=models.CASCADE, related_name="absence_signals", null=True, blank=True,
    )
    trigger_event = models.CharField(max_length=10, choices=TRIGGER_CHOICES)
    trigger_leave_request_id = models.PositiveIntegerField()
    reason = models.TextField()
    cycle_closed = models.BooleanField(
        default=False,
        help_text="Whether the affected entitlement's cycle was already closed when detected -- correction deferred.",
    )
    detected_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_absence_signals",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)
    # LOT RH13 follow-up: while this signal stays unresolved, a FURTHER
    # unrelated event touching the same stale credit must never spawn a
    # second row (see check_late_decision_signal's own dedup-by-credit
    # rule) -- but it must not vanish either. Each such event is appended
    # here (never replacing the original), so RH can see every contributing
    # event even though only one signal row ever exists per credit at a time.
    additional_events = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-detected_at"]

    def __str__(self):
        return f"signal({self.entitlement_id}/{self.credit_id}): {self.trigger_event} req={self.trigger_leave_request_id}"
