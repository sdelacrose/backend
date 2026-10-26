"""LOT 3B: Correspondence -- a formal external communication.

It is NOT a controlled Document/Livrable, a DocumentRevision, a Transmittal, or a review/
approval workflow (LOT 3A / 3A-BIS). It owns its own permanent reference, its own reference
counter (never shares Document's), and its own response-tracking lifecycle
(`response_required` / `response_status`), completely independent of internal Document
approval and of the future Transmittal concept.
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from models.models import CompanyProfile, User

from .contact import ContactCompany, ContactUser
from .project import Project


def get_correspondence_attachment_upload_path(instance, filename):
    """<correspondence_attachments>/correspondence_<id>/<filename>.

    Correspondence has no Folder tree (unlike Document), so the path is simply keyed by the
    Correspondence id -- storage never overwrites a same-named upload (Django's default
    FileSystemStorage already deduplicates by suffixing the stored name).
    """
    return f"correspondence_attachments/correspondence_{instance.correspondence_id}/{filename}"


class Correspondence(models.Model):
    """A formal IN/OUT external communication. May optionally require a response, tracked via
    `response_required` / `response_status` -- independent of Livrable review and of
    Transmittal (LOT 3A-BIS section 1/9/11).
    """

    class Direction(models.TextChoices):
        IN = "IN", "Incoming"
        OUT = "OUT", "Outgoing"

    class Status(models.TextChoices):
        """LOT 3B stabilization: a minimal, one-way lifecycle -- NOT a workflow engine. It
        only distinguishes a prepared Correspondence (a draft OUT response, typically) from a
        formally issued/received one. issue_date remains a plain business date; it is no
        longer used as a proxy for this."""

        DRAFT = "DRAFT", "Draft"
        ISSUED = "ISSUED", "Issued"

    class ResponseStatus(models.TextChoices):
        NOT_REQUIRED = "NOT_REQUIRED", "Not required"
        TO_ANSWER = "TO_ANSWER", "To answer"
        IN_PREPARATION = "IN_PREPARATION", "In preparation"
        AWAITING_REPLY = "AWAITING_REPLY", "Awaiting reply"
        ANSWERED = "ANSWERED", "Answered"
        # Never written by any create/update path: derived at read time from a still-pending
        # status plus an elapsed response_due_date (LOT 3B section 4 -- no scheduler).
        OVERDUE = "OVERDUE", "Overdue"

    PENDING_STATUSES = (
        ResponseStatus.TO_ANSWER,
        ResponseStatus.IN_PREPARATION,
        ResponseStatus.AWAITING_REPLY,
    )

    # Owning library scope: mirrors Folder / Document / PurchaseOrder exactly (never shared
    # with Document's own counters/permissions -- a completely separate concept).
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="correspondences",
        null=True, blank=True,
    )
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="correspondences",
        null=True, blank=True,
    )
    freelancer = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="freelancer_correspondences",
        null=True, blank=True,
    )

    # Permanent once allocated (LOT 3B section 8/10): never rewritten after creation.
    reference = models.CharField(max_length=255, blank=True, default="", db_index=True)
    direction = models.CharField(max_length=3, choices=Direction.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    # Configurable company/project reference data (LOT 3B stabilization section 2): validated
    # against CompanyConfig/ECPMPConfig.correspondence_types, same mechanism as Document's own
    # configured fields -- plain labels, no "Label:CODE" segment (none is needed here).
    correspondence_type = models.CharField(max_length=255, null=True, blank=True)
    subject = models.CharField(max_length=500)
    message = models.TextField(blank=True, default="")
    # LOT 3D-3A TER: a free-text identifier from the external party's own numbering (e.g. their
    # letter/reference number) -- distinct from `reference`, which is this system's own
    # permanent, auto-generated number (LOT 3B section 8/10) and is never affected by this field.
    external_reference = models.CharField(max_length=255, null=True, blank=True)

    # External party (canonical Network architecture -- never duplicated).
    sender_company = models.ForeignKey(
        ContactCompany, on_delete=models.SET_NULL, related_name="correspondences_sent",
        null=True, blank=True,
    )
    sender_contact = models.ForeignKey(
        ContactUser, on_delete=models.SET_NULL, related_name="correspondences_sent",
        null=True, blank=True,
    )
    recipient_company = models.ForeignKey(
        ContactCompany, on_delete=models.SET_NULL, related_name="correspondences_received",
        null=True, blank=True,
    )
    recipient_contact = models.ForeignKey(
        ContactUser, on_delete=models.SET_NULL, related_name="correspondences_received",
        null=True, blank=True,
    )

    issue_date = models.DateField(null=True, blank=True)
    received_date = models.DateField(null=True, blank=True)

    response_required = models.BooleanField(default=False)
    response_due_date = models.DateField(null=True, blank=True)
    response_status = models.CharField(
        max_length=20, choices=ResponseStatus.choices, default=ResponseStatus.NOT_REQUIRED,
    )

    # The response to (or of) another Correspondence. Direction must differ from the parent's
    # (never IN->IN or OUT->OUT) and both must share the exact same owner scope -- enforced in
    # the serializer, not here, to keep model-level saves usable from trusted service code.
    parent_correspondence = models.ForeignKey(
        "self", on_delete=models.SET_NULL, related_name="responses",
        null=True, blank=True,
    )

    # LOT 3D-3A TER: the person accountable for this Correspondence -- mirrors Document.assignee
    # exactly (same FK shape/on_delete), reusing the existing per-record "responsible user"
    # precedent rather than inventing a second one. The picker itself is not new either: the
    # frontend reuses useInlineEditMemberOptions (project-team members in Project scope, company
    # members in Company scope) unchanged.
    responsible = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name="responsible_correspondences",
        null=True, blank=True,
    )

    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name="created_correspondences",
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                check=(
                    (Q(project__isnull=False) & Q(company_profile__isnull=True) & Q(freelancer__isnull=True))
                    | (Q(project__isnull=True) & Q(company_profile__isnull=False) & Q(freelancer__isnull=True))
                    | (Q(project__isnull=True) & Q(company_profile__isnull=True) & Q(freelancer__isnull=False))
                ),
                name="correspondence_exactly_one_owner_scope",
            ),
        ]

    def __str__(self):
        return self.reference or f"Correspondence #{self.pk}"

    @property
    def display_reference(self):
        """LOT 3D-3C0C section 8: the ONE reference to show for this Correspondence, direction-
        aware. OUT always shows its own internally generated `reference` -- unaffected by this
        lot. IN shows the sender's manually-entered `external_reference` when present; a
        historical IN record from before this lot (back when `reference` was generated for
        every direction) falls back to that instead of going blank -- historical rows are never
        rewritten (section 7/8), so they must never lose a reference they already display."""
        if self.direction == self.Direction.OUT:
            return self.reference
        return self.external_reference or self.reference or ""

    @property
    def is_overdue(self):
        """Derived, never persisted (LOT 3B section 4/6): no background scheduler rewrites
        rows merely because time passed."""
        if self.response_status not in self.PENDING_STATUSES:
            return False
        if not self.response_due_date:
            return False
        return self.response_due_date < timezone.localdate()

    @property
    def effective_response_status(self):
        """The value to display: OVERDUE overrides a still-pending stored status once the
        due date has passed; the stored value otherwise. Never write OVERDUE to `response_status`."""
        return self.ResponseStatus.OVERDUE if self.is_overdue else self.response_status

    def save(self, *args, **kwargs):
        if self.pk:
            original = Correspondence.objects.get(pk=self.pk)
            if original.reference and self.reference != original.reference:
                raise ValidationError("The correspondence reference cannot be changed.")
            if self.direction != original.direction:
                raise ValidationError("The correspondence direction cannot be changed.")
            if original.status == self.Status.ISSUED and self.status == self.Status.DRAFT:
                raise ValidationError(
                    "An issued correspondence cannot be reverted to draft."
                )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        # MAIL-1.4-A: a correspondence that was really sent keeps its send history (and the proofs
        # that hang on it). A never-sent one, including one whose attempts only failed, is deleted
        # exactly as before. (A delete cascading from another object is stopped, for Microsoft 365
        # sends, by CorrespondenceSendProof.event being PROTECT.)
        if self.pk and self.send_events.filter(result="SUCCESS").exists():
            raise ValidationError("A correspondence that was sent cannot be deleted.")
        return super().delete(*args, **kwargs)


class CorrespondenceAttachment(models.Model):
    """A file attached to a Correspondence: evidence of the communication only -- never a
    controlled Document, DocumentRevision, or Document-level DocumentAttachment (LOT 3A-BIS
    section 10/16)."""

    correspondence = models.ForeignKey(
        Correspondence, related_name="attachments", on_delete=models.CASCADE,
    )
    file = models.FileField(upload_to=get_correspondence_attachment_upload_path, max_length=500)
    file_name = models.CharField(max_length=500)
    file_size = models.BigIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        User, related_name="uploaded_correspondence_attachments",
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class CorrespondenceReferenceCounter(models.Model):
    """A locked, owner-scoped sequence counter for Correspondence references -- structurally
    identical to DocumentReferenceCounter's pattern, but a fully separate table/state: never
    shared with Document's own counters (LOT 3B section 8)."""

    project = models.ForeignKey(
        Project, related_name="correspondence_reference_counters",
        on_delete=models.CASCADE, null=True, blank=True,
    )
    company_profile = models.ForeignKey(
        CompanyProfile, related_name="correspondence_reference_counters",
        on_delete=models.CASCADE, null=True, blank=True,
    )
    freelancer = models.ForeignKey(
        User, related_name="correspondence_reference_counters",
        on_delete=models.CASCADE, null=True, blank=True,
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    (Q(project__isnull=False) & Q(company_profile__isnull=True) & Q(freelancer__isnull=True))
                    | (Q(project__isnull=True) & Q(company_profile__isnull=False) & Q(freelancer__isnull=True))
                    | (Q(project__isnull=True) & Q(company_profile__isnull=True) & Q(freelancer__isnull=False))
                ),
                name="correspondence_counter_exactly_one_owner_scope",
            ),
            models.UniqueConstraint(fields=["project"], name="unique_correspondence_counter_project", condition=Q(project__isnull=False)),
            models.UniqueConstraint(fields=["company_profile"], name="unique_correspondence_counter_company", condition=Q(company_profile__isnull=False)),
            models.UniqueConstraint(fields=["freelancer"], name="unique_correspondence_counter_freelancer", condition=Q(freelancer__isnull=False)),
        ]
