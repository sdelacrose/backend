"""LOT 3D-3D2: auditable send evidence for an OUT Correspondence.

CorrespondenceSendEvent is the immutable-once-terminal record of ONE send attempt -- mirrors
CorrespondenceValidationWorkflow's own "mutable while active, frozen once terminal" shape
(section 8's own docstring), not the purely append-only CorrespondenceValidationEvent, because a
send attempt genuinely has a short IN_PROGRESS phase (the row is created BEFORE the external
SMTP call, precisely so it can serve as the idempotency guard -- see services/
correspondence_send.py) that then resolves to SUCCESS or FAILED.

Never the Correspondence's own reference/direction/parent_correspondence -- send evidence is
additive audit data, never a second copy of Correspondence's own business fields.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import ProtectedError, Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from models.models import User

from .correspondence import Correspondence


class CorrespondenceSendEvent(models.Model):
    class Result(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        SUCCESS = "SUCCESS", "Success"
        FAILED = "FAILED", "Failed"

    correspondence = models.ForeignKey(
        Correspondence, related_name="send_events", on_delete=models.CASCADE,
    )
    # Who clicked Send -- kept even if the user is later removed (SET_NULL), the evidence itself
    # (from_email etc.) still stands on its own.
    sender = models.ForeignKey(
        User, related_name="correspondence_send_events", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    from_email = models.CharField(max_length=255, blank=True, default="")
    # Snapshots, not FKs to Network Contact: "what was true AT SEND TIME" (section 7) must
    # survive a later Contact edit/deletion. Each entry: {"email": str, "name": str|None}.
    to_recipients = models.JSONField(default=list, blank=True)
    cc_recipients = models.JSONField(default=list, blank=True)
    bcc_recipients = models.JSONField(default=list, blank=True)
    # The email's own subject/body as actually sent -- deliberately separate from
    # Correspondence.subject/message (section 5: editing them in the composer must never mutate
    # the already-approved Correspondence content).
    subject = models.CharField(max_length=500, blank=True, default="")
    body = models.TextField(blank=True, default="")
    # Attachment metadata snapshot only (section 6): CorrespondenceAttachment rows are already
    # immutable from the moment validation starts (is_content_locked blocks add/delete, and stays
    # true through APPROVED and therefore through ISSUED too) -- so the existing row IS already
    # a stable, tamper-proof record; duplicating the file bytes would add real storage cost for
    # no integrity benefit. Each entry: {"id", "file_name", "file_size"}.
    attachments_snapshot = models.JSONField(default=list, blank=True)
    # V1 is SMTP-only (section 2); "provider" is a plain label, not a foreign key, so a future
    # Microsoft Graph adapter needs no migration to start writing "graph" here instead.
    provider = models.CharField(max_length=20, default="smtp")
    # MAIL-1.2A section 8: "Boîte expéditrice" -- which MailboxConnection a Microsoft Graph send
    # actually used. Always null for provider="smtp" (no mailbox selection exists in that path).
    # SET_NULL, not CASCADE: MailboxConnection has no delete endpoint today (MAIL-1.1C1's own
    # docstring), but this send-evidence row must never become unreadable/deleted purely because
    # a mailbox record was removed by some future, as-yet-unbuilt admin action.
    mailbox = models.ForeignKey(
        "userapis.MailboxConnection",
        related_name="correspondence_send_events",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    # Django's SMTP backend gives no message id -- always blank for V1 (see report section 2).
    provider_message_id = models.CharField(max_length=255, blank=True, default="")
    result = models.CharField(max_length=20, choices=Result.choices, default=Result.IN_PROGRESS)
    error_message = models.TextField(blank=True, default="")
    # Only ever set on SUCCESS -- the actual transmission timestamp, deliberately never
    # Correspondence.issue_date (section 10: that stays the business/document date it already
    # was, untouched by this lot).
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # CORR-DIST-2 section 8: RESEND is now an approved business rule -- a historical
            # SUCCESS must never block a later, independent distribution occurrence. The
            # invariant this constraint now enforces is narrower and purely about concurrency:
            # at most one IN_PROGRESS row per Correspondence at any time (a concurrent second
            # Send/Resend request racing to create its own IN_PROGRESS row hits this constraint
            # -- IntegrityError, mapped to a clean "already sending" error by the service).
            # SUCCESS and FAILED rows are both exempt: every terminal attempt (success OR
            # failure) is its own permanent, independent piece of evidence, and either one must
            # let a subsequent attempt start. (Originally
            # correspondence_send_event_one_in_flight_or_success, LOT 3D-3D2 -- renamed because
            # "or_success" is no longer accurate.)
            models.UniqueConstraint(
                fields=["correspondence"],
                condition=Q(result="IN_PROGRESS"),
                name="correspondence_send_event_one_in_flight",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = (
                CorrespondenceSendEvent.objects.filter(pk=self.pk)
                .values_list("result", flat=True)
                .first()
            )
            if stored is not None and stored != self.Result.IN_PROGRESS:
                raise ValidationError("A completed send record cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Send evidence cannot be deleted.")

    def __str__(self):
        return f"send event {self.pk} correspondence {self.correspondence_id} {self.result}"


@receiver(pre_delete, sender=CorrespondenceSendEvent)
def refuse_cascade_delete_of_a_microsoft_365_send(sender, instance, **kwargs):
    """MAIL-1.4-B: the send history of a correspondence really sent through Microsoft 365 survives
    every delete that reaches it, a cascade included (a queryset delete, a project or a company being
    removed) -- whether or not its proof exists yet (a send recorded before MAIL-1.4-A has none).
    A send that failed, and an SMTP send, are not covered here: they keep their previous behavior."""
    if instance.provider == "graph" and instance.result == CorrespondenceSendEvent.Result.SUCCESS:
        raise ProtectedError(
            "A correspondence sent through Microsoft 365 keeps its send history.", {instance},
        )
