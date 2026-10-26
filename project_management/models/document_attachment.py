import posixpath

from django.db import models

from models.models import User

from .document import Document, get_upload_path


def get_attachment_upload_path(instance, filename):
    """Supporting files live next to the document, apart from its stored versions.

    <document folder path>/attachments/document_<id>/<filename>; the storage never
    overwrites an existing file (a same-named upload gets a unique stored name).
    """
    base = posixpath.dirname(get_upload_path(instance.document, "_"))
    return posixpath.join(
        base, "attachments", f"document_{instance.document_id}", filename
    )


class DocumentAttachment(models.Model):
    """A supporting file (annex) of a Document.

    It belongs to the Document identity only: no version, revision, workflow or approval,
    and it never touches the controlled main file (Document.file / DocumentRevision).
    """

    document = models.ForeignKey(
        Document, related_name="supporting_attachments", on_delete=models.CASCADE
    )
    file = models.FileField(upload_to=get_attachment_upload_path, max_length=500)
    file_name = models.CharField(max_length=500)
    file_size = models.BigIntegerField(null=True, blank=True)
    uploaded_by = models.ForeignKey(
        User,
        related_name="uploaded_document_attachments",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
