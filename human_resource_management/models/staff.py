from django.db import models
from model_utils import FieldTracker
from models.models import Staff, CompanyProfile, User

class StaffObservation(models.Model):
    company_profile = models.ForeignKey(CompanyProfile, related_name="staff_observations", on_delete=models.CASCADE)
    title = models.CharField(max_length=500)
    description = models.CharField(max_length=1000, null=True, blank=True)
    staff = models.ForeignKey(Staff, related_name="staff_observations", on_delete=models.CASCADE)

    created_by = models.ForeignKey(User, related_name="created_staff_observations", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()


class StaffContractAuditLog(models.Model):
    """AI-1685: append-only audit trail for changes to Staff.contract_type
    and Staff.contract_end_date -- who changed what, from what, to what, and
    when (the real save timestamp, never a business-effective date: contract
    type changes take effect immediately in this lot, no future scheduling).
    Written explicitly by StaffSerializer.update (the only reachable "normal
    API path" for these fields -- Staff creation itself is disabled in this
    API, see StaffViewSet.create). Never backfilled for changes that
    happened before this lot -- a gap here means "not tracked before AI-1685",
    never a fabricated past entry.

    L00-A: also reused as-is for Staff.salary/hourly_rate changes -- the
    same append-only "who/what field/when" shape applies identically;
    old_value/new_value are stored as the literal REDACTED_VALUE marker
    (never the real figure) for these two fields specifically, per the
    explicit instruction to never write compensation values to any log."""

    FIELD_CONTRACT_TYPE = "contract_type"
    FIELD_CONTRACT_END_DATE = "contract_end_date"
    FIELD_SALARY = "salary"
    FIELD_HOURLY_RATE = "hourly_rate"
    FIELD_CHOICES = (
        (FIELD_CONTRACT_TYPE, "Contract type"),
        (FIELD_CONTRACT_END_DATE, "Contract end date"),
        (FIELD_SALARY, "Salary"),
        (FIELD_HOURLY_RATE, "Hourly rate"),
    )
    REDACTED_VALUE = "[redacted]"

    staff = models.ForeignKey(Staff, related_name="contract_audit_log", on_delete=models.CASCADE)
    field = models.CharField(max_length=32, choices=FIELD_CHOICES)
    old_value = models.CharField(max_length=255, null=True, blank=True)
    new_value = models.CharField(max_length=255, null=True, blank=True)
    changed_by = models.ForeignKey(User, related_name="+", on_delete=models.SET_NULL, null=True, blank=True)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_id", "-changed_at"]

    def __str__(self):
        return f"{self.staff_id}: {self.field} {self.old_value!r} -> {self.new_value!r}"


class StaffCompensationHistory(models.Model):
    """L00-B3.1: the dedicated, append-only source of truth for an
    employee's compensation (gross monthly salary / hourly rate) over time
    -- same "never rewrite history, a correction is a new row" precedent as
    StaffQuotiteHistory (RH12). StaffContractAuditLog (above) remains a
    pure, redacted ACTION trace ("who changed what field when") -- it has
    never been able to reconstruct a historical salary, and still can't.
    THIS model is the one place the real historical figures live, gated by
    the exact same compensation permissions as the rest of L00-A
    (can_view_staff_compensation/can_update_staff_compensation).

    `gross_monthly_salary`/`hourly_rate` are independent, nullable Decimal
    fields (never Float -- unlike the legacy `Staff.salary`/`hourly_rate`
    mirror, see services.staff_compensation) -- a row may carry one, the
    other, or both, exactly like the live fields they historize.
    `currency` is snapshotted on every row (never read live from
    CompanyProfile at resolution time) so a past situation stays
    interpretable even if the company's display currency changes later.

    `basis` distinguishes a NEW, RH-confirmed gross monthly figure from a
    LEGACY value captured verbatim at data-takeover time whose brut/net
    nature is simply unknown -- never auto-requalified (see the 0028
    data migration). `event_type` + `supersedes` give an explicit, auditable
    link from a qualification/correction/cancellation back to the entry it
    concerns, without ever touching that original row -- resolution itself
    never looks at either field, only at `effective_from`/`created_at`
    (see services.staff_compensation.resolve_current_compensation)."""

    BASIS_CONFIRMED_GROSS = "confirmed_gross"
    BASIS_LEGACY_UNQUALIFIED = "legacy_unqualified"
    BASIS_CHOICES = (
        (BASIS_CONFIRMED_GROSS, "Confirmed gross monthly"),
        (BASIS_LEGACY_UNQUALIFIED, "Legacy amount, basis unknown"),
    )

    EVENT_INITIAL_CAPTURE = "initial_capture"
    EVENT_NEW_SITUATION = "new_situation"
    EVENT_QUALIFICATION = "qualification"
    EVENT_CORRECTION = "correction"
    EVENT_CANCELLATION = "cancellation"
    EVENT_CHOICES = (
        (EVENT_INITIAL_CAPTURE, "Initial capture (data takeover)"),
        (EVENT_NEW_SITUATION, "New situation"),
        (EVENT_QUALIFICATION, "Qualification of a captured amount"),
        (EVENT_CORRECTION, "Retroactive correction"),
        (EVENT_CANCELLATION, "Cancellation of a scheduled change"),
    )

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="compensation_history")
    gross_monthly_salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10)
    basis = models.CharField(max_length=24, choices=BASIS_CHOICES)
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_by",
    )
    effective_from = models.DateField()
    note = models.CharField(max_length=255, null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_id", "effective_from", "created_at"]
        verbose_name_plural = "Staff compensation histories"

    def __str__(self):
        return f"{self.staff_id}: {self.event_type} from {self.effective_from}"


class CompanyChargeRateHistory(models.Model):
    """L00-B3.2: company-wide, historized, AGGREGATE employee/employer charge
    rates -- a deliberately minimal estimate (two percentages, not an
    itemized breakdown per contribution type -- see CONCEPTION_L00B3.md
    arbitrage #2), never a legally-authoritative figure, never activated by
    default (no row = rates genuinely unknown, not 0%). Same append-only,
    "a correction/cancellation is a new row" precedent as
    StaffCompensationHistory -- resolved the exact same way
    ((-effective_from, -created_at)), never rewritten.

    Company-wide (not per-employee): DEC-04 (no self-mutation) does not
    naturally apply here -- there is no individual "staff" target, and
    changing a company-wide rate affects every employee identically, it is
    not a mechanism for one person to alter their OWN pay/net figure (see
    services.staff_employer_cost module docstring for the full reasoning,
    and RAPPORT_L00B32.md for this explicitly recorded as a point needing a
    human decision if this assessment is ever challenged)."""

    EVENT_NEW_SITUATION = "new_situation"
    EVENT_CORRECTION = "correction"
    EVENT_CANCELLATION = "cancellation"
    EVENT_CHOICES = (
        (EVENT_NEW_SITUATION, "New situation"),
        (EVENT_CORRECTION, "Retroactive correction"),
        (EVENT_CANCELLATION, "Cancellation of a scheduled change"),
    )

    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="charge_rate_history")
    employee_rate_percent = models.DecimalField(max_digits=5, decimal_places=2)
    employer_rate_percent = models.DecimalField(max_digits=5, decimal_places=2)
    effective_from = models.DateField()
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_by",
    )
    note = models.CharField(max_length=255, null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_profile_id", "effective_from", "created_at"]
        verbose_name_plural = "Company charge rate histories"

    def __str__(self):
        return f"{self.company_profile_id}: {self.event_type} from {self.effective_from}"


class CompanyCostItemType(models.Model):
    """L00-B3.2: a company's extensible catalog of ancillary-cost categories
    (insurance, occupational health, phone, works council, meals, travel,
    etc.) -- a plain, un-inherited per-company list (deliberately simpler
    than LeaveType's Admin/company inheritance, which would be
    over-engineered for this lot, see CONCEPTION_L00B3.md §1.3).
    Deactivating a type (is_active=False) only hides it from new
    assignments -- it NEVER touches or deletes any existing
    StaffAncillaryCostHistory row referencing it (on_delete=PROTECT on that
    model's FK enforces this at the database level too)."""

    FREQUENCY_MONTHLY = "monthly"
    FREQUENCY_ANNUAL = "annual"
    FREQUENCY_ONE_OFF = "one_off"
    FREQUENCY_CHOICES = (
        (FREQUENCY_MONTHLY, "Monthly"),
        (FREQUENCY_ANNUAL, "Annual"),
        (FREQUENCY_ONE_OFF, "One-off"),
    )

    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="cost_item_types")
    name = models.CharField(max_length=100)
    default_frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES)
    default_included_in_consolidated_cost = models.BooleanField(default=False)
    # L00-B3.2: free-text/enum-ish marker, explicitly NEVER a legal
    # conformity claim -- "to_qualify" until payroll/legal confirms a
    # rubric's real social/fiscal treatment (see CONCEPTION_L00B3.md).
    social_tax_treatment = models.CharField(max_length=24, default="to_qualify")
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_profile_id", "name"]

    def __str__(self):
        return f"{self.company_profile_id}: {self.name}"


class StaffAncillaryCostHistory(models.Model):
    """L00-B3.2: per-employee, per-cost-item-type, append-only assignment
    history -- same resolution rule as StaffCompensationHistory
    ((-effective_from, -created_at) per (staff, cost_item_type) pair,
    independently for each type the employee has ever had a row for).

    `amount=None` is a DELIBERATE, distinct state from `amount=0` -- it
    means "this benefit is not/no longer in force as of this row" (event_
    type=termination or a cancellation reverting to no prior assignment),
    never confused with a real, explicitly recorded zero-cost benefit
    (`amount=0`, event_type=new_assignment, e.g. a complimentary perk).

    `frequency` and `included_in_consolidated_cost` are captured on EVERY
    row (not read live from CompanyCostItemType at use time) -- the
    catalog's own defaults may change later, and a past assignment must
    stay interpretable exactly as it was confirmed (same precedent as RH12
    capturing full_time_reference_hours on every StaffQuotiteHistory row).

    `nature` distinguishes a budgeted ESTIMATE from a confirmed ACTUAL
    figure -- replacing an estimate with a real amount is a new row (same
    append-only discipline), so the two are never both counted (resolution
    only ever returns the single latest row per type)."""

    FREQUENCY_MONTHLY = CompanyCostItemType.FREQUENCY_MONTHLY
    FREQUENCY_ANNUAL = CompanyCostItemType.FREQUENCY_ANNUAL
    FREQUENCY_ONE_OFF = CompanyCostItemType.FREQUENCY_ONE_OFF
    FREQUENCY_CHOICES = CompanyCostItemType.FREQUENCY_CHOICES

    NATURE_ESTIMATED = "estimated"
    NATURE_ACTUAL = "actual"
    NATURE_CHOICES = (
        (NATURE_ESTIMATED, "Estimated"),
        (NATURE_ACTUAL, "Actual"),
    )

    EVENT_NEW_ASSIGNMENT = "new_assignment"
    EVENT_CORRECTION = "correction"
    EVENT_TERMINATION = "termination"
    EVENT_CANCELLATION = "cancellation"
    EVENT_CHOICES = (
        (EVENT_NEW_ASSIGNMENT, "New assignment"),
        (EVENT_CORRECTION, "Correction"),
        (EVENT_TERMINATION, "Termination (benefit ends)"),
        (EVENT_CANCELLATION, "Cancellation of a scheduled change"),
    )

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="ancillary_cost_history")
    cost_item_type = models.ForeignKey(CompanyCostItemType, on_delete=models.PROTECT, related_name="assignments")
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=10, null=True, blank=True)
    frequency = models.CharField(max_length=16, choices=FREQUENCY_CHOICES, null=True, blank=True)
    included_in_consolidated_cost = models.BooleanField(null=True, blank=True)
    nature = models.CharField(max_length=16, choices=NATURE_CHOICES, default=NATURE_ESTIMATED)
    event_type = models.CharField(max_length=20, choices=EVENT_CHOICES)
    supersedes = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="superseded_by",
    )
    effective_from = models.DateField()
    note = models.CharField(max_length=255, null=True, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_id", "cost_item_type_id", "effective_from", "created_at"]
        verbose_name_plural = "Staff ancillary cost histories"

    def __str__(self):
        return f"{self.staff_id}/{self.cost_item_type_id}: {self.event_type} from {self.effective_from}"