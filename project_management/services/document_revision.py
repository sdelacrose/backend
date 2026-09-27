import hashlib
import posixpath

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max

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
    """Stored versions of a document, newest first (deterministic: by version number)."""
    return (
        DocumentRevision.objects.filter(document=document)
        .select_related("created_by")
        .order_by("-version_number", "-id")
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


# --- File evidence (SHA-256) -----------------------------------------------------

def sha256_of_file(file_obj):
    """Lowercase hex SHA-256 of the EXACT raw bytes of a file object (upload or stored file).

    Streams in chunks; leaves an upload rewound so it can still be stored afterwards.
    """
    digest = hashlib.sha256()
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    for chunk in file_obj.chunks():
        digest.update(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    return digest.hexdigest()


def _hash_of_stored_version(revision):
    """SHA-256 of a stored Version file (legacy versions approved after the hash existed)."""
    try:
        revision.file.open("rb")
        try:
            return sha256_of_file(revision.file)
        finally:
            revision.file.close()
    except Exception as error:
        raise ValidationError(
            {
                "file": (
                    "The stored file of this version cannot be read, so the approval "
                    "evidence cannot be recorded."
                )
            }
        ) from error


# --- Writes ----------------------------------------------------------------------

def _title_for_new_file(document, revision, previous_file=""):
    """Keep the document title stable across versions.

    Only the extension follows the new file (the viewer/download pick the renderer
    and the saved name from it), e.g. plan.docx + a PDF upload -> plan.pdf. A title is
    never cut at an arbitrary dot ("Plan v1.2" + PDF -> "Plan v1.2.pdf"), and an empty
    title (first file of a register entry) takes the uploaded file name.
    """
    previous_file = previous_file or ""
    current = document.file_name or (
        posixpath.basename(previous_file) if previous_file else ""
    )
    new_name = revision.file_name or ""
    if not current:
        return new_name
    new_ext = posixpath.splitext(new_name)[1]
    if not new_ext or current.lower().endswith(new_ext.lower()):
        return current
    previous_ext = posixpath.splitext(previous_file)[1]
    if previous_ext and current.lower().endswith(previous_ext.lower()):
        return f"{current[: -len(previous_ext)]}{new_ext}"
    return f"{current}{new_ext}"


def _next_version_number(document):
    """Next sequential VERSION number of a document (caller holds the document lock)."""
    return (
        DocumentRevision.objects.filter(document=document).aggregate(
            m=Max("version_number")
        )["m"]
        or 0
    ) + 1


def next_revision_label(existing_labels, start="A"):
    """Next REVISION label: the configured start, then A -> B -> ... -> Z -> AA (or 0 -> 1 -> 2).

    Labels already assigned are respected (the sequence continues after the highest one).
    """
    labels = [label for label in existing_labels if label]
    if not labels:
        return start
    return increment_major(max(labels, key=lambda label: (len(label), label)))


def assign_revision_label(revision):
    """Give an internally approved version its official revision label, once.

    Serialized on the document row; an already assigned label is never changed.
    The DocumentRevision row itself stays immutable (only this controlled update).
    """
    with transaction.atomic():
        Document.objects.select_for_update().get(pk=revision.document_id)
        current = DocumentRevision.objects.get(pk=revision.pk)
        if current.revision_label:
            revision.revision_label = current.revision_label
            return current.revision_label
        from project_management.services.document_control_settings import (
            get_document_control_settings,
        )

        settings = get_document_control_settings(current.document)["revision"]
        label = next_revision_label(
            DocumentRevision.objects.filter(
                document_id=revision.document_id, revision_label__isnull=False
            ).values_list("revision_label", flat=True),
            start=settings["start"],
        )
        DocumentRevision.objects.filter(
            pk=revision.pk, revision_label__isnull=True
        ).update(revision_label=label)
        revision.revision_label = label
        return label


def _snapshot_project(document):
    project = document.folder.project if document.folder_id else None
    if project is None:
        return None
    return {
        "id": project.pk,
        "name": project.project_name,
        "code": project.project_code or None,
    }


def build_approval_snapshot(revision, workflow):
    """Approval-time metadata evidence of ONE Version (JSON, human-interpretable).

    Real current model values only. The workflow actors / events / comments are NOT
    duplicated: they stay in the protected workflow records (referenced by id).
    """
    document = revision.document
    issuer = document.external_issuer_company
    return {
        "schema_version": 1,
        "document": {
            "id": document.pk,
            "reference": document.full_document_number,
            "title": document.file_name,
            "document_type": document.document_type,
            "discipline": {
                "group_id": document.discipline_group_id,
                "group": document.discipline_group,
                "option_id": document.discipline_option_id,
                "label": document.discipline,
                "code": document.discipline_code,
            },
            "origin": {
                "value": document.document_origin,
                "label": document.get_document_origin_display(),
            },
            "external_issuer": (
                {"id": issuer.pk, "name": issuer.company_name} if issuer else None
            ),
            "description": document.description,
            "tags": list(document.tags or []),
            "business_status": document.status,
        },
        "project": _snapshot_project(document),
        "version": {
            "id": revision.pk,
            "number": revision.version_number,
            "revision_label": revision.revision_label,
        },
        "file": {
            "name": revision.file_name,
            "path": revision.file.name,
            "size": revision.file_size,
            "sha256": revision.file_sha256,
        },
        "workflow": {
            "id": workflow.pk,
            "type": workflow.workflow_type,
            "approved_at": workflow.completed_at.isoformat()
            if workflow.completed_at
            else None,
        },
    }


def record_approval_evidence(revision, workflow):
    """Approval evidence, in the SAME transaction as the approval, in this order:

    1. the official revision label (assign_revision_label, already done by the caller),
    2. the file SHA-256 (legacy versions: computed now, approval fails if unreadable),
    3. the approval snapshot, which therefore carries THAT label and hash.

    Each value is written once; an existing one is never replaced.
    """
    with transaction.atomic():
        Document.objects.select_for_update().get(pk=revision.document_id)
        current = DocumentRevision.objects.select_related(
            "document", "document__folder", "document__external_issuer_company"
        ).get(pk=revision.pk)
        if not current.revision_label:
            raise ValidationError("The official revision label is missing.")
        if not current.file_sha256:
            digest = _hash_of_stored_version(current)
            DocumentRevision.objects.filter(pk=current.pk).update(file_sha256=digest)
            current.file_sha256 = digest
        if current.approval_snapshot is None:
            snapshot = build_approval_snapshot(current, workflow)
            DocumentRevision.objects.filter(pk=current.pk).update(
                approval_snapshot=snapshot
            )
            current.approval_snapshot = snapshot
        revision.file_sha256 = current.file_sha256
        revision.approval_snapshot = current.approval_snapshot
        return current


def _materialize_first_revision(locked):
    """First real revision of a legacy document (caller holds the document lock).

    The current Document.file is snapshotted as-is (same stored path, nothing is
    copied). Only called by operations that really need a revision; never on read.
    """
    if DocumentRevision.objects.filter(document=locked).exists() or not locked.file:
        return
    legacy = DocumentRevision(
        document=locked,
        version_number=_next_version_number(locked),
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
            # First stored version of a register entry without a file (nothing to
            # materialize): legacy compatibility label 0-00 (never the official Revision).
            major, minor = _legacy_major(locked), 0

        revision = DocumentRevision(
            document=locked,
            version_number=_next_version_number(locked),
            major_revision=major,
            minor_revision=minor,
            file_name=posixpath.basename(getattr(uploaded_file, "name", "") or ""),
            file_size=getattr(uploaded_file, "size", None),
            # Calculated from the upload itself (no download from storage), before it is stored.
            file_sha256=sha256_of_file(uploaded_file),
            comment=comment,
            created_by=user,
        )
        revision.file = uploaded_file
        try:
            with transaction.atomic():
                revision.save()
        except IntegrityError:
            raise ValidationError("This revision already exists.")

        previous_file = locked.file.name if locked.file else ""
        locked.file = revision.file.name
        locked.file_name = _title_for_new_file(locked, revision, previous_file)
        # Document.status (business status) is deliberately left untouched.
        fields = ["file", "file_name", "updated_at"]
        if major.isdigit() and int(major) <= 99:
            # Keep the integer shown by the Register in step with numeric majors.
            locked.revision = int(major)
            fields.append("revision")
        locked.save(update_fields=fields)
        return revision


def is_first_main_file_upload(document):
    """A register entry with no controlled file yet: no stored version and no Document.file."""
    return not document.file and not DocumentRevision.objects.filter(
        document=document
    ).exists()


def create_document_revision(document, uploaded_file, user, revision_type, comment):
    """Validated entry point used by the API: new file + reason are mandatory.

    Exception: the FIRST main file of a document that has none (TO_CREATE) only needs the
    file; the version type and the comment may be omitted.
    """
    if is_first_main_file_upload(document):
        if uploaded_file is None:
            raise ValidationError({"file": "A new file is required."})
        return add_document_revision(
            document,
            uploaded_file,
            user,
            revision_type=MINOR,
            comment=(comment or "").strip(),
        )
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
