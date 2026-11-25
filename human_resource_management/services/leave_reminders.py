"""AI-1685: leave-balance reminder rules -- eligibility, preview, and
delivery. Reuses existing RH9-13 reference services wherever possible
(get_entitlement_breakdown, resolve_company_timezone, create_notification_
with_recipients, get_user_email_preferences) rather than introducing any
new computation for balances, timezones, or notifications themselves."""
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from ecpmpapp.utils import create_notification_with_recipients
from human_resource_management.models.reminder import LeaveBalanceReminderRule, LeaveReminderDelivery
from human_resource_management.permissions import (
    has_cumulative_leave_review_permission,
    has_staff_management_view_permission,
)
from human_resource_management.services.leave_accrual import (
    LeaveAccrualTimezoneError,
    resolve_company_timezone,
)
from human_resource_management.services.leave_entitlement import get_entitlement_breakdown
from human_resource_management.utils import send_leave_balance_reminder_email
from models.models import Staff
from userapis.utils import get_user_email_preferences


class LeaveReminderBlockedError(Exception):
    """A rule could not be processed at all (e.g. no valid company timezone)
    -- surfaced explicitly to the caller (command/manual trigger), never a
    silent skip or a fallback to a different timezone."""


def eligible_staff_for_rule(rule):
    """Every Staff this rule could ever apply to, scoped to the rule's own
    CompanyConfig (company OR freelancer, same shape as CompanyConfig
    itself) -- narrowed to the explicit `staff` selection unless
    `all_eligible_staff` is set."""
    company_config = rule.company_config
    if company_config.user_id:
        base = Staff.objects.filter(user_id=company_config.user_id)
    else:
        base = Staff.objects.filter(
            user__company_id=company_config.company_profile_id, user__is_deleted=False,
        )
    if rule.all_eligible_staff:
        return base
    return base.filter(pk__in=rule.staff.values_list("pk", flat=True))


def resolve_occurrence_deadline(rule, staff):
    """The deadline date for THIS rule/staff pair, read live (never cached)
    -- a later contract-end-date change or rule edit is picked up on the
    very next call, which is what lets a prolongation/cancellation simply
    stop producing new deliveries for the old date without any special-case
    code (see module docstring on LeaveReminderDelivery)."""
    if rule.trigger == LeaveBalanceReminderRule.TRIGGER_CONTRACT_END:
        return staff.contract_end_date
    return rule.deadline_date


def compute_balance_snapshot(staff, leave_types, as_of):
    """Per leave-type balance, reusing get_entitlement_breakdown AS-IS (the
    single RH9-13 reference computation) for a single-day window on `as_of`
    -- this naturally returns only the CYCLE CONTAINING `as_of` (never a
    future not-yet-started cycle, never a past closed one), and `available`
    already includes any active carry-over (own + carry-over summed exactly
    once -- never re-added on top of `carry_over_available`)."""
    snapshot = []
    for leave_type in leave_types:
        breakdowns = get_entitlement_breakdown(staff, leave_type, as_of, as_of)
        if not breakdowns:
            continue
        b = breakdowns[0]
        carry_over_expires_on = b.carry_over_expires_on
        snapshot.append({
            "leave_type_id": leave_type.id,
            "leave_type_name": leave_type.name,
            "available": float(b.available) if b.available is not None else None,
            "own_available": float(b.own_available) if b.own_available is not None else None,
            "reserved": float(b.reserved) if b.reserved is not None else None,
            "carry_over_available": (
                float(b.carry_over_available) if b.carry_over_available is not None else None
            ),
            "carry_over_expires_on": carry_over_expires_on.isoformat() if carry_over_expires_on else None,
        })
    return snapshot


def _resolve_occurrence(rule, staff, local_today):
    """Resolves BOTH the automatic (scheduled job) and manual (RH-initiated)
    eligibility for this staff/occurrence. The two differ in exactly one
    way: a manual trigger may ANTICIPATE the automatic window (fire before
    `deadline - anticipation_days`), matching the explicit requirement that
    RH can act ahead of the scheduled job -- but neither mode ever acts
    once the deadline itself has passed, and neither ever ignores the
    balance check."""
    deadline = resolve_occurrence_deadline(rule, staff)
    if deadline is None:
        return {
            "staff_id": staff.id, "staff_name": staff.user.full_name,
            "deadline_date": None, "deadline_date_raw": None,
            "in_window": False, "manual_in_window": False, "past_deadline": False,
            "balances": [], "exclusion_reason": "no_deadline_date",
            "manual_exclusion_reason": "no_deadline_date",
        }
    window_start = deadline - timedelta(days=rule.anticipation_days)
    past_deadline = local_today > deadline
    within_date_window = window_start <= local_today <= deadline
    balances = compute_balance_snapshot(staff, rule.leave_types.all(), local_today)
    has_available_balance = any((b["available"] or 0) > 0 for b in balances)

    if past_deadline:
        exclusion_reason = "deadline_passed"
    elif not within_date_window:
        exclusion_reason = "not_yet_in_window"
    elif not has_available_balance:
        exclusion_reason = "no_available_balance"
    else:
        exclusion_reason = None

    # Manual: the SAME deadline/balance constraints, but the date-window
    # floor is never applied -- RH may anticipate it explicitly.
    if past_deadline:
        manual_exclusion_reason = "deadline_passed"
    elif not has_available_balance:
        manual_exclusion_reason = "no_available_balance"
    else:
        manual_exclusion_reason = None

    return {
        "staff_id": staff.id, "staff_name": staff.user.full_name,
        "deadline_date": deadline.isoformat(), "deadline_date_raw": deadline,
        "in_window": exclusion_reason is None,
        "manual_in_window": manual_exclusion_reason is None,
        "past_deadline": past_deadline,
        "balances": balances,
        "exclusion_reason": exclusion_reason,
        "manual_exclusion_reason": manual_exclusion_reason,
    }


def _authorized_cc_users(rule):
    """Re-checked at delivery time (never cached/trusted from rule.cc_hr_
    users alone) -- a CC'd HR user who lost their permission, or moved to a
    different company, since the rule was saved is silently excluded here,
    never delivered to."""
    company_id = rule.company_config.company_profile_id
    authorized = set()
    for user in rule.cc_hr_users.all():
        if company_id and user.company_id != company_id:
            continue
        if has_cumulative_leave_review_permission(user) or has_staff_management_view_permission(user):
            authorized.add(user)
    return authorized


def _claim_delivery(rule, staff, deadline_date, channel, recipient, *, manual, is_cc):
    with transaction.atomic():
        delivery, created = LeaveReminderDelivery.objects.get_or_create(
            rule=rule, staff=staff, deadline_date=deadline_date, channel=channel, recipient=recipient,
            defaults={"triggered_manually": manual, "is_cc": is_cc},
        )
        if created:
            return delivery
        delivery = LeaveReminderDelivery.objects.select_for_update().get(pk=delivery.pk)
        if delivery.status != LeaveReminderDelivery.Status.FAILED:
            # Already pending/sent/ignored for THIS recipient's exact
            # occurrence+channel -- never a forced resend, and never
            # affects any OTHER recipient's own independent row.
            return None
        delivery.status = LeaveReminderDelivery.Status.PENDING
        delivery.last_error = ""
        delivery.save(update_fields=["status", "last_error", "updated_at"])
        return delivery


def _build_reminder_message(rule, staff, deadline_date, balances):
    lines = [
        f"Reminder: {staff.user.full_name}'s leave balance ahead of "
        f"{'contract end' if rule.trigger == LeaveBalanceReminderRule.TRIGGER_CONTRACT_END else 'deadline'} "
        f"on {deadline_date}.",
    ]
    for b in balances:
        line = f"- {b['leave_type_name']}: {b['available']} available (reserved: {b['reserved']})"
        if b["carry_over_available"]:
            expiry = b["carry_over_expires_on"] or "no expiry recorded"
            line += f", including a carry-over of {b['carry_over_available']} expiring {expiry}"
        lines.append(line)
    lines.append(
        "Only currently available balance is shown -- never a promise that every "
        "right will remain usable until departure. Please review before the deadline."
    )
    return "\n".join(lines)


def _deliver_to_recipient(rule, staff, deadline_date, balances, recipient, *, manual, is_cc):
    """Every channel for ONE recipient, fully independent of any other
    recipient's own claim/send/retry for this same occurrence."""
    outcomes = {}
    internal_delivery = _claim_delivery(
        rule, staff, deadline_date, LeaveReminderDelivery.Channel.INTERNAL, recipient,
        manual=manual, is_cc=is_cc,
    )
    if internal_delivery is not None:
        try:
            create_notification_with_recipients(
                message=_build_reminder_message(rule, staff, deadline_date, balances),
                content_type=ContentType.objects.get_for_model(LeaveReminderDelivery),
                object_id=internal_delivery.id,
                content_object=internal_delivery,
                notification_type="leave_balance_reminder",
                recipients={recipient},
                bypass_preference_check=True,
            )
            internal_delivery.status = LeaveReminderDelivery.Status.SENT
            internal_delivery.sent_at = timezone.now()
            internal_delivery.save(update_fields=["status", "sent_at", "updated_at"])
        except Exception as exc:  # pragma: no cover - defensive, mirrors ActionItemReminderDelivery
            internal_delivery.status = LeaveReminderDelivery.Status.FAILED
            internal_delivery.last_error = str(exc)
            internal_delivery.save(update_fields=["status", "last_error", "updated_at"])
        outcomes["internal"] = internal_delivery.status
    else:
        outcomes["internal"] = "already_delivered"

    if rule.email_enabled:
        email_delivery = _claim_delivery(
            rule, staff, deadline_date, LeaveReminderDelivery.Channel.EMAIL, recipient,
            manual=manual, is_cc=is_cc,
        )
        if email_delivery is not None:
            email_pref = get_user_email_preferences(recipient.id)
            if not email_pref:
                email_delivery.status = LeaveReminderDelivery.Status.IGNORED
                email_delivery.last_error = "Email preference disabled for this recipient."
                email_delivery.save(update_fields=["status", "last_error", "updated_at"])
            else:
                try:
                    # One independent send PER recipient (never a single
                    # multi-recipient to/cc send) -- a transport failure for
                    # one recipient can never block or re-send to another.
                    send_leave_balance_reminder_email(
                        staff, deadline_date, balances, to_user=recipient,
                    )
                    email_delivery.status = LeaveReminderDelivery.Status.SENT
                    email_delivery.sent_at = timezone.now()
                    email_delivery.save(update_fields=["status", "sent_at", "updated_at"])
                except Exception as exc:
                    email_delivery.status = LeaveReminderDelivery.Status.FAILED
                    email_delivery.last_error = str(exc)
                    email_delivery.save(update_fields=["status", "last_error", "updated_at"])
            outcomes["email"] = email_delivery.status
        else:
            outcomes["email"] = "already_delivered"
    return outcomes


def _deliver_occurrence(rule, staff, deadline_date, balances, *, manual):
    """Per-recipient delivery: the subject's own user, plus every currently
    authorized CC'd HR user -- each with their own independent claim/send/
    retry (see _deliver_to_recipient)."""
    per_recipient = {"staff": _deliver_to_recipient(
        rule, staff, deadline_date, balances, staff.user, manual=manual, is_cc=False,
    )}
    cc_outcomes = {}
    for cc_user in _authorized_cc_users(rule):
        cc_outcomes[cc_user.id] = _deliver_to_recipient(
            rule, staff, deadline_date, balances, cc_user, manual=manual, is_cc=True,
        )
    per_recipient["cc"] = cc_outcomes
    return per_recipient


def build_rule_preview(rule, *, as_of=None):
    """Read-only: never claims/creates a delivery row. Lists every eligible
    staff member with their resolved deadline, balances, and -- when not
    currently actionable -- an explicit exclusion reason for BOTH the
    automatic job (`exclusion_reason`) and a manual trigger
    (`manual_exclusion_reason`, which never applies the date-window floor)
    -- never silently omitted."""
    tz = resolve_company_timezone(rule.company_config)
    local_today = as_of or timezone.now().astimezone(tz).date()
    return [
        _resolve_occurrence(rule, staff, local_today)
        for staff in eligible_staff_for_rule(rule).select_related("user")
    ]


def process_rule(rule, *, manual=False, as_of=None):
    """Shared by the scheduled command/task and the manual-trigger endpoint
    -- the exact same eligibility recheck and anti-duplicate claim either
    way, so a manual trigger can never create a second delivery for an
    occurrence the automatic run already sent (or vice versa). The ONLY
    difference between the two modes is which window flag is consulted
    (`in_window` for the automatic job, `manual_in_window` -- which may
    anticipate the window -- for a manual trigger); the balance/deadline
    checks and the delivery/claim logic itself are identical."""
    try:
        tz = resolve_company_timezone(rule.company_config)
    except LeaveAccrualTimezoneError as exc:
        raise LeaveReminderBlockedError(str(exc)) from exc
    if not rule.is_active:
        raise LeaveReminderBlockedError(f"Rule {rule.id} is not active.")
    local_today = as_of or timezone.now().astimezone(tz).date()
    entries = []
    for staff in eligible_staff_for_rule(rule).select_related("user"):
        entry = _resolve_occurrence(rule, staff, local_today)
        actionable = entry["manual_in_window"] if manual else entry["in_window"]
        if actionable:
            entry["deliveries"] = _deliver_occurrence(
                rule, staff, entry["deadline_date_raw"], entry["balances"], manual=manual,
            )
        entries.append(entry)
    return entries


def process_all_active_rules():
    """Shared by the management command and the scheduled Celery task --
    one place defining "process every active rule", so the two entry
    points can never drift apart (same precedent as
    human_resource_management.tasks mirroring run_monthly_accrual/
    run_cycle_closure 1:1). Isolates one rule's failure from the rest."""
    results = []
    for rule in LeaveBalanceReminderRule.objects.filter(is_active=True):
        try:
            entries = process_rule(rule, manual=False)
            results.append({"rule_id": rule.id, "entries": entries})
        except LeaveReminderBlockedError as exc:
            results.append({"rule_id": rule.id, "blocked": str(exc)})
        except Exception as exc:  # noqa: BLE001 - isolate one rule's failure from the rest
            results.append({"rule_id": rule.id, "error": str(exc)})
    return results
