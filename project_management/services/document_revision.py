import posixpath

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from project_management.models import Document, DocumentRevision
from project_management.services.document_workflow import (
    assert_document_file_unlocked,
)

MINOR = "minor"
MAJOR = "major"
REVISION_TYPES = (MINOR, MAJOR)


# --- Temporary default numbering -------------------------------------------------
# Major: alphabetic (A -> B -> ... -> Z -> AA); a purely numeric major (legacy
# Document.revision integers such as "0") keeps counting numerically.
# Minor: 00 -> 01 -> 02. Everything numbering-related lives here so it can later be
# replaced by a project/company configuration.

def increment_major(major):
    if major.isdigit():
        return str(int(major) + 1)
    if major.isalpha() and major.isupper():
        letters = list(major)
        index = len(letters) - 1
        while index >= 0:
            if letters[index] == "Z":
                letters[index] = "A"
                index -= 1
            else:
                letters[index] = chr(ord(letters[index]) + 1)
                return "".join(letters)
        return "A" + "".join(letters)
    raise ValidationError(f"Unsupported revision sequence: {major!r}.")


def next_version(major, minor, revision_type):
    """(major, minor) of the version that follows `major-minor`."""
    if revision_type == MAJOR:
        return increment_major(major), 0
    return major, minor + 1


def format_version(major, minor):
    return f"{major}-{minor:02d}"


# --- Reads -----------------------------------------------------------------------

def _stored_size(file_field):
    try:
        return file_field.size
    except (OSError, ValueError):
        return None


def _legacy_major(document):
    return str(document.revision if document.revision is not None else 0)


def get_revision_history(document):
    """Stored versions of a document, newest first."""
    return DocumentRevision.objects.filter(document=document).select_related(
        "created_by"
    )


def get_next_versions(document):
    """Labels of the next minor / major version (for the UI), from the current one."""
    latest = get_revision_history(document).first()
    major = latest.major_revision if latest else _legacy_major(document)
    minor = latest.minor_revision if latest else 0
    try:
        return {
            MINOR: format_version(*next_version(major, minor, MINOR)),
            MAJOR: format_version(*next_version(major, minor, MAJOR)),
        }
    except ValidationError:
        return {MINOR: None, MAJOR: None}


# --- Writes ----------------------------------------------------------------------

def _title_for_new_file(document, revision):
    """Keep the document title stable across versions.

    Only the extension follows the new file (the viewer/download pick the renderer
    and the saved name from it), e.g. plan.docx + a PDF upload -> plan.pdf.
    """
    current = document.file_name or posixpath.basename(document.file.name)
    new_ext = posixpath.splitext(revision.file_name or "")[1]
    stem, ext = posixpath.splitext(current)
    if not new_ext or new_ext.lower() == ext.lower():
        return current
    return f"{stem}{new_ext}"


def _materialize_first_revision(locked):
    """First real revision of a legacy document (caller holds the document lock).

    The current Document.file is snapshotted as-is (same stored path, nothing is
    copied). Only called by operations that really need a revision; never on read.
    """
    if DocumentRevision.objects.filter(document=locked).exists() or not locked.file:
        return
    legacy = DocumentRevision(
        document=locked,
        major_revision=_legacy_major(locked),
        minor_revision=0,
        file_name=locked.file_name or posixpath.basename(locked.file.name),
        created_by=locked.created_by,
    )
    legacy.file = locked.file.name
    legacy.file_size = _stored_size(locked.file)
    legacy.save()


def ensure_current_revision(document):
    """Current (latest) revision of a document, materializing the first one if needed."""
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        _materialize_first_revision(locked)
        return (
            DocumentRevision.objects.filter(document=locked)
            .order_by("-created_at", "-id")
            .first()
        )


def add_document_revision(
    document,
    uploaded_file,
    user,
    major_revision=None,
    revision_type=MINOR,
    comment="",
):
    """Store a new immutable version of `document` and make it the current file.

    - `revision_type` minor: A-00 -> A-01; major: A-01 -> B-00. An explicit
      `major_revision` starts/continues that major instead.
    - Existing documents are not backfilled: their current Document.file is only
      snapshotted (same stored path, nothing copied) the first time a real
      revision is created, so it stays retrievable.
    - Previous files are never overwritten or deleted.
    Approval rules for a new major revision belong to the future workflow lot and
    are intentionally not enforced here.
    """
    with transaction.atomic():
        # Serialize concurrent version numbering for one document.
        locked = Document.objects.select_for_update().get(pk=document.pk)
        # The current revision/file cannot change while its workflow is running.
        assert_document_file_unlocked(locked)
        revisions = DocumentRevision.objects.filter(document=locked)

        _materialize_first_revision(locked)

        latest = revisions.order_by("-created_at", "-id").first()
        if major_revision:
            last_minor = (
                revisions.filter(major_revision=major_revision)
                .order_by("-minor_revision")
                .values_list("minor_revision", flat=True)
                .first()
            )
            major = major_revision
            minor = 0 if last_minor is None else last_minor + 1
        elif latest:
            major, minor = next_version(
                latest.major_revision, latest.minor_revision, revision_type
            )
        else:
            major, minor = next_version(_legacy_major(locked), 0, revision_type)

        revision = DocumentRevision(
            document=locked,
            major_revision=major,
            minor_revision=minor,
            file_name=posixpath.basename(getattr(uploaded_file, "name", "") or ""),
            file_size=getattr(uploaded_file, "size", None),
            comment=comment,
            created_by=user,
        )
        revision.file = uploaded_file
        try:
            with transaction.atomic():
                revision.save()
        except IntegrityError:
            raise ValidationError("This revision already exists.")

        locked.file = revision.file.name
        locked.file_name = _title_for_new_file(locked, revision)
        fields = ["file", "file_name", "updated_at"]
        if major.isdigit() and int(major) <= 99:
            # Keep the integer shown by the Register in step with numeric majors.
            locked.revision = int(major)
            fields.append("revision")
        locked.save(update_fields=fields)
        return revision


def create_document_revision(document, uploaded_file, user, revision_type, comment):
    """Validated entry point used by the API: new file + reason are mandatory."""
    errors = {}
    if revision_type not in REVISION_TYPES:
        errors["revision_type"] = "Must be 'minor' or 'major'."
    if uploaded_file is None:
        errors["file"] = "A new file is required."
    comment = (comment or "").strip()
    if not comment:
        errors["comment"] = "A comment (reason for the revision) is required."
    if errors:
        raise ValidationError(errors)
    return add_document_revision(
        document,
        uploaded_file,
        user,
        revision_type=revision_type,
        comment=comment,
    )
