"""L00-B3.2: the consolidated employer-cost ESTIMATE -- a simplified,
explicitly-labeled estimate for RH/finance consultation, never a payroll
engine, never a legally-authoritative net/gross figure. Reads
StaffCompensationHistory (L00-B3.1), CompanyChargeRateHistory and
StaffAncillaryCostHistory (this lot) -- never writes anything, never
touches the legacy Staff.salary/hourly_rate mirror, never computes an
internal hourly cost (deferred to sous-lot 3) and never feeds the EVM
(unmodified, unread by this module).

Rounding: ALL monetary results are quantized to the centime (2 decimal
places, ROUND_HALF_UP) at the single, centralized point below -- no other
rounding happens anywhere else in this module.
"""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from human_resource_management.models.staff import StaffCompensationHistory
from human_resource_management.services.staff_ancillary_costs import (
    resolve_active_ancillary_costs, resolve_current_charge_rates,
)
from human_resource_management.services.staff_compensation import resolve_current_compensation

CENTS = Decimal("0.01")
HUNDRED = Decimal("100")


def _money(value):
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


def resolve_employer_cost_estimate(staff, as_of=None):
    """Returns a dict describing the consolidated employer cost estimate at
    `as_of` (defaults to today). Always returns a `status` key; `status !=
    "complete"` means some part of the result is explicitly incomplete or
    not calculable, with its precise cause -- NEVER a fabricated total."""
    as_of = as_of or date.today()
    result = {"as_of": as_of.isoformat(), "status": None}

    comp = resolve_current_compensation(staff, as_of=as_of)
    if comp is None:
        result["status"] = "no_compensation_history"
        return result
    if comp.basis == StaffCompensationHistory.BASIS_LEGACY_UNQUALIFIED:
        # L00-B3.2 explicit rule: an unqualified legacy amount's brut/net
        # nature is unknown -- never treated as a confirmed gross figure.
        result["status"] = "unqualified_legacy"
        result["compensation_effective_from"] = comp.effective_from.isoformat()
        return result
    if comp.gross_monthly_salary is None:
        # An hourly-only situation (no monthly salary recorded) -- this
        # lot's formula is explicitly gross-monthly-based, not extended to
        # hourly contracts here.
        result["status"] = "no_monthly_salary"
        return result

    gross = comp.gross_monthly_salary
    currency = comp.currency
    result["gross_monthly_salary"] = float(gross)
    result["currency"] = currency
    result["compensation_basis"] = comp.basis
    result["compensation_effective_from"] = comp.effective_from.isoformat()

    company_profile = getattr(staff.user, "company", None)
    rates = resolve_current_charge_rates(company_profile, as_of=as_of) if company_profile else None
    if rates is None:
        result["status"] = "no_charge_rates"
        return result

    employee_rate = rates.employee_rate_percent
    employer_rate = rates.employer_rate_percent
    result["employee_rate_percent"] = float(employee_rate)
    result["employer_rate_percent"] = float(employer_rate)

    # L00-B3.2 explicit rule: employee charges are NEVER added to the
    # employer cost -- two entirely separate, non-overlapping formulas.
    net_estimate = _money(gross * (Decimal("1") - employee_rate / HUNDRED))
    employer_cost_excl_extras = _money(gross * (Decimal("1") + employer_rate / HUNDRED))
    result["net_estimate"] = float(net_estimate)
    result["employer_cost_excl_extras"] = float(employer_cost_excl_extras)

    active_costs = resolve_active_ancillary_costs(staff, as_of=as_of)
    included = [c for c in active_costs if c.included_in_consolidated_cost]
    excluded = [c for c in active_costs if not c.included_in_consolidated_cost]

    # L00-B3.2 explicit rule: never auto-prorate an annual cost by /12, nor
    # a one-off cost over any period -- only MONTHLY-frequency items are
    # summed into the consolidated monthly total; others are reported
    # separately, raw, never blended.
    monthly_included = [c for c in included if c.frequency == "monthly"]
    non_monthly_included = [c for c in included if c.frequency != "monthly"]

    # L00-B3.2 explicit rule: never sum different currencies without an
    # existing, dated conversion mechanism (none exists) -- a mismatch
    # marks the CONSOLIDATED total incomplete, while net/employer-cost-
    # excl-extras (which never involved ancillary costs) remain valid.
    mismatched_currency_items = [c for c in monthly_included if c.currency and c.currency != currency]
    monthly_included_same_currency = [c for c in monthly_included if not (c.currency and c.currency != currency)]

    extras_total = sum((c.amount for c in monthly_included_same_currency), Decimal("0"))
    consolidated = _money(employer_cost_excl_extras + extras_total)

    result["included_monthly_costs"] = [
        {"cost_item_type_id": c.cost_item_type_id, "name": c.cost_item_type.name,
         "amount": float(c.amount), "currency": c.currency, "nature": c.nature}
        for c in monthly_included_same_currency
    ]
    result["non_monthly_included_costs_not_summed"] = [
        {"cost_item_type_id": c.cost_item_type_id, "name": c.cost_item_type.name,
         "amount": float(c.amount), "currency": c.currency, "frequency": c.frequency, "nature": c.nature}
        for c in non_monthly_included
    ]
    result["excluded_costs"] = [
        {"cost_item_type_id": c.cost_item_type_id, "name": c.cost_item_type.name,
         "amount": float(c.amount), "currency": c.currency, "frequency": c.frequency, "nature": c.nature}
        for c in excluded
    ]

    if mismatched_currency_items:
        result["status"] = "mixed_currency_incomplete"
        result["mixed_currency_items"] = [
            {"cost_item_type_id": c.cost_item_type_id, "name": c.cost_item_type.name, "currency": c.currency}
            for c in mismatched_currency_items
        ]
        # The partial total (excluding the mismatched-currency items) is
        # still given, clearly labeled incomplete -- never silently
        # presented as the full consolidated figure.
        result["consolidated_employer_cost_partial"] = float(consolidated)
        return result

    result["consolidated_employer_cost"] = float(consolidated)
    result["status"] = "complete" if not non_monthly_included else "complete_with_separate_non_monthly_items"
    return result
