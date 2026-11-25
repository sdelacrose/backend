"""L03/L05 -- due-date reminders for onboarding steps and document-validity
expiry, reusing the EXACT AI-1685 delivery-ledger shape (LeaveReminderDelivery:
one row per (subject, recipient, channel), dedup via a unique constraint,
live re-derivation of "is this still actionable" on every run, a FAILED
delivery simply gets reclaimed -- flipped back to PENDING -- the next time
the same processing function runs, whether that's the scheduled command or
a manual trigger).

Deliberately scoped down from AI-1685's full shape in one respect: no
separate, company-configurable "Rule" model layer here -- the anticipation
window is a simple, fixed constant per feature
(services.hr_reminders.ONBOARDING_STEP_REMINDER_ANTICIPATION_DAYS /
DOCUMENT_VALIDITY_REMINDER_ANTICIPATION_DAYS), not a per-company admin
screen. This is a deliberate proportionality choice for this lot (building a
second and third fully-configurable multi-rule-per-company admin surface in
the SAME lot as everything else here would be disproportionate) -- the
dedup/retry/stop-at-resolution machinery, which is the part that is
genuinely hard to get right, is reused in full; only the "which rules exist"
configuration layer is simplified. Extending to configurable rules later
would reuse LeaveBalanceReminderRule's exact shape, unchanged."""
from django.db import models
from models.models import User

from human_resource_management.models.onboarding import OnboardingStepInstance
from human_resource_management.models.staff_documents import StaffDocumentFile


class ReminderStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"
    IGNORED = "ignored", "Ignored"


class ReminderChannel(models.TextChoices):
    INTERNAL = "internal", "Internal notification"
    EMAIL = "email", "Email"


class OnboardingStepReminderDelivery(models.Model):
    """One row per (step_instance, recipient, channel) -- the step's own
    `due_date` already uniquely identifies the occurrence (no separate
    `deadline_date` column needed, unlike LeaveReminderDelivery: a step's due
    date is itself immutable once computed, see OnboardingStepInstance's own
    docstring on `due_date` recompute discipline)."""

    step_instance = models.ForeignKey(OnboardingStepInstance, on_delete=models.CASCADE, related_name="reminder_deliveries")
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="onboarding_step_reminder_deliveries")
    channel = models.CharField(max_length=16, choices=ReminderChannel.choices)
    status = models.CharField(max_length=16, choices=ReminderStatus.choices, default=ReminderStatus.PENDING)
    triggered_manually = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["step_instance", "recipient", "channel"], name="unique_onboarding_step_reminder_delivery"),
        ]

    def __str__(self):
        return f"{self.step_instance_id}->{self.recipient_id} [{self.channel}:{self.status}]"


class StaffDocumentValidityReminderDelivery(models.Model):
    """One row per (file, recipient, channel) -- keyed to one IMMUTABLE
    StaffDocumentFile version, so a renewal (a brand new file row with its
    own new valid_until) is automatically a genuinely NEW occurrence, never
    a silent reuse of an old delivery row."""

    file = models.ForeignKey(StaffDocumentFile, on_delete=models.CASCADE, related_name="validity_reminder_deliveries")
    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="staff_document_validity_reminder_deliveries")
    channel = models.CharField(max_length=16, choices=ReminderChannel.choices)
    status = models.CharField(max_length=16, choices=ReminderStatus.choices, default=ReminderStatus.PENDING)
    triggered_manually = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["file", "recipient", "channel"], name="unique_staff_document_validity_reminder_delivery"),
        ]

    def __str__(self):
        return f"{self.file_id}->{self.recipient_id} [{self.channel}:{self.status}]"
