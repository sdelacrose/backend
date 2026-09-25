import re

from django.core.exceptions import ValidationError
from django.db.models import Q

MAX_LENGTH = 10
FALLBACK_CODE = "PRJ"
_CODE_RE = re.compile(r"^[A-Z0-9]{1,%d}$" % MAX_LENGTH)


def derive_project_code(project_name):
    """Simple deterministic proposal: initials of the words (2-4 words), else the first 3 letters.

    "Marseille Office Renovation" -> MOR, "Project Alpha" -> PA, "Alpha" -> ALP.
    """
    words = re.findall(r"[A-Za-z0-9]+", str(project_name or ""))
    if len(words) >= 2:
        return "".join(word[0] for word in words[:4]).upper()
    if words:
        return words[0][:3].upper()
    return FALLBACK_CODE


def normalize_project_code(value):
    return re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()


def _owner_projects(project):
    """Projects of the same owner (company, or freelancer): codes are unique per owner."""
    from project_management.models import Project

    company_id = project.company_profile_id or getattr(project.user, "company_id", None)
    if company_id:
        return Project.objects.filter(
            Q(company_profile_id=company_id)
            | Q(company_profile__isnull=True, user__company_id=company_id)
        )
    return Project.objects.filter(user_id=project.user_id, company_profile__isnull=True)


def is_project_code_taken(project, code):
    others = _owner_projects(project)
    if project.pk:
        others = others.exclude(pk=project.pk)
    return others.filter(project_code=code).exists()


def propose_unique_project_code(project):
    base = derive_project_code(project.project_name)
    candidate, number = base, 1
    while is_project_code_taken(project, candidate):
        number += 1
        suffix = str(number)
        candidate = f"{base[: MAX_LENGTH - len(suffix)]}{suffix}"
    return candidate


def project_code_is_locked(project):
    """Locked as soon as a Document of the project has a permanent reference (derived, no flag)."""
    from project_management.models import Document

    if not project.pk:
        return False
    return (
        Document.objects.filter(folder__project_id=project.pk)
        .exclude(full_document_number__isnull=True)
        .exclude(full_document_number="")
        .exists()
    )


def validate_project_code_value(project, code):
    if not _CODE_RE.match(code):
        raise ValidationError(
            f"The project code must be 1-{MAX_LENGTH} letters or digits."
        )
    if is_project_code_taken(project, code):
        raise ValidationError("This project code is already used by another project.")


def assert_project_code_change_allowed(project):
    if project_code_is_locked(project):
        raise ValidationError(
            "The project code cannot be changed: documents already have permanent "
            "references built with it."
        )
    validate_project_code_value(project, project.project_code)


def reference_convention_is_frozen(project):
    """The reference convention of a project is frozen once its first permanent reference exists.

    Derived from the referenced documents (same principle as the project code): no stored flag.
    """
    return project_code_is_locked(project)


def revision_convention_is_frozen(project):
    """The revision scheme/start is frozen once a revision label has been assigned in the project."""
    from project_management.models import DocumentRevision

    if not project.pk:
        return False
    return (
        DocumentRevision.objects.filter(document__folder__project_id=project.pk)
        .exclude(revision_label__isnull=True)
        .exclude(revision_label="")
        .exists()
    )


def _section(document_control_configuration, name):
    from models.document_control_configuration import (
        resolve_document_control_configuration,
    )

    return resolve_document_control_configuration(document_control_configuration)[name]


_FROZEN_CONVENTIONS = (
    (
        "reference",
        reference_convention_is_frozen,
        "The reference convention (segments, separator, sequence width and sequence "
        "scope) cannot be changed: documents of this project already have permanent "
        "references built with it.",
    ),
    (
        "revision",
        revision_convention_is_frozen,
        "The revision scheme and start cannot be changed: revisions of this project "
        "have already been assigned with them.",
    ),
)


def assert_conventions_unchanged(project, current, new):
    """Reject a change of reference.* or revision.* once the project has used them.

    workflow.* is never frozen.
    """
    for name, is_frozen, message in _FROZEN_CONVENTIONS:
        if _section(current, name) != _section(new, name) and is_frozen(project):
            raise ValidationError(message)


def keep_frozen_conventions(project, current, new):
    """Sync/reset helper: on a started project, carry the current frozen sections over `new`."""
    merged = dict(new or {})
    for name, is_frozen, _message in _FROZEN_CONVENTIONS:
        if is_frozen(project):
            merged[name] = dict(_section(current, name))
    return merged
