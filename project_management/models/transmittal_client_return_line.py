"""GED-WF.4.3.1: client returns received through an incoming (IN) Transmittal.

A line is the received fact "the client answered about THIS distribution" (one ClientIssue per IN, with its own
result, comments and files). It is not a document: no Document, DocumentRevision or TransmittalItem exists for it,
and nothing here touches the supplier circuit (TransmittalReceiptLine stays untouched).

Lifecycle, derived and never stored as a redundant status:
  draft        the IN is still a DRAFT: the line and its pending files can be edited/removed;
  to qualify   the IN is confirmed (RECEIVED) and `client_return` is empty: the reception is recorded but nobody
               holding documents.client_surveillance_manage has turned it into a ClientSurveillanceReturn yet;
  qualified    `client_return` is set (once: OneToOne), the immutable WF.4.3 return carries the result.

Files are kept apart under their own pending prefix until the confirmation, which turns each of them into an
EvidenceBlob (one per company and SHA-256) and clears the pending copy.
"""
import logging
import posixpath
import uuid

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_delete
from django.dispatch import receiver

from models.models import User

from .client_issue import ClientIssue
from .client_surveillance_code import ClientSurveillanceCode
from .client_surveillance_return import ClientSurveillanceReturn
from .correspondence_send_proof import EvidenceBlob
from .transmittal import Transmittal, TransmittalEvent

logger = logging.getLogger(__name__)

# Distinct from the supplier prefix (transmittal_receipt_pending): the supplier cleanup never sees these files.
PENDING_CLIENT_RETURN_PREFIX = "pending_client_returns"


def get_pending_client_return_upload_path(instance, filename):
    line = instance.line
    return posixpath.join(
        PENDING_CLIENT_RETURN_PREFIX, f"project_{line.transmittal.project_id}", f"transmittal_{line.transmittal_id}",
        uuid.uuid4().hex, posixpath.basename(filename),
    )


class TransmittalClientReturnLine(models.Model):
    Category = ClientSurveillanceCode.Category
    Channel = ClientSurveillanceReturn.Channel

    transmittal = models.ForeignKey(Transmittal, on_delete=models.PROTECT, related_name="client_return_lines")
    client_issue = models.ForeignKey(ClientIssue, on_delete=models.PROTECT, related_name="in_return_lines")
    # The exact SEND_SUCCEEDED answered. Null only when the issue has no recorded real send: then
    # `no_send_justification` holds the explicit, traced confirmation (never an invented send event).
    send_event = models.ForeignKey(TransmittalEvent, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    category = models.CharField(max_length=32, choices=ClientSurveillanceCode.Category.choices, null=True, blank=True)
    received_at = models.DateTimeField()
    source_channel = models.CharField(max_length=20, choices=ClientSurveillanceReturn.Channel.choices)
    comment = models.TextField(blank=True, default="")
    client_reference = models.CharField(max_length=255, blank=True, default="")
    # Informative only: never copied onto ClientIssue.client_revision_label nor onto a DocumentRevision.
    client_revision = models.CharField(max_length=100, blank=True, default="")
    no_send_justification = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    # Set once, at qualification (immediately at confirmation for a user who may manage client surveillances).
    client_return = models.OneToOneField(ClientSurveillanceReturn, on_delete=models.PROTECT, null=True, blank=True, related_name="in_line")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["transmittal", "client_issue"], name="in_client_line_one_per_issue"),
            models.CheckConstraint(
                check=Q(category__isnull=True) | Q(category__in=["ACCEPTED", "ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED"]),
                name="in_client_line_known_category"),
            models.CheckConstraint(check=Q(source_channel__in=["EMAIL", "PORTAL", "POSTAL_MAIL", "OTHER"]), name="in_client_line_known_channel"),
        ]

    @property
    def is_qualified(self):
        return self.client_return_id is not None

    def save(self, *args, **kwargs):
        if self._state.adding:
            parent = Transmittal.objects.filter(pk=self.transmittal_id).values("direction", "status").first()
            if parent is None or parent["direction"] != Transmittal.Direction.IN or parent["status"] != Transmittal.Status.DRAFT:
                raise ValidationError("A client return line only belongs to an incoming draft transmittal.")
        else:
            stored = type(self).objects.get(pk=self.pk)
            if stored.client_return_id is not None:
                raise ValidationError("A qualified client return line is immutable.")
            for field in self._meta.concrete_fields:
                if field.name != "client_return" and getattr(stored, field.attname) != getattr(self, field.attname):
                    raise ValidationError("A received client return line can only be qualified, never edited.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.client_return_id is not None or self.transmittal.status != Transmittal.Status.DRAFT:
            raise ValidationError("Only a line of an incoming draft can be removed.")
        return super().delete(*args, **kwargs)


class TransmittalClientReturnLineFile(models.Model):
    class Kind(models.TextChoices):
        ANNOTATED = "annotated", "Annotated document"
        COMMENT_SHEET = "comment_sheet", "Comment sheet"
        OTHER = "other", "Other"

    line = models.ForeignKey(TransmittalClientReturnLine, on_delete=models.PROTECT, related_name="files")
    file_name = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True, default="")
    file_size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ANNOTATED)
    # Pending copy until the confirmation; cleared once the bytes are kept as an EvidenceBlob.
    file = models.FileField(upload_to=get_pending_client_return_upload_path, max_length=500, null=True, blank=True)
    blob = models.ForeignKey(EvidenceBlob, on_delete=models.PROTECT, null=True, blank=True, related_name="client_return_line_files")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["line", "sha256"], name="in_client_line_file_unique_sha"),
            models.CheckConstraint(check=Q(blob__isnull=False) | (Q(file__isnull=False) & ~Q(file="")), name="in_client_line_file_has_bytes"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            stored = type(self).objects.get(pk=self.pk)
            if stored.blob_id is not None:
                raise ValidationError("A kept client return file is immutable.")
            if (stored.line_id, stored.sha256, stored.file_size, stored.file_name) != (self.line_id, self.sha256, self.file_size, self.file_name):
                raise ValidationError("A client return file can only be converted, never edited.")
        if self.blob_id is not None and (self.blob.sha256 != self.sha256 or self.blob.file_size != self.file_size):
            raise ValidationError("The kept file must match its stored bytes.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.blob_id is not None or self.line.transmittal.status != Transmittal.Status.DRAFT:
            raise ValidationError("A kept client return file cannot be removed.")
        return super().delete(*args, **kwargs)


@receiver(post_delete, sender=TransmittalClientReturnLineFile)
def delete_pending_client_return_file(sender, instance, **kwargs):
    """The pending copy goes away with its row, only once the deletion is really committed (a rolled-back
    deletion, or a failed confirmation, never loses a pending file that is still referenced)."""
    name, storage = (instance.file.name if instance.file else None), instance.file.storage if instance.file else None
    if not name:
        return

    def _delete():
        try:
            storage.delete(name)
        except Exception:  # pragma: no cover - traced; the file stays under the pending prefix
            logger.exception("Pending client return file could not be deleted: %s", name)

    transaction.on_commit(_delete)
