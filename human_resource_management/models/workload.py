from django.db import models
from django.db.models import Q

from models.models import CompanyConfig, Staff, User


class StaffWorkloadAlertConfig(models.Model):
    """AI-2054: per (company, solution) configuration of the staff-workload
    overload alert -- the "rule" analog of AI-1685's LeaveBalanceReminderRule,
    scoped to the SAME CompanyConfig isolation boundary (company+solution),
    never a new isolation mechanism. Never active by default (`is_active`
    defaults to False) -- a company must explicitly turn alerting on.
    `recipients` is an explicit, RH-configured selection (never "everyone who
    happens to hold the permission") -- re-validated (active, licensed,
    permission still held, same company/solution) at delivery time, exactly
    like AI-1685's `cc_hr_users`/`_authorized_cc_users`, never trusted from
    this list alone."""

    company_config = models.OneToOneField(
        CompanyConfig, related_name="workload_alert_config", on_delete=models.CASCADE,
    )
    is_active = models.BooleanField(default=False)
    recipients = models.ManyToManyField(User, related_name="workload_alert_configs", blank=True)
    created_by = models.ForeignKey(User, related_name="+", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"WorkloadAlertConfig({self.company_config_id})"


class StaffWorkloadAlertEpisode(models.Model):
    """AI-2054: one continuous stretch of a staff member being strictly
    above 100% workload for a given calendar period (`period_key`, e.g.
    "2026-10") -- the de-duplication anchor for alerts (mirrors AI-1685's
    occurrence-by-deadline_date, but here the "occurrence" is an episode
    that can persist across many re-evaluations rather than a single
    deadline date).

    A partial unique constraint (`status != CLOSED`) guarantees AT MOST ONE
    LIVE episode (OPEN or INDETERMINATE) per (staff, period_key) at a time --
    re-evaluating while still above 100% always resolves to the SAME open
    row (no new delivery); a genuine close (confirmed <= 100% on sufficient
    data) followed later by a new crossing creates a genuinely NEW row with
    its own id, even on the same calendar day (the row's id/opened_at
    distinguishes it, never the date alone, so two same-day episodes are
    never collapsed).

    `INDETERMINATE` is a distinct, still-LIVE state for "data became
    insufficient while an episode was open" -- explicitly never silently
    folded into CLOSED (closing requires an actually-known rate <= 100%).
    Data clarity returning to show overload again simply RESUMES the same
    live row (back to OPEN, same id, no duplicate delivery for an already-
    served recipient) rather than opening a second episode."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        INDETERMINATE = "indeterminate", "Indeterminate"

    company_config = models.ForeignKey(
        CompanyConfig, related_name="workload_alert_episodes", on_delete=models.CASCADE,
    )
    staff = models.ForeignKey(Staff, related_name="workload_alert_episodes", on_delete=models.CASCADE)
    period_key = models.CharField(max_length=16, help_text="Calendar period, e.g. '2026-10'.")
    period_start = models.DateField()
    period_end = models.DateField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    started_on = models.DateField()
    closed_on = models.DateField(null=True, blank=True)
    last_evaluated_at = models.DateTimeField(auto_now_add=True)
    last_known_rate = models.DecimalField(max_digits=9, decimal_places=4, null=True, blank=True)
    # Set at any evaluation where this episode was open/would-open but no
    # recipient was currently eligible -- traced explicitly rather than
    # silently doing nothing (see services.workload_alerts).
    had_no_eligible_recipients = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["staff", "period_key"],
                condition=~Q(status="closed"),
                name="unique_open_workload_episode",
            ),
        ]
        indexes = [
            models.Index(fields=["staff", "period_key"], name="wl_episode_staff_period_idx"),
        ]

    def __str__(self):
        return f"WorkloadEpisode({self.staff_id}/{self.period_key})[{self.status}]"


class StaffWorkloadAlertDelivery(models.Model):
    """AI-2054: per-recipient delivery for one episode, same
    get_or_create+select_for_update claim pattern as AI-1685's
    LeaveReminderDelivery (see services.workload_alerts._claim_delivery) --
    a recalculation never resends to an already-SENT recipient, a failed
    recipient is retried independently of any other. `channel` is kept (not
    collapsed into a single implicit value) purely to mirror AI-1685's shape
    for a straightforward later extension -- only INTERNAL is ever produced
    in this delivery ("pas d'email dans cette première livraison")."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    class Channel(models.TextChoices):
        INTERNAL = "internal", "Internal notification"

    episode = models.ForeignKey(StaffWorkloadAlertEpisode, related_name="deliveries", on_delete=models.CASCADE)
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.INTERNAL)
    recipient = models.ForeignKey(User, related_name="workload_alert_deliveries_received", on_delete=models.CASCADE)
    is_cc = models.BooleanField(
        default=False,
        help_text="Reserved for parity with AI-1685's shape; always False in this delivery (there is no 'subject' recipient here distinct from RH recipients -- every recipient is an RH destinataire).",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["episode", "channel", "recipient"],
                name="unique_workload_alert_delivery",
            ),
        ]

    def __str__(self):
        return f"WorkloadDelivery({self.episode_id}->{self.recipient_id})[{self.status}]"
