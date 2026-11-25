"""Evidence of one accepted transmittal send, separate from correspondence proofs."""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.dispatch import receiver


class ProofEvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Transmittal proof evidence cannot be bulk modified.")

    def delete(self):
        raise ValidationError("Transmittal proof evidence cannot be deleted.")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Transmittal proof evidence must be created individually.")


class TransmittalSendProof(models.Model):
    class Provider(models.TextChoices):
        SMTP = "smtp", "SMTP prepared message"
        GRAPH = "graph", "Microsoft 365 original"

    class State(models.TextChoices):
        PENDING = "PENDING", "Original pending"
        ARCHIVED = "ARCHIVED", "Archived"
        FAILED = "FAILED", "Archiving failed"

    event = models.OneToOneField('TransmittalEvent', related_name='send_proof', on_delete=models.PROTECT)
    attempt = models.OneToOneField('TransmittalEvent', related_name='prepared_proof', on_delete=models.PROTECT)
    provider = models.CharField(max_length=10, choices=Provider.choices)
    state = models.CharField(max_length=12, choices=State.choices, default=State.PENDING)
    error_code = models.CharField(max_length=64, blank=True, default='')
    graph_message_id = models.CharField(max_length=512, blank=True, default='')
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = ProofEvidenceQuerySet.as_manager()

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(check=~Q(state='ARCHIVED') | Q(archived_at__isnull=False), name='tr_proof_archived_has_date')]

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.state != self.State.PENDING:
                raise ValidationError('A transmittal proof starts pending.')
            if (self.event.event_type != 'SEND_SUCCEEDED' or self.attempt.event_type != 'SEND_STARTED'
                    or self.event.transmittal_id != self.attempt.transmittal_id
                    or (self.event.metadata or {}).get('attempt') != self.attempt_id
                    or (self.attempt.metadata or {}).get('provider') != self.provider):
                raise ValidationError('The proof must identify its successful send and exact attempt.')
        else:
            stored = type(self).objects.get(pk=self.pk)
            if stored.state == self.State.ARCHIVED:
                raise ValidationError('An archived transmittal proof cannot be modified.')
            if (stored.event_id, stored.attempt_id, stored.provider) != (self.event_id, self.attempt_id, self.provider):
                raise ValidationError('The source of a transmittal proof cannot change.')
            if self.state == self.State.ARCHIVED:
                required = 'original' if self.provider == self.Provider.GRAPH else 'prepared'
                if not self.files.filter(kind=required).exists() or not self.archived_at:
                    raise ValidationError('A complete proof requires its message.')
                expected = (self.attempt.metadata or {}).get('attachments') or []
                stored_files = list(self.files.filter(kind='attachment').values('source_revision_id', 'sha256'))
                if not expected or len(stored_files) != len(expected) or any(
                    not any(f['source_revision_id'] == item.get('document_revision') and f['sha256'] == item.get('sha256') for f in stored_files)
                    for item in expected
                ):
                    raise ValidationError('Every transmitted revision must have verified archived bytes.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Transmittal proofs cannot be deleted.')


class TransmittalSendProofFile(models.Model):
    class Kind(models.TextChoices):
        PREPARED = 'prepared', 'Copy prepared by LINKS'
        ORIGINAL = 'original', 'Microsoft 365 original'
        ATTACHMENT = 'attachment', 'Transmitted document'

    proof = models.ForeignKey(TransmittalSendProof, related_name='files', on_delete=models.PROTECT)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    file_name = models.CharField(max_length=500)
    content_type = models.CharField(max_length=255, blank=True, default='')
    file_size = models.BigIntegerField()
    sha256 = models.CharField(max_length=64)
    blob = models.ForeignKey('EvidenceBlob', related_name='transmittal_proof_files', on_delete=models.PROTECT)
    source_revision = models.ForeignKey('DocumentRevision', null=True, blank=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    objects = ProofEvidenceQuerySet.as_manager()

    class Meta:
        ordering = ['id']
        constraints = [
            models.UniqueConstraint(fields=['proof', 'kind'], condition=~Q(kind='attachment'), name='tr_proof_one_message_kind'),
            models.UniqueConstraint(fields=['proof', 'source_revision'], condition=Q(kind='attachment'), name='tr_proof_one_revision'),
            models.CheckConstraint(check=Q(kind='attachment', source_revision__isnull=False) | (~Q(kind='attachment') & Q(source_revision__isnull=True)), name='tr_proof_file_revision_kind'),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding or TransmittalSendProof.objects.filter(pk=self.proof_id, state=TransmittalSendProof.State.ARCHIVED).exists():
            raise ValidationError('Archived proof files cannot be changed or extended.')
        company_id = self.proof.event.transmittal.project.company_profile_id
        if (self.blob.company_profile_id != company_id or self.blob.sha256 != self.sha256 or self.blob.file_size != self.file_size):
            raise ValidationError('The proof file must match its tenant and stored bytes.')
        if self.kind == self.Kind.ATTACHMENT:
            expected = (self.proof.attempt.metadata or {}).get('attachments') or []
            if not any(item.get('document_revision') == self.source_revision_id and item.get('sha256') == self.sha256 for item in expected):
                raise ValidationError('The file must be one of the exact transmitted revisions.')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Transmittal proof files cannot be deleted.')


@receiver(pre_delete, sender=TransmittalSendProof)
@receiver(pre_delete, sender=TransmittalSendProofFile)
def protect_transmittal_proof(sender, instance, **kwargs):
    raise ValidationError('Transmittal proof evidence cannot be deleted in a cascade.')
