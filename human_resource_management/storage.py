"""Dedicated, non-public storage zone for LeaveRequestAttachment (LOT RH4
closure, item 3). A UUID-prefixed path under the ordinary MEDIA_ROOT is not
actually private: this project serves `MEDIA_URL`/`MEDIA_ROOT` through
Django's own `static()` helper whenever DEBUG=True (every environment that
uses FileSystemStorage here runs with DEBUG=True), and in S3-backed
environments `AWS_S3_CUSTOM_DOMAIN` makes `.url()` return a stable, unsigned,
public link for every upload. Either way, anyone who learns or guesses the
path can fetch the file directly, bypassing the authenticated `download`
action's owner/HR/company checks entirely -- confirmed with a plain
unauthenticated `curl` against a real uploaded attachment before this fix.

This reuses the SAME backend this environment's `default` storage already
uses (FileSystemStorage locally, S3Boto3Storage in development/staging/
production) -- not a new storage system -- but points it at a location the
public media tree never exposes, and nothing in this codebase ever calls
`.url()` on it (the attachments view always streams bytes through its own
authenticated `download` action), so there is no public URL to leak even by
accident.
"""
from pathlib import Path

from django.conf import settings


def _build_private_leave_attachment_storage():
    default_backend = settings.STORAGES["default"]["BACKEND"]

    if default_backend.endswith("S3Boto3Storage"):
        from storages.backends.s3boto3 import S3Boto3Storage

        class PrivateLeaveAttachmentS3Storage(S3Boto3Storage):
            # Separate key prefix from every other upload in this codebase,
            # explicit private ACL (never inherited from a bucket-wide
            # public-read default), and no custom domain -- so `.url()`,
            # if anything ever called it, falls back to boto3's normal
            # query-string-signed, time-limited URL instead of the stable
            # public link every other document in this codebase gets.
            location = "private/hr_attachments"
            default_acl = "private"
            custom_domain = None
            querystring_auth = True
            file_overwrite = False

        return PrivateLeaveAttachmentS3Storage()

    # FileSystemStorage: a sibling directory OUTSIDE MEDIA_ROOT, with no
    # base_url -- the public `static(MEDIA_URL, document_root=MEDIA_ROOT)`
    # urlpattern can never resolve a path into it.
    from django.core.files.storage import FileSystemStorage

    private_root = Path(settings.BASE_DIR) / "private_media" / "hr_attachments"
    return FileSystemStorage(location=str(private_root), base_url=None)


leave_attachment_storage = _build_private_leave_attachment_storage()


def _build_private_staff_document_storage():
    """L03/L05: the SAME private-storage approach as
    `leave_attachment_storage` above (see its module docstring for the full
    rationale), own separate location/prefix. Private HR documents (identity
    pieces, contracts...) and shared-but-still-internal HR documents are at
    LEAST as sensitive as a leave-request supporting document -- they must
    never go through the generic, unsigned `Document`/`Folder` storage
    either, for the exact same reason."""
    default_backend = settings.STORAGES["default"]["BACKEND"]

    if default_backend.endswith("S3Boto3Storage"):
        from storages.backends.s3boto3 import S3Boto3Storage

        class PrivateStaffDocumentS3Storage(S3Boto3Storage):
            location = "private/staff_documents"
            default_acl = "private"
            custom_domain = None
            querystring_auth = True
            file_overwrite = False

        return PrivateStaffDocumentS3Storage()

    from django.core.files.storage import FileSystemStorage

    private_root = Path(settings.BASE_DIR) / "private_media" / "staff_documents"
    return FileSystemStorage(location=str(private_root), base_url=None)


staff_document_storage = _build_private_staff_document_storage()


# HR-0036: models reference these callables (not the instances) so that migrations serialise a stable
# import path instead of the environment-specific location/backend resolved at import time.
def get_leave_attachment_storage():
    return leave_attachment_storage


def get_staff_document_storage():
    return staff_document_storage
