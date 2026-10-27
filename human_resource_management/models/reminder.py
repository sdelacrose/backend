from django.db import models

from models.models import CompanyConfig, LeaveType, Staff, User


class LeaveBalanceReminderRule(models.Model):
    """AI-1685: a configurable rule triggering a leave-balance reminder
    before an employee's contract end date (or an arbitrary fixed
    deadline), scoped to one CompanyConfig -- the SAME company+solution
    isolation boundary as LeaveType (see views.leave_type.
    _company_configs_for_request / permissions.resolve_authorized_
    solution_for_write), never a new isolation mechanism. Never active by
    default (`is_active=False`) -- a company must explicitly turn a rule on;
    creating/editing it never sends anything by itself."""

    TRIGGER_CONTRACT_END = "contract_end"
    TRIGGER_DEADLINE = "deadline"
    TRIGGER_CHOICES = (
        (TRIGGER_CONTRACT_END, "Contract end date"),
        (TRIGGER_DEADLINE, "Fixed deadline date"),
    )

    company_config = models.ForeignKey(
        CompanyConfig, related_name="leave_balance_reminder_rules", on_delete=models.CASCADE,
    )
    name = models.CharField(max_length=255)
    trigger = models.CharField(max_length=16, choices=TRIGGER_CHOICES)
    # Required only when trigger == TRIGGER_DEADLINE (validated at the
    # serializer, not here -- same "validate the relevant field, don't block
    # everything" precedent as StaffSerializer.validate_weekly_hours).
    deadline_date = models.DateField(null=True, blank=True)
    anticipation_days = models.PositiveIntegerField(
        help_text="Calendar days before the deadline the reminder window opens. 0 allowed (reminder only on the deadline day itself).",
    )
    leave_types = models.ManyToManyField(LeaveType, related_name="leave_balance_reminder_rules")
    # "Tous les collaborateurs eligibles de cette entreprise/solution" vs an
    # explicit, named selection -- never both silently combined.
    all_eligible_staff = models.BooleanField(default=False)
    staff = models.ManyToManyField(Staff, related_name="leave_balance_reminder_rules", blank=True)
    email_enabled = models.BooleanField(default=False)
    # Explicitly selected HR users to CC -- authorization (still holding
    # staff_management.manage_leave_reminders, still in the same
    # company/solution) is re-checked server-side at delivery time, never
    # trusted from this list alone (see services.leave_reminders).
    cc_hr_users = models.ManyToManyField(User, related_name="cc_leave_balance_reminder_rules", blank=True)
    is_active = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, related_name="+", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.company_config_id})"


class LeaveReminderDelivery(models.Model):
    """AI-1685 follow-up: one row per (rule, staff, deadline, channel,
    RECIPIENT) -- the exact ActionItemReminderDelivery pattern
    (project_management.models.project), adapted minimally, now with an
    explicit `recipient` so the collaborator's own delivery and each CC'd
    HR recipient's delivery are tracked (and retried) completely
    INDEPENDENTLY: a failed send to one recipient is retried for THAT
    recipient alone, never re-sent to a recipient who already has a SENT/
    IGNORED row for this exact occurrence+channel. `staff` identifies WHOSE
    balance/occurrence this is about (the subject); `recipient` identifies
    WHO actually receives this one delivery (the subject's own user, or an
    authorized CC'd HR user) -- the two coincide for the collaborator's own
    deliveries. `deadline_date` is part of the row (not just read live from
    the rule/staff at send time) so that a later contract-end-date change
    produces a genuinely NEW occurrence (a new unique row) rather than
    silently reusing the old one -- the old row's history is never
    rewritten. `channel` is also part of the unique key so a failed email
    retry can never recreate/duplicate an already-sent internal
    notification for the same occurrence."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        # Explicitly NOT an error -- e.g. the recipient's email preference is
        # disabled. Never counted as a failure, never retried.
        IGNORED = "ignored", "Ignored"

    class Channel(models.TextChoices):
        INTERNAL = "internal", "Internal notification"
        EMAIL = "email", "Email"

    rule = models.ForeignKey(LeaveBalanceReminderRule, related_name="deliveries", on_delete=models.CASCADE)
    staff = models.ForeignKey(Staff, related_name="leave_reminder_deliveries", on_delete=models.CASCADE)
    deadline_date = models.DateField()
    channel = models.CharField(max_length=16, choices=Channel.choices)
    recipient = models.ForeignKey(
        User, related_name="leave_reminder_deliveries_received", on_delete=models.CASCADE,
    )
    is_cc = models.BooleanField(
        default=False,
        help_text="True for a CC'd HR recipient's own delivery row; False for the subject staff's own.",
    )
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    # True when this occurrence was created by an RH-initiated manual
    # trigger rather than the scheduled command -- both share the exact
    # same claim/dedup row, never two separate deliveries for one occurrence.
    triggered_manually = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "staff", "deadline_date", "channel", "recipient"],
                name="unique_leave_reminder_delivery",
            ),
        ]
        indexes = [
            models.Index(fields=["deadline_date", "status"], name="leave_reminder_date_status_idx"),
        ]

    def __str__(self):
        return f"{self.rule_id}/{self.staff_id}/{self.deadline_date}/{self.channel}->{self.recipient_id} [{self.status}]"
