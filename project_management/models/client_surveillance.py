"""Explicit client follow-up, separate from internal validation and supplier surveillance."""
from django.db import models
from django.conf import settings
from django.core.exceptions import ValidationError

class ClientSurveillance(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under review'
        READY_FOR_CLOSURE = 'READY_FOR_CLOSURE', 'Ready for closure'
        CLOSED = 'CLOSED', 'Closed'
    class Result(models.TextChoices):
        # GED-WF.4.3: the four stable categories of ClientSurveillanceCode plus the legacy REVIEWED, which is
        # NEVER read as ACCEPTED (it stays "reviewed, to qualify"). COMMENTED/REJECTED keep their meaning.
        REVIEWED = 'REVIEWED', 'Reviewed'
        ACCEPTED = 'ACCEPTED', 'Accepted'
        ACCEPTED_WITH_COMMENTS = 'ACCEPTED_WITH_COMMENTS', 'Accepted with comments'
        COMMENTED = 'COMMENTED', 'Commented'
        REJECTED = 'REJECTED', 'Rejected'
    client_issue = models.OneToOneField('ClientIssue', on_delete=models.PROTECT, related_name='surveillance')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    result = models.CharField(max_length=32, choices=Result.choices, null=True, blank=True)
    comment = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # GED-WF.4.2: opened automatically by the first successful OUT send FOR_REVIEW. All nullable: a
    # surveillance created by hand (or before WF.4.2) simply has none of these.
    opening_transmittal_item = models.ForeignKey('TransmittalItem', on_delete=models.PROTECT, null=True, blank=True, related_name='+')
    # Unique: one send event can never open two surveillances.
    opening_send_event = models.OneToOneField('TransmittalEvent', on_delete=models.PROTECT, null=True, blank=True, related_name='opened_client_surveillance')
    review_duration_days = models.PositiveIntegerField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)  # aware datetime, stored in UTC
    opened_at = models.DateTimeField(null=True, blank=True)

class ClientSurveillanceEventQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Client surveillance events are immutable.')
    def delete(self):
        raise ValidationError('Client surveillance events are immutable.')

class ClientSurveillanceEvent(models.Model):
    class EventType(models.TextChoices):
        STATUS = 'STATUS', 'Status'  # every event recorded before WF.4.2
        OPENED = 'OPENED', 'Opened'
        RESENT = 'RESENT', 'Resent'
        DUE_DATE_CHANGED = 'DUE_DATE_CHANGED', 'Due date changed'
        # GED-WF.4.3: a client return was recorded / corrected / recorded after the closure (information only).
        RETURN_RECORDED = 'RETURN_RECORDED', 'Client return recorded'
        RETURN_CORRECTED = 'RETURN_CORRECTED', 'Client return corrected'
        RETURN_LATE = 'RETURN_LATE', 'Client return received after closure'
        # LOT 1: automated follow-up of a return (opened, file provided, validated, closed by a verified send,
        # explicit review needed).
        FU_OPENED = 'FU_OPENED', 'Follow-up opened'
        FU_FILE = 'FU_FILE', 'Follow-up file provided'
        FU_VALIDATED = 'FU_VALIDATED', 'Follow-up revision validated'
        AUTO_CLOSED = 'AUTO_CLOSED', 'Closed by the verified send'
        REVIEW_NEEDED = 'REVIEW_NEEDED', 'Explicit review needed'
    objects = ClientSurveillanceEventQuerySet.as_manager()
    event_type = models.CharField(max_length=20, choices=EventType.choices, default=EventType.STATUS)
    data = models.JSONField(null=True, blank=True)
    surveillance = models.ForeignKey(ClientSurveillance, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name='+')
    status = models.CharField(max_length=20)
    result = models.CharField(max_length=32, null=True, blank=True)
    comment = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['created_at', 'pk']
    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Client surveillance events are immutable.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Client surveillance events are immutable.')
