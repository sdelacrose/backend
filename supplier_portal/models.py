"""Explicit external access and deliberately published supplier-facing evidence."""
from django.core.exceptions import ValidationError
from django.db import models


class SupplierPortalGrant(models.Model):
    document = models.ForeignKey('project_management.Document', on_delete=models.PROTECT, related_name='supplier_portal_grants')
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    user = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='supplier_portal_grants')
    granted_by = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey('models.User', null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['document', 'user'], condition=models.Q(revoked_at__isnull=True), name='supplier_portal_active_grant')]


class PortalEvidenceQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('Supplier portal evidence cannot be changed.')

    def delete(self):
        raise ValidationError('Supplier portal evidence cannot be deleted.')


class ImmutablePortalEvidence(models.Model):
    objects = PortalEvidenceQuerySet.as_manager()
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError('Supplier portal evidence cannot be changed.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('Supplier portal evidence cannot be deleted.')


class SupplierPortalSubmission(ImmutablePortalEvidence):
    grant = models.ForeignKey(SupplierPortalGrant, null=True, blank=True, on_delete=models.PROTECT, related_name='submissions')
    project_access = models.ForeignKey('SupplierProjectAccess', null=True, blank=True, on_delete=models.PROTECT, related_name='submissions')
    revision = models.OneToOneField('project_management.DocumentRevision', on_delete=models.PROTECT, related_name='portal_submission')
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=(models.Q(grant__isnull=False, project_access__isnull=True) |
            models.Q(grant__isnull=True, project_access__isnull=False)), name='supplier_submission_one_access')]


class SupplierPortalReturn(ImmutablePortalEvidence):
    surveillance = models.OneToOneField('project_management.SupplierSurveillance', on_delete=models.PROTECT, related_name='portal_return')
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    published_by = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='+')
    public_comment = models.TextField(blank=True, default='')
    result = models.CharField(max_length=20)
    new_revision_required = models.BooleanField()
    published_at = models.DateTimeField(auto_now_add=True)


class SupplierProjectAccess(models.Model):
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    folder = models.ForeignKey('project_management.Folder', on_delete=models.PROTECT)
    user = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='supplier_projects')
    granted_by = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey('models.User', null=True, blank=True, on_delete=models.PROTECT, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'supplier', 'user'], condition=models.Q(revoked_at__isnull=True), name='supplier_project_active_access')]


class SupplierProposalBatch(models.Model):
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    folder = models.ForeignKey('project_management.Folder', on_delete=models.PROTECT)
    submitted_by = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='+')
    source = models.CharField(max_length=10, choices=[('MANUAL', 'Manual'), ('IMPORT', 'Import')])
    created_at = models.DateTimeField(auto_now_add=True)


class SupplierDocumentProposal(models.Model):
    batch = models.ForeignKey(SupplierProposalBatch, on_delete=models.PROTECT, related_name='rows')
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    line_number = models.PositiveIntegerField()
    metadata = models.JSONField(default=dict)
    identity_key = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=[('PENDING', 'Pending'), ('REFUSED', 'Refused'), ('ACCEPTED', 'Accepted')])
    errors = models.JSONField(default=dict)
    document = models.OneToOneField('project_management.Document', null=True, blank=True, on_delete=models.PROTECT, related_name='supplier_proposal')
    decided_by = models.ForeignKey('models.User', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_reason = models.TextField(blank=True, default='')

    class Meta:
        ordering = ['line_number', 'id']
        constraints = [
            models.UniqueConstraint(fields=['batch', 'line_number'], name='supplier_proposal_batch_line'),
            models.UniqueConstraint(fields=['project', 'supplier', 'identity_key'], condition=models.Q(status__in=['PENDING', 'ACCEPTED']), name='supplier_proposal_reserved_identity'),
            models.CheckConstraint(condition=(models.Q(status='ACCEPTED', document__isnull=False) |
                models.Q(status__in=['PENDING', 'REFUSED'], document__isnull=True)), name='supplier_proposal_document_state'),
        ]


class SupplierProposalEvent(ImmutablePortalEvidence):
    proposal = models.ForeignKey(SupplierDocumentProposal, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey('models.User', on_delete=models.PROTECT, related_name='+')
    event_type = models.CharField(max_length=24)
    reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)


class SupplierSharedExchange(ImmutablePortalEvidence):
    project = models.ForeignKey('project_management.Project', on_delete=models.PROTECT)
    supplier = models.ForeignKey('project_management.ContactCompany', on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=[('CORRESPONDENCE', 'Correspondence'), ('TRANSMITTAL', 'Transmittal')])
    source_id = models.PositiveBigIntegerField()
    snapshot = models.JSONField(default=dict)
    published_by = models.ForeignKey('models.User', on_delete=models.SET_NULL, null=True, related_name='+')
    published_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['project', 'supplier', 'kind', 'source_id'], name='supplier_shared_exchange_once')]

from .shared_storage import PrivateSupplierStorage

class SupplierSharedFile(ImmutablePortalEvidence):
    exchange = models.ForeignKey(SupplierSharedExchange, on_delete=models.PROTECT, related_name='files')
    file = models.FileField(storage=PrivateSupplierStorage(), upload_to='exchanges/%Y/%m', max_length=500)
    name = models.CharField(max_length=500)
    sha256 = models.CharField(max_length=64)


class SupplierFeedbackSnapshot(ImmutablePortalEvidence):
    publication = models.OneToOneField(SupplierPortalReturn,on_delete=models.PROTECT,related_name='feedback_snapshot')
    content = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

class SupplierFeedbackAnswer(ImmutablePortalEvidence):
    submission = models.ForeignKey(SupplierPortalSubmission,on_delete=models.PROTECT,related_name='feedback_answers')
    publication = models.ForeignKey(SupplierPortalReturn,on_delete=models.PROTECT,related_name='answers')
    comment_id = models.PositiveIntegerField()
    disposition = models.CharField(max_length=20,choices=[('ADDRESSED','Addressed'),('PARTIAL','Partially addressed'),('NOT_ADDRESSED','Not addressed')])
    response = models.TextField()
    responded_by = models.ForeignKey('models.User',on_delete=models.PROTECT,related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints=[models.UniqueConstraint(fields=['submission','publication','comment_id'],name='supplier_feedback_answer_unique')]

class SupplierFeedbackAssessment(ImmutablePortalEvidence):
    answer = models.ForeignKey(SupplierFeedbackAnswer,on_delete=models.PROTECT,related_name='assessments')
    accepted = models.BooleanField()
    comment = models.TextField(blank=True,default='')
    assessed_by = models.ForeignKey('models.User',on_delete=models.PROTECT,related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
