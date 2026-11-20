"""GED-WF.4.3: the facts a client sends back about a revision it received, kept as immutable rows.

A return is anchored on the ClientIssue (revision x client company), so it exists with or without a
surveillance (a FOR_INFORMATION distribution has none, and a return can arrive before the surveillance
opens). It never changes a document, a revision or a status: the business consequences of the category
belong to GED-WF.5. A return is never edited or deleted: a mistake is corrected by a NEW return that points
to the one it replaces (`supersedes`), and each return can be replaced once at most. The retained return
of a closed surveillance is chosen explicitly at closure, never inferred from dates.

The visible code and the EN/FR/DE labels are frozen here at recording time (null label = the default
label of the stable category), so renaming a project code later never rewrites history.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .client_surveillance_code import ClientSurveillanceCode


class ImmutableReturnQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Client returns are immutable.")

    def delete(self):
        raise ValidationError("Client returns are immutable.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Client returns must be created individually.")


class ClientSurveillanceReturn(models.Model):
    Category = ClientSurveillanceCode.Category

    class Channel(models.TextChoices):
        EMAIL = "EMAIL", "E-mail"
        PORTAL = "PORTAL", "Portal"
        POSTAL_MAIL = "POSTAL_MAIL", "Postal mail"
        OTHER = "OTHER", "Other"

    objects = ImmutableReturnQuerySet.as_manager()

    client_issue = models.ForeignKey("ClientIssue", on_delete=models.PROTECT, related_name="client_returns")
    # The exact SEND_SUCCEEDED this return answers. Null only when the issue has no recorded real send
    # (a distribution issued before sends were tracked): never a guess.
    send_event = models.ForeignKey("TransmittalEvent", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    # Null only for a spontaneous return on a FOR_INFORMATION distribution without a formal result.
    category = models.CharField(max_length=32, choices=ClientSurveillanceCode.Category.choices, null=True, blank=True)
    code_snapshot = models.CharField(max_length=10, blank=True, default="")
    label_en_snapshot = models.CharField(max_length=100, null=True, blank=True)
    label_fr_snapshot = models.CharField(max_length=100, null=True, blank=True)
    label_de_snapshot = models.CharField(max_length=100, null=True, blank=True)
    received_at = models.DateTimeField()
    comment = models.TextField(blank=True, default="")
    source_correspondence = models.ForeignKey("Correspondence", on_delete=models.PROTECT, null=True, blank=True, related_name="client_surveillance_returns")
    source_reference = models.CharField(max_length=255, blank=True, default="")
    source_channel = models.CharField(max_length=20, choices=Channel.choices)
    in_transmittal = models.ForeignKey("Transmittal", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    supersedes = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="corrections")
    correction_reason = models.TextField(blank=True, default="")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["recorded_at", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["client_issue", "source_correspondence"], condition=Q(source_correspondence__isnull=False, supersedes__isnull=True), name="client_return_issue_corr_once"),
            # one correction per return, even under concurrency
            models.UniqueConstraint(fields=["supersedes"], condition=Q(supersedes__isnull=False), name="client_return_one_correction"),
            models.CheckConstraint(check=Q(supersedes__isnull=True) | ~Q(correction_reason=""), name="client_return_correction_has_reason"),
            models.CheckConstraint(
                check=Q(category__isnull=True) | Q(category__in=["ACCEPTED", "ACCEPTED_WITH_COMMENTS", "COMMENTED", "REJECTED"]),
                name="client_return_known_category"),
            models.CheckConstraint(check=Q(source_channel__in=["EMAIL", "PORTAL", "POSTAL_MAIL", "OTHER"]), name="client_return_known_channel"),
        ]
        indexes = [
            models.Index(fields=["client_issue", "received_at"], name="client_return_issue_recv_idx"),
            models.Index(fields=["in_transmittal"], name="client_return_in_idx"),
            models.Index(fields=["send_event"], name="client_return_send_idx"),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Client returns are immutable.")
        if self.supersedes_id:
            previous = type(self).objects.filter(pk=self.supersedes_id).values("client_issue_id").first()
            if previous is None or previous["client_issue_id"] != self.client_issue_id:
                raise ValidationError("A correction must replace a return of the same client issue.")
            if not (self.correction_reason or "").strip():
                raise ValidationError("A correction needs a reason.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Client returns are immutable.")


class ClientSurveillanceReturnFileQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Client return files are immutable.")

    def delete(self):
        raise ValidationError("Client return files are immutable.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Client return files must be created individually.")


class ClientSurveillanceReturnFile(models.Model):
    class Kind(models.TextChoices):
        ANNOTATED = "annotated", "Annotated document"
        COMMENT_SHEET = "comment_sheet", "Comment sheet"
        OTHER = "other", "Other"

    objects = ClientSurveillanceReturnFileQuerySet.as_manager()

    # named client_return: "return" is a reserved word
    client_return = models.ForeignKey(ClientSurveillanceReturn, on_delete=models.PROTECT, related_name="files")
    blob = models.ForeignKey("EvidenceBlob", on_delete=models.PROTECT, related_name="client_return_files")
    file_name = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True, default="")
    file_size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.ANNOTATED)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]
        constraints = [models.UniqueConstraint(fields=["client_return", "blob"], name="client_return_file_unique_blob")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Client return files are immutable.")
        if self.blob.sha256 != self.sha256 or self.blob.file_size != self.file_size:
            raise ValidationError("The return file must match its stored bytes.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Client return files are immutable.")
