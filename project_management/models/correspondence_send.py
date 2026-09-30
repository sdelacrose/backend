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
from django.db.models import Q

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
            # Section 15 (idempotency): at most one IN_PROGRESS-or-SUCCESS row per Correspondence
            # at any time -- a concurrent second Send request racing to create its own
            # IN_PROGRESS row hits this constraint (IntegrityError, mapped to a clean "already
            # sending" error by the service), and once a SUCCESS row exists no further attempt
            # can even start. FAILED rows are exempt on purpose: retrying after a failure must
            # create a new attempt/row, preserving every prior failure as its own evidence.
            models.UniqueConstraint(
                fields=["correspondence"],
                condition=Q(result__in=["IN_PROGRESS", "SUCCESS"]),
                name="correspondence_send_event_one_in_flight_or_success",
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
