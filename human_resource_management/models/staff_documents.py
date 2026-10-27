"""L03 -- private HR documents attached to a Staff record. Deliberately its
own model family, never an extension of the generic `Document`/`Folder`
module: that module's storage (`S3Boto3Storage` with `AWS_S3_CUSTOM_DOMAIN`,
or `FileSystemStorage` served through Django's own `static()` helper) returns
a stable, unsigned, PUBLIC link for every upload -- already confirmed and
fixed once before for leave-request attachments (see
`human_resource_management/storage.py`'s docstring). The same private
storage approach is reused here (`staff_document_storage`), and NOTHING in
this app ever calls `.url()` on a StaffDocumentFile -- only the gated
`download` action ever streams its bytes (see views/staff_documents.py).

This covers BOTH individually-private pieces (ID card, signed contract...)
AND documents "shared" within a company (an HR policy, a handbook) -- the
word "shared" never means "publicly reachable by URL"; it only ever changes
WHO is authorized to view it (see StaffDocumentCategory.scope below), never
how the file is stored or served.
"""
import uuid

from django.core.exceptions import ValidationError
from django.db import models
from models.models import CompanyProfile, Staff, User

from human_resource_management.storage import staff_document_storage


class StaffDocumentCategory(models.Model):
    """A company's extensible catalog of HR document categories -- empty at
    first, RH creates its own (exactly the same convention as
    CompanyCostItemType, L00-B3.2). Deactivating a category never touches or
    deletes any existing StaffDocument/StaffDocumentFile referencing it
    (on_delete=PROTECT below enforces this at the database level too)."""

    SCOPE_PRIVATE = "private"
    SCOPE_SHARED = "shared"
    SCOPE_CHOICES = (
        (SCOPE_PRIVATE, "Private to one collaborator"),
        (SCOPE_SHARED, "Shared within the company"),
    )

    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="staff_document_categories")
    name = models.CharField(max_length=100)
    # SHARED categories are never individually-private by nature (e.g. a
    # company handbook) -- still served through the SAME private storage and
    # gated download as PRIVATE ones, only the authorized-viewer set differs
    # (see permissions.can_view_staff_document). Never a path back to the
    # generic, unsigned Document/Folder module.
    scope = models.CharField(max_length=16, choices=SCOPE_CHOICES, default=SCOPE_PRIVATE)
    requires_validity_date = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    # LOT L04 (SST): a COMPLETELY SEPARATE confidentiality tier, narrower than
    # the general view_documents/update_documents permissions -- see
    # permissions.can_view_staff_document/can_update_staff_document, which
    # check this flag FIRST and never fall through to the general logic
    # (no Company Admin fallback, no ordinary StaffDocumentGrant) for a
    # category marked this way. Always SCOPE_PRIVATE (enforced at creation,
    # see views.staff_documents.StaffDocumentCategoryViewSet.create) and,
    # like `scope` itself, never editable after creation (no code path does
    # so -- partial_update only ever toggles `is_active`).
    is_sst = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["company_profile_id", "name"]

    def __str__(self):
        return f"{self.company_profile_id}: {self.name}"


class StaffDocument(models.Model):
    """A single "document slot" for one collaborator -- e.g. "national ID
    card" or "passport", both possibly filed under the SAME category without
    being confused for versions of each other. `title` distinguishes several
    documents of the same category (never merged automatically); `category`
    is fixed at creation (changing it would be "this is actually a different
    document", not a metadata correction) -- deliberately NOT user-editable
    after creation, unlike `title` (see update_staff_document_title in
    services/staff_documents.py, audited via StaffDocumentEvent).
    SHARED-scope documents (category.scope == SHARED) have `staff=None` --
    they belong to the COMPANY, not to one individual; PRIVATE-scope
    documents always have a `staff` set. Enforced in `clean()`, not a DB
    constraint (same convention as project_management.models.document.
    Folder's scope exclusivity)."""

    staff = models.ForeignKey(Staff, on_delete=models.CASCADE, related_name="documents", null=True, blank=True)
    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="shared_staff_documents")
    category = models.ForeignKey(StaffDocumentCategory, on_delete=models.PROTECT, related_name="documents")
    # LOT L04 (SST): optional -- only ever set for a document created through
    # the SST module (category.is_sst=True), to assemble a case's dossier.
    # Never set/read by anything outside human_resource_management.services.sst.
    sst_case = models.ForeignKey(
        "human_resource_management.SSTCase", on_delete=models.SET_NULL, null=True, blank=True, related_name="documents",
    )
    title = models.CharField(max_length=255)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_id", "category_id", "id"]

    def clean(self):
        if self.category_id and self.category.scope == StaffDocumentCategory.SCOPE_SHARED and self.staff_id:
            raise ValidationError("A SHARED-scope document cannot be attached to one individual staff member.")
        if self.category_id and self.category.scope == StaffDocumentCategory.SCOPE_PRIVATE and not self.staff_id:
            raise ValidationError("A PRIVATE-scope document must be attached to a staff member.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} ({self.staff_id or self.company_profile_id})"


def _staff_document_upload_path(instance, filename):
    # Deliberately not "staff_<id>/<category>/<filename>" (guessable,
    # enumerable path) -- a random component, exactly the same convention as
    # leave_attachment_upload_path. The path is still never served directly:
    # only StaffDocumentFileViewSet.download ever reads it, after its own
    # permission check, never a raw URL returned to any client.
    company_id = instance.staff_document.company_profile_id
    return f"company_{company_id}/staff_document_{instance.staff_document_id}/{uuid.uuid4().hex}_{filename}"


class StaffDocumentFileQuerySet(models.QuerySet):
    """Mirrors project_management.models.document_revision.
    DocumentRevisionQuerySet: a stored file version is evidence, never
    rewritten or deleted, at the queryset level (not just by convention)."""

    def update(self, **kwargs):
        raise ValidationError("Stored document file versions cannot be modified.")

    def delete(self):
        raise ValidationError("Stored document file versions cannot be deleted.")


class StaffDocumentFile(models.Model):
    """One IMMUTABLE version of a StaffDocument -- a renewal is simply a new
    row with a new `version_number`, never an edit of an existing one.
    `version_number` is protected against concurrent uploads for the SAME
    StaffDocument by a UniqueConstraint (fields=["staff_document",
    "version_number"]) -- a race between two concurrent uploads is resolved
    the same way L01-B1 resolves a concurrent Staff-provisioning race: the
    DB constraint rejects the loser, the service layer (see
    services.staff_documents.create_staff_document_file) retries once with
    the next available number rather than surfacing a raw IntegrityError.
    `valid_from`/`valid_until` live on the FILE VERSION, not on the
    StaffDocument shell -- a renewal (a NEW file with new validity dates) is
    the only "renewal" concept that exists; there is no separate renewal
    model. A NEW version always starts back at `submitted` (see
    StaffDocumentFileStatusEvent) -- a prior version's `accepted` status is
    NEVER carried over."""

    objects = StaffDocumentFileQuerySet.as_manager()

    staff_document = models.ForeignKey(StaffDocument, on_delete=models.CASCADE, related_name="files")
    version_number = models.PositiveIntegerField()
    file = models.FileField(upload_to=_staff_document_upload_path, storage=staff_document_storage, max_length=500)
    file_name = models.CharField(max_length=255)
    file_size = models.PositiveIntegerField(null=True, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True, default="")
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_document_id", "-version_number"]
        constraints = [
            models.UniqueConstraint(fields=["staff_document", "version_number"], name="unique_staff_document_file_version"),
        ]

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise ValidationError("Stored document file versions cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Stored document file versions cannot be deleted.")

    def __str__(self):
        return f"{self.staff_document_id} v{self.version_number}"


class StaffDocumentFileStatusEvent(models.Model):
    """Append-only status transition log for ONE immutable StaffDocumentFile
    -- "transitions historisees separement du fichier immuable". The
    CURRENT status of a file is always resolved as its LATEST event here
    (services.staff_documents.resolve_file_status), never a mutable field on
    StaffDocumentFile itself. A new file version always starts with its own
    fresh SUBMITTED event -- never inherits a prior version's ACCEPTED
    status."""

    STATUS_SUBMITTED = "submitted"
    STATUS_VERIFIED = "verified"
    STATUS_ACCEPTED = "accepted"
    STATUS_CHOICES = (
        (STATUS_SUBMITTED, "Submitted"),
        (STATUS_VERIFIED, "Verified"),
        (STATUS_ACCEPTED, "Accepted"),
    )

    file = models.ForeignKey(StaffDocumentFile, on_delete=models.CASCADE, related_name="status_events")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    note = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["file_id", "created_at"]

    def __str__(self):
        return f"{self.file_id}: {self.status}"


class StaffDocumentGrant(models.Model):
    """An explicit, per-document, READ-ONLY access grant to a named user of
    the SAME company -- "autres intervenants explicitement autorises". Never
    an arbitrary external account (enforced in
    services.staff_documents.grant_staff_document_access, which requires the
    grantee to already belong to the same company -- same `_same_company`-
    style check used throughout this project). Revocation is a field update
    (`revoked_at`/`revoked_by`), never a delete -- the grant's own history
    (who granted/revoked it, when) is itself the audit trail, no separate
    event table needed for this one."""

    staff_document = models.ForeignKey(StaffDocument, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="staff_document_grants")
    granted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)
    revoked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["staff_document_id", "-granted_at"]

    @property
    def is_active(self):
        return self.revoked_at is None

    def __str__(self):
        status = "active" if self.is_active else "revoked"
        return f"{self.staff_document_id} -> {self.user_id} ({status})"


class StaffDocumentEvent(models.Model):
    """Append-only audit trail for the "useful metadata changes" that aren't
    already covered by StaffDocumentFile (uploads) or
    StaffDocumentFileStatusEvent (status transitions) -- title renames and
    grant creation/revocation. Never rewritten."""

    EVENT_TITLE_CHANGED = "title_changed"
    EVENT_GRANT_CREATED = "grant_created"
    EVENT_GRANT_REVOKED = "grant_revoked"
    EVENT_CHOICES = (
        (EVENT_TITLE_CHANGED, "Title changed"),
        (EVENT_GRANT_CREATED, "Grant created"),
        (EVENT_GRANT_REVOKED, "Grant revoked"),
    )

    staff_document = models.ForeignKey(StaffDocument, on_delete=models.CASCADE, related_name="events")
    event_type = models.CharField(max_length=24, choices=EVENT_CHOICES)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    detail = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["staff_document_id", "created_at"]

    def __str__(self):
        return f"{self.staff_document_id}: {self.event_type}"
