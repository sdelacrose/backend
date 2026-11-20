"""GED-SEC.08.2-B0.2 / B0.3: the one server-side policy of every upload that ends up in S3.

Pure functions, no AWS import: what may be uploaded (per usage), how an object is named, the signed ticket that ties a
prepared upload to the user who asked for it, and the grammar of the keys that prove an upload was finalised.

Layout of the bucket (nothing is chosen by the browser):
  pending : uploads/pending/<env>/<usage>/<uuid>.<ext>                         one prefix, meant to be expired by a
                                                                                lifecycle rule, never read by anybody
  final   : uploads/<public|private>/<env>/<usage>/<owner>/[project_N/]<uuid>.<ext>
            <owner> is company_N or user_N. A final key exists only if a finalisation succeeded (the object is
            written there by the server, after its checks, never by the browser), so the existence of a final key IS
            the proof of finalisation that a business endpoint needs, without any table.

The content type declared by a browser proves nothing about the bytes, so the policy also knows how to recognise a file
from its first bytes (`content_matches_extension`).
"""
import posixpath
import re
import unicodedata
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.core import signing

PRESIGN_SECONDS = 300  # a prepared upload can be started for five minutes only
READ_SECONDS = 300  # a private file is read through a link valid five minutes
TICKET_MAX_AGE_SECONDS = 3600  # ... and finalised within one hour
# Proposed lifecycle (to be configured by the AWS owner, NOT done here): expire `uploads/pending/` after one day, a
# comfortable margin over the ticket life. Nothing finalised ever lives under that prefix.
PENDING_EXPIRY_DAYS_PROPOSED = 1
TICKET_SALT = "links.secure-upload.v1"
MAX_FILENAME_LENGTH = 200
SNIFF_BYTES = 2048
PENDING = "pending"
PUBLIC, PRIVATE = "public", "private"

ALLOWED_REQUEST_FIELDS = frozenset(
    {"usage", "company_id", "project_id", "user_id", "filename", "content_type", "size_bytes"}
)


class UploadRejected(Exception):
    """A request refused by the policy: `status_code` and a message that never contains a path or a provider error."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.public_message = message
        self.status_code = status_code


# extension -> (canonical content type, other content types a browser may declare for it, may be shown inline)
_JPEG = ("image/jpeg", ("image/jpg", "image/pjpeg"), True)
FORMATS = {
    "jpg": _JPEG,
    "jpeg": _JPEG,
    "png": ("image/png", ("image/x-png",), True),
    "webp": ("image/webp", (), True),
    "gif": ("image/gif", (), True),
    "pdf": ("application/pdf", (), True),
    "txt": ("text/plain", (), False),
    "csv": ("text/csv", ("application/csv", "application/vnd.ms-excel", "text/plain"), False),
    "tsv": ("text/tab-separated-values", ("text/plain",), False),
    "json": ("application/json", ("text/json",), False),
    "xml": ("application/xml", ("text/xml",), False),
    "rtf": ("application/rtf", ("text/rtf",), False),
    "doc": ("application/msword", (), False),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", (), False),
    "xls": ("application/vnd.ms-excel", (), False),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", (), False),
    "ppt": ("application/vnd.ms-powerpoint", (), False),
    "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", (), False),
    "odt": ("application/vnd.oasis.opendocument.text", (), False),
    "ods": ("application/vnd.oasis.opendocument.spreadsheet", (), False),
    "odp": ("application/vnd.oasis.opendocument.presentation", (), False),
    "msg": ("application/vnd.ms-outlook", ("application/octet-stream",), False),
}

RASTER_IMAGES = frozenset({"jpg", "jpeg", "png", "webp", "gif"})
DOCUMENTS = frozenset(
    {"pdf", "txt", "csv", "tsv", "json", "xml", "rtf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "msg"}
)

# Active content is never accepted, whatever the usage and whatever the declared type, and also when it hides in an
# inner part of the name ("report.php.jpg").
DANGEROUS_EXTENSIONS = frozenset(
    {
        "php", "phtml", "phar", "asp", "aspx", "jsp", "cgi", "pl", "py", "rb", "js", "mjs", "cjs", "html", "htm", "xhtml",
        "shtml", "svg", "svgz", "swf", "exe", "dll", "bat", "cmd", "com", "scr", "msi", "jar", "sh", "bash", "ps1", "vbs",
        "vbe", "wsf", "hta", "lnk", "apk", "dmg", "iso", "app",
    }
)

MB = 1024 * 1024


@dataclass(frozen=True)
class UsagePolicy:
    name: str
    extensions: frozenset
    max_bytes: int
    # "project_required" | "project_optional" | "company" | "self" | "authenticated"
    scope: str
    # False: reachable only through the endpoint that already owns its authorisation (never through "prepare")
    prepare_allowed: bool = True
    # PUBLIC: meant to be displayed by anybody who has its URL (a logo); PRIVATE: read only through an authorised,
    # short-lived link, never through a permanent public URL
    visibility: str = PRIVATE


USAGES = {
    # an image chosen when creating or editing a project (the project does not exist yet on creation)
    "project_image": UsagePolicy("project_image", RASTER_IMAGES, 5 * MB, "project_optional", visibility=PUBLIC),
    # the logo of a company of the contact directory (NOT the user's own company profile)
    "contact_company_logo": UsagePolicy("contact_company_logo", RASTER_IMAGES, 2 * MB, "company", visibility=PUBLIC),
    "user_avatar": UsagePolicy("user_avatar", RASTER_IMAGES, 2 * MB, "self", visibility=PUBLIC),
    # a feedback belongs to its author: the attachment is bound to that user, not to the company
    "feedback_attachment": UsagePolicy("feedback_attachment", RASTER_IMAGES | {"pdf"}, 10 * MB, "self"),
    # the observation form has always accepted images and PDF documents (the screen lists "Jpg, Png or Pdf")
    "site_report_photo": UsagePolicy("site_report_photo", RASTER_IMAGES | {"pdf"}, 10 * MB, "project_required"),
    "object_assessment_file": UsagePolicy("object_assessment_file", RASTER_IMAGES | DOCUMENTS, 25 * MB, "project_required"),
    # the historical multipart endpoint `projects/upload_file_to_s3` (Object Assessment "Documents" tab): the server
    # receives the file itself, and the endpoint keeps its own permission as the minimum. The object is only a preview
    # helper: it lives under the pending prefix and expires.
    "legacy_multipart": UsagePolicy(
        "legacy_multipart", RASTER_IMAGES | DOCUMENTS, 25 * MB, "authenticated", prepare_allowed=False
    ),
}


def usage_policy(name, *, for_prepare=True):
    """The policy of a usage; an unknown OR not yet supported usage is one and the same refusal."""
    policy = USAGES.get(name) if isinstance(name, str) else None
    if policy is None or (for_prepare and not policy.prepare_allowed):
        raise UploadRejected("This upload is not supported.")
    return policy


def max_bytes_for(policy):
    """The size limit of a usage, overridable per environment through `SECURE_UPLOAD_MAX_BYTES` ({usage: bytes})."""
    overrides = getattr(settings, "SECURE_UPLOAD_MAX_BYTES", None) or {}
    value = overrides.get(policy.name, policy.max_bytes)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else policy.max_bytes


def reject_unexpected_fields(payload):
    """The browser never chooses a bucket, a key, a prefix, an ACL, a URL or credentials: any field outside the
    contract is refused instead of being ignored, so that an attempt is visible rather than silent."""
    if not hasattr(payload, "keys"):
        raise UploadRejected("Invalid request.")
    if set(payload.keys()) - ALLOWED_REQUEST_FIELDS:
        raise UploadRejected("Invalid request.")


def validate_filename(filename):
    """Return the lower-case extension of a safe file name, or refuse. The name is never used as a path or as the
    object name; this only decides whether the declared file is acceptable."""
    if not isinstance(filename, str) or not filename or len(filename) > MAX_FILENAME_LENGTH:
        raise UploadRejected("Invalid file name.")
    if filename != filename.strip() or filename.startswith(".") or filename.endswith("."):
        raise UploadRejected("Invalid file name.")
    if "/" in filename or "\\" in filename or ":" in filename or ".." in filename:
        raise UploadRejected("Invalid file name.")
    if any(unicodedata.category(char).startswith("C") for char in filename):  # control, format (bidi), unassigned
        raise UploadRejected("Invalid file name.")
    parts = filename.lower().split(".")
    if len(parts) < 2 or not parts[-1]:
        raise UploadRejected("This file type is not allowed.")
    if any(part in DANGEROUS_EXTENSIONS for part in parts[1:]):
        raise UploadRejected("This file type is not allowed.")
    return parts[-1]


def validate_upload_request(policy, filename, content_type, size_bytes):
    """Check the declared file against the usage. Returns (extension, canonical content type, disposition, size)."""
    extension = validate_filename(filename)
    if extension not in policy.extensions or extension not in FORMATS:
        raise UploadRejected("This file type is not allowed.")
    canonical, aliases, inline_safe = FORMATS[extension]
    declared = content_type.split(";")[0].strip().lower() if isinstance(content_type, str) else ""
    if declared not in (canonical,) + tuple(aliases):
        raise UploadRejected("The content type does not match the file type.")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int):
        if isinstance(size_bytes, str) and size_bytes.isdigit():
            size_bytes = int(size_bytes)
        else:
            raise UploadRejected("Invalid file size.")
    if size_bytes < 1:
        raise UploadRejected("Invalid file size.")
    if size_bytes > max_bytes_for(policy):
        raise UploadRejected("The file is too large.", status_code=413)
    return extension, canonical, ("inline" if inline_safe else "attachment"), size_bytes


# --- keys -------------------------------------------------------------------------------------------------------------
def _root():
    return str(getattr(settings, "SECURE_UPLOAD_KEY_ROOT", "uploads")).strip("/") or "uploads"


def _environment_segment():
    value = re.sub(r"[^a-z0-9-]", "", str(getattr(settings, "ENV", "local")).lower()) or "local"
    # an environment can never be mistaken for a visibility or for the pending prefix
    return f"env-{value}" if value in (PENDING, PUBLIC, PRIVATE) else value


def pending_prefix():
    """The prefix a lifecycle rule would expire. Nothing finalised is ever stored under it."""
    return f"{_root()}/{PENDING}/"


def is_pending_key(key):
    return isinstance(key, str) and key.startswith(pending_prefix())


def build_pending_key(usage, extension):
    """`uploads/pending/<env>/<usage>/<uuid>.<ext>`: no owner, no project, no client name; the scope is in the ticket."""
    return posixpath.join(_root(), PENDING, _environment_segment(), usage, f"{uuid.uuid4().hex}.{extension}")


def owner_segment(user, policy):
    """company_N, user_N: the account an upload belongs to. An avatar belongs to its user, not to the company."""
    if policy.scope == "self" or not getattr(user, "company_id", None):
        return f"user_{user.pk}"
    return f"company_{user.company_id}"


def project_team_owners(project):
    """GED-SEC.08.2-B0.4-FIX: the `company_N` / `user_N` segments whose uploads of this project are legitimate: the project's
    own company and owner, and the companies and users of its team. One definition for the PDF generation and for the direct
    read of a site report photo. It decides WHOSE upload it is, never who may read it (that stays the caller's project scope
    and business permission)."""
    from project_management.models import ProjectTeamMembers

    members = list(ProjectTeamMembers.objects.filter(project=project).select_related("team_member"))
    companies = {project.company_profile_id} | {m.team_member.company_id for m in members if m.team_member.company_id}
    users = {m.team_member_id for m in members} | {project.user_id}
    return {f"company_{n}" for n in companies if n} | {f"user_{n}" for n in users if n}


def build_final_key(policy, owner, project_id, extension):
    """`uploads/<public|private>/<env>/<usage>/<owner>/[project_N/]<uuid>.<ext>`: built by the server, the random part
    is a version 4 UUID (os.urandom), the client name never takes part. Two calls never give the same key, so an upload
    can never replace an existing object."""
    parts = [_root(), policy.visibility, _environment_segment(), policy.name, owner]
    if project_id:
        parts.append(f"project_{project_id}")
    return posixpath.join(*parts, f"{uuid.uuid4().hex}.{extension}")


@dataclass(frozen=True)
class FinalReference:
    visibility: str
    environment: str
    usage: str
    owner: str
    project_id: object
    extension: str


def _reference_pattern():
    return re.compile(
        rf"^{re.escape(_root())}/(public|private)/([a-z0-9-]+)/([a-z_]+)/((?:company|user)_[1-9][0-9]*)"
        rf"/(?:project_([1-9][0-9]*)/)?[0-9a-f]{{32}}\.([a-z0-9]+)$"
    )


def parse_final_reference(key):
    """The structure of a final key, or None for anything else (a URL, a pending key, a path with a detour...)."""
    if not isinstance(key, str) or len(key) > 300:
        return None
    match = _reference_pattern().match(key)
    if not match:
        return None
    visibility, environment, usage, owner, project, extension = match.groups()
    return FinalReference(visibility, environment, usage, owner, int(project) if project else None, extension)


def reference_belongs_to(reference, policy, *, owner, project_id=None):
    """Is this parsed key the final key of this usage, of this owner, in this environment and of this project? With no
    project in play the key must carry none either: a key prepared for a project is not attachable somewhere else."""
    if reference is None or reference.usage != policy.name or reference.visibility != policy.visibility:
        return False
    if reference.environment != _environment_segment() or reference.owner != owner:
        return False
    if reference.extension not in policy.extensions:
        return False
    return reference.project_id == project_id


def public_base_url():
    """Where the PUBLIC prefix is served. `SECURE_UPLOAD_PUBLIC_BASE_URL` (a future CDN domain) wins; otherwise the
    bucket's own address. Whether that prefix may really be read anonymously is an AWS decision, not assumed here."""
    configured = getattr(settings, "SECURE_UPLOAD_PUBLIC_BASE_URL", None)
    if configured:
        return str(configured).rstrip("/")
    domain = getattr(settings, "AWS_S3_CUSTOM_DOMAIN", None)
    if domain:
        return f"https://{domain}"
    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", "")
    region = getattr(settings, "AWS_S3_REGION_NAME", "") or "eu-west-3"
    return f"https://{bucket}.s3.{region}.amazonaws.com"


def public_url_for(key):
    return f"{public_base_url()}/{key}"


# --- ticket -----------------------------------------------------------------------------------------------------------
def issue_ticket(*, user, usage, pending_key, final_key, content_type, extension, declared_size, project_id, original_name):
    """A signed, non-secret reference to one prepared upload (no AWS data in it): who asked, for what, and where."""
    return signing.dumps(
        {
            "v": 2, "uid": user.pk, "usage": usage, "pending": pending_key, "key": final_key, "ct": content_type,
            "ext": extension, "size": declared_size, "project": project_id, "name": original_name[:MAX_FILENAME_LENGTH],
        },
        salt=TICKET_SALT,
        compress=True,
    )


def read_ticket(token, user):
    """The ticket of a prepared upload. A forged, expired, foreign or malformed reference is one and the same
    refusal, so that nothing tells whether it belongs to somebody else."""
    refusal = UploadRejected("Invalid upload reference.")
    if not isinstance(token, str) or not token or len(token) > 4096:
        raise refusal
    try:
        data = signing.loads(token, salt=TICKET_SALT, max_age=TICKET_MAX_AGE_SECONDS)
    except signing.BadSignature:
        raise refusal
    if not isinstance(data, dict) or data.get("v") != 2 or data.get("uid") != user.pk:
        raise refusal
    return data


_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP = b"PK\x03\x04"


def content_matches_extension(extension, head):
    """Does the beginning of the file look like what its extension claims? A recognisable signature for binary
    formats, no NUL byte for the text ones. It does not prove a file is harmless: it refuses the obvious mismatch
    (a page or a program renamed .png), nothing more."""
    if extension in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if extension == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == "gif":
        return head.startswith((b"GIF87a", b"GIF89a"))
    if extension == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if extension == "pdf":
        return b"%PDF-" in head[:1024]
    if extension in ("docx", "xlsx", "pptx", "odt", "ods", "odp"):
        return head.startswith(_ZIP)
    if extension in ("doc", "xls", "ppt", "msg"):
        return head.startswith(_OLE)
    if extension == "rtf":
        return head.startswith(b"{\\rtf")
    if extension in ("txt", "csv", "tsv", "json", "xml"):
        if b"\x00" in head:
            return False
        text = head.lstrip(b"\xef\xbb\xbf").lstrip()
        if extension == "xml":
            return text.startswith(b"<")
        if extension == "json":
            return text[:1] in (b"{", b"[", b'"', b"-", b"t", b"f", b"n") or text[:1].isdigit()
        return True
    return False
