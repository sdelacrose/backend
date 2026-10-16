"""MAIL-1.4-A: the structures that will hold the PROOF of an OUT send -- one proof per send event.

A proof is what will keep, for ONE precise CorrespondenceSendEvent, the original message (.eml) found
in Sent Items and every attachment actually sent, each with its SHA-256. It is never a second copy of
the Correspondence's own business fields and never the correspondence's current attachments:

  * CorrespondenceSendProof     one per send event (a resend is another event, hence another proof).
                                 The history of a send; its retryable state while the files are not
                                 all kept yet.
  * CorrespondenceSendProofFile one kept file of a proof (the .eml or one sent attachment), with its
                                 own size and SHA-256. Rows of different proofs may share a SHA-256:
                                 two sends of the same file are two entries of two histories.
  * EvidenceBlob                the physical file, one per company and SHA-256, independent of every
                                 CorrespondenceAttachment (deleting or replacing a current attachment
                                 never touches it). Histories are separate rows; only bytes are shared.

This lot only creates the structures: nothing here reads a mailbox, downloads or stores a file.

Nothing finalised can be changed or removed: a proof once ARCHIVED, a proof file and a blob are never
updated, and none of the three can be deleted (their relations are PROTECT too, so deleting a
correspondence or a send event that has a proof is refused instead of cascading).
"""

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from models.models import CompanyProfile

from .correspondence import CorrespondenceAttachment
from .correspondence_send import CorrespondenceSendEvent


def get_evidence_blob_upload_path(instance, filename):
    """send_evidence/company_<id>/<first 2 of sha256>/<sha256> -- keyed only by the content hash,
    never by a user supplied file name (that name lives on the proof file row)."""
    return f"send_evidence/company_{instance.company_profile_id}/{instance.sha256[:2]}/{instance.sha256}"


class CorrespondenceSendProof(models.Model):
    class State(models.TextChoices):
        PENDING = "PENDING", "Pending"          # created, nothing kept yet (or still being retried)
        ARCHIVED = "ARCHIVED", "Archived"       # the .eml and every sent attachment are kept: final
        PARTIAL = "PARTIAL", "Partial"          # some files kept, the rest to retry
        UNAVAILABLE = "UNAVAILABLE", "Unavailable"  # Sent Items copy not found within the retry period
        FAILED = "FAILED", "Failed"             # a technical failure, to retry

    # PROTECT: the send event (and through it the correspondence) cannot be deleted while it has a
    # proof -- never a cascade that would destroy the quality history.
    event = models.OneToOneField(
        CorrespondenceSendEvent, related_name="proof", on_delete=models.PROTECT,
    )
    # The Sent Items copy of THIS send, once found through its HMAC marker. SET_NULL: the proof must
    # outlive the cached message metadata (e.g. a removed mailbox).
    mailbox_message = models.ForeignKey(
        "userapis.MailboxMessage", related_name="send_proofs", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    # A short technical code (e.g. "copy_not_found", "temporary"): never a message, token or address.
    error_code = models.CharField(max_length=64, blank=True, default="")
    archived_at = models.DateTimeField(null=True, blank=True)
    # The exact documents/revisions diffused, when a later flow knows them. Never inferred from names.
    distribution_snapshot = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                check=Q(state="ARCHIVED", archived_at__isnull=False) | ~Q(state="ARCHIVED"),
                name="send_proof_archived_has_date",
            ),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.state != self.State.PENDING:
                raise ValidationError("A send proof starts as pending.")
        else:
            stored = (
                CorrespondenceSendProof.objects.filter(pk=self.pk).values_list("state", flat=True).first()
            )
            if stored == self.State.ARCHIVED:
                raise ValidationError("An archived send proof cannot be modified.")
            if self.state == self.State.ARCHIVED and not self.files.filter(
                kind=CorrespondenceSendProofFile.Kind.EML
            ).exists():
                raise ValidationError("A proof cannot be archived without its original message.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Send proofs cannot be deleted.")

    def __str__(self):
        return f"send proof {self.pk} of event {self.event_id} {self.state}"


class EvidenceBlob(models.Model):
    company_profile = models.ForeignKey(
        CompanyProfile, related_name="evidence_blobs", on_delete=models.PROTECT,
    )
    sha256 = models.CharField(max_length=64)
    file_size = models.BigIntegerField()
    content_type = models.CharField(max_length=255, blank=True, default="")
    file = models.FileField(upload_to=get_evidence_blob_upload_path, max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile", "sha256"], name="evidence_blob_unique_content_per_company",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A stored evidence file cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Evidence files cannot be deleted.")

    def __str__(self):
        return f"evidence blob {self.pk}"


class CorrespondenceSendProofFile(models.Model):
    class Kind(models.TextChoices):
        EML = "eml", "Original message (.eml)"
        ATTACHMENT = "attachment", "Sent attachment"

    proof = models.ForeignKey(
        CorrespondenceSendProof, related_name="files", on_delete=models.PROTECT,
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    file_name = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True, default="")
    # The real size and SHA-256 of the kept bytes (never a declared size); both must equal the blob's.
    file_size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    # Graph's own attachment id ("" for the .eml); technical, never sent to a client.
    graph_attachment_id = models.CharField(max_length=512, blank=True, default="")
    # The CorrespondenceAttachment this was sent from, when it still exists. Information only: the
    # proof never depends on it.
    source_attachment = models.ForeignKey(
        CorrespondenceAttachment, related_name="send_proof_files", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    blob = models.ForeignKey(EvidenceBlob, related_name="proof_files", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["proof"], condition=Q(kind="eml"), name="send_proof_file_one_eml_per_proof",
            ),
            models.UniqueConstraint(
                fields=["proof", "graph_attachment_id"], condition=Q(kind="attachment"),
                name="send_proof_file_one_row_per_graph_attachment",
            ),
        ]
        indexes = [models.Index(fields=["sha256"], name="send_proof_file_sha256_idx")]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("A kept proof file cannot be modified.")
        if self.proof.state == CorrespondenceSendProof.State.ARCHIVED:
            raise ValidationError("An archived send proof cannot receive more files.")
        if self.blob.sha256 != self.sha256 or self.blob.file_size != self.file_size:
            raise ValidationError("The proof file must match its stored evidence file.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Proof files cannot be deleted.")

    def __str__(self):
        return f"send proof file {self.pk} ({self.kind}) of proof {self.proof_id}"
