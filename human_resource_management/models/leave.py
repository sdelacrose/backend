import uuid

from django.db import models
from model_utils import FieldTracker
from simple_history.models import HistoricalRecords
from models.models import LeaveSubType, LeaveType, Staff, User

from human_resource_management.storage import get_leave_attachment_storage

HALF_DAY_MORNING = 'morning'
HALF_DAY_AFTERNOON = 'afternoon'
HALF_DAY_PERIODS = [(HALF_DAY_MORNING, 'Morning'), (HALF_DAY_AFTERNOON, 'Afternoon')]


class LeaveRequest(models.Model):
    LEAVE_STATUSES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ]
    employee = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name='leave_requests')
    manager = models.ForeignKey(
        Staff,
        on_delete=models.SET_NULL,
        related_name='approval_requests',
        null=True,
        blank=True,
    )
    # `type` (free-form label) is kept as the single source of truth for display
    # and historical requests. `leave_type`/`leave_sub_type` are the stable-id
    # link added in LOT RH 3 for NEW requests only -- never backfilled onto
    # historical rows (see services/leave_balance.py). SET_NULL so deleting a
    # LeaveType/LeaveSubType can never destroy a past request (its `type` label
    # survives regardless).
    type = models.CharField(max_length=50, null=True, blank=True)
    leave_type = models.ForeignKey(
        LeaveType, on_delete=models.SET_NULL, null=True, blank=True, related_name='leave_requests'
    )
    leave_sub_type = models.ForeignKey(
        LeaveSubType, on_delete=models.SET_NULL, null=True, blank=True, related_name='leave_requests'
    )
    start_date = models.DateField()
    end_date = models.DateField()
    is_working_from_home = models.BooleanField(default=False)
    is_half_day = models.BooleanField(default=False)
    half_day_period = models.CharField(max_length=16, choices=HALF_DAY_PERIODS, null=True, blank=True)
    status = models.CharField(choices=LEAVE_STATUSES, default='pending')
    reason = models.TextField(blank=True, null=True)
    # LOT RH 5: decided ONCE, at creation time, from the submission date vs
    # this request's own start_date -- never recomputed afterwards (an
    # early/future request must not become a "regularization" just because
    # time passes while it's still pending; see LeaveRequestSerializer.create
    # and .batch, the only two places this is ever set).
    is_regularization = models.BooleanField(default=False)
    # LOT RH13: no reliable decision audit existed anywhere on this model
    # before this lot (confirmed: no history/simple_history, no timestamp of
    # WHEN status became approved/rejected, no record of WHO decided) --
    # added as explicit, nullable fields, populated going forward ONLY by
    # LeaveRequestSerializer.update() the one place status is ever set. A
    # historical request decided before this lot shipped simply has both
    # None -- never backfilled/guessed (see that serializer's own comment).
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='decided_leave_requests'
    )
    tracker = FieldTracker()
    def __str__(self):
        return f"{self.employee.user.email} - {self.type} leave request"


def leave_attachment_upload_path(instance, filename):
    # LOT RH 4: deliberately its OWN model/path -- never a `Document`/
    # `DocumentAttachment` row. Those are reachable through the generic
    # Company Documents permission module (`documents.view`/`full_access`,
    # held broadly by company roles) and their serializers return a raw,
    # unsigned, non-expiring storage URL directly in list/retrieve JSON --
    # confirmed there is no mechanism in this codebase to make that private.
    # The random component means even within the private storage zone
    # (`leave_attachment_storage`, see human_resource_management/storage.py)
    # the path itself isn't a guessable "leave_request/<id>/<filename>"
    # pattern; the file is still only ever served through
    # LeaveRequestAttachmentViewSet.download's own permission check below,
    # never a raw URL returned to any client.
    return (
        f"company_{instance.leave_request.employee.user.company_id}/"
        f"leave_request_{instance.leave_request_id}/{uuid.uuid4().hex}_{filename}"
    )


class LeaveRequestAttachment(models.Model):
    leave_request = models.ForeignKey(LeaveRequest, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to=leave_attachment_upload_path, storage=get_leave_attachment_storage)
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.file_name} ({self.leave_request_id})"


class LeaveManagerOpinion(models.Model):
    """LOT RH 5: a team manager's non-binding operational opinion on a leave
    request from one of their direct reports (`Staff.supervisor`). Never
    changes `LeaveRequest.status` and never affects the leave balance --
    purely informational for HR, who can approve/reject with or against it.
    One row per request (corrected in place while still pending, rather than
    appended), with `simple_history` -- the same audit mechanism already
    used elsewhere in this codebase (see companyprofileapis.models) --
    keeping every prior value, its author and its date."""
    OPINION_FAVORABLE = 'favorable'
    OPINION_UNFAVORABLE = 'unfavorable'
    OPINION_CHOICES = [
        (OPINION_FAVORABLE, 'Favorable'),
        (OPINION_UNFAVORABLE, 'Unfavorable'),
    ]

    leave_request = models.OneToOneField(
        LeaveRequest, on_delete=models.CASCADE, related_name='manager_opinion'
    )
    opinion = models.CharField(max_length=16, choices=OPINION_CHOICES)
    comment = models.TextField(blank=True, null=True)
    given_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    given_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"{self.opinion} opinion on {self.leave_request_id}"
