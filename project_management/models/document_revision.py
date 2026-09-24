import posixpath

from django.core.exceptions import ValidationError
from django.db import models

from models.models import User

from .document import Document, get_upload_path


def get_revision_upload_path(instance, filename):
    """Store every version in its own folder so a file is never overwritten.

    Follows the Documents storage convention, next to the document's folder:
    <document folder path>/revisions/document_<id>/<major>-<minor>/<filename>
    """
    base = posixpath.dirname(get_upload_path(instance.document, "_"))
    return posixpath.join(
        base,
        "revisions",
        f"document_{instance.document_id}",
        f"{instance.major_revision}-{instance.minor_revision:02d}",
        filename,
    )


class DocumentRevision(models.Model):
    """One immutable stored version (major revision + minor version) of a Document.

    The Document stays the single permanent identity (and reference); this table
    only preserves the files over time. Existing documents have no rows until
    their first real revision is created (see services.document_revision).
    """

    document = models.ForeignKey(
        Document, related_name="revisions", on_delete=models.CASCADE
    )
    major_revision = models.CharField(max_length=20)
    minor_revision = models.PositiveIntegerField(default=0)
    file = models.FileField(upload_to=get_revision_upload_path, max_length=500)
    file_name = models.CharField(max_length=500, null=True, blank=True)
    file_size = models.BigIntegerField(null=True, blank=True)
    comment = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        User,
        related_name="created_document_revisions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "major_revision", "minor_revision"],
                name="document_revision_unique_version",
            )
        ]

    @property
    def label(self):
        return f"{self.major_revision}-{self.minor_revision:02d}"

    def save(self, *args, **kwargs):
        # Stored versions are immutable: a correction is a new minor version.
        if not self._state.adding:
            raise ValidationError("A stored document revision cannot be modified.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.document_id} {self.label}"
