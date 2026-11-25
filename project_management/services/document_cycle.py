"""Explicit Document Control right, independent of generic document modification."""
from rest_framework.exceptions import PermissionDenied

PERMISSION_KEY = "documents.manage_cycle"

def _explicit_role_right(user, scope, context):
    from models.models import UserRole
    if scope == "company":
        roles = UserRole.objects.filter(user=user, role__scope="company", role__type="solution",
                                        role__is_active=True, role__company_id=user.company_id)
        if user.is_freelancer:
            from django.db.models import Q
            roles = roles.filter(Q(role__freelancer_id=user.pk) | Q(role__freelancer_id__isnull=True))
        return roles.filter(role__role_permissions__permission__key=PERMISSION_KEY).exists()
    from project_management.models import ProjectTeamMembers
    return ProjectTeamMembers.objects.filter(
        project_id=context.get("project"), team_member=user,
        project_role__scope="project", project_role__type="solution", project_role__is_active=True,
        project_role__role_permissions__permission__key=PERMISSION_KEY,
    ).exists()

def has_document_control_right(user, document):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    folder = document.folder
    project = folder.project if folder.project_id else None
    company_id = folder.company_profile_id or (project.company_profile_id if project else None)
    if company_id and user.company_id == company_id:
        if _explicit_role_right(user, "company", {}):
            return True
    if folder.freelancer_id == user.pk and user.is_freelancer:
        if _explicit_role_right(user, "company", {}):
            return True
    return bool(project and _explicit_role_right(user, "project", {"project": project.pk}))

def can_manage_cycle_identity(user, document):
    return bool(user and user.is_authenticated and user.is_active and (
        user.pk in (document.author_id, document.created_by_id) or has_document_control_right(user, document)
    ))

def assert_can_manage_document_cycle(user, document):
    # Called only AFTER the existing document access/update check in write endpoints.
    if not can_manage_cycle_identity(user, document):
        raise PermissionDenied("Only the document author, creator or an explicitly authorised Document Controller may manage its cycle.")

def document_cycle_permissions(user, document):
    from project_management.services.document_register import can_update_document
    return {"can_manage": bool(user and user.is_authenticated and can_manage_cycle_identity(user, document) and can_update_document(user, document))}

def can_stop_workflow(user, workflow):
    from project_management.services.document_register import can_update_document
    document = workflow.revision.document
    return bool(user and user.is_authenticated and workflow.is_active and
                user.pk in (workflow.author_id, document.created_by_id) and can_update_document(user, document))


# GED-WF.1.1: dedicated validation right. It only lets the ASSIGNED actor of the current step sign,
# verify, approve or reject (the assignment is checked in services.document_workflow); it never
# authorises editing a document, its metadata, files or revisions, and documents.update alone never
# authorises a validation. documents.full_access is honoured as the superior right, as everywhere else
# in Documents, at the scope of the document (company library or project).
VALIDATE_PERMISSION_KEY = "documents.validate"
VALIDATION_ACTIONS = ("SIGN", "VALIDATE", "APPROVE", "REJECT")


def can_validate_document(user, document):
    if not user or not user.is_authenticated or not user.is_active or document is None:
        return False
    from project_management.permissions import DocumentPermission

    checker = DocumentPermission(action="validate", scope="mixed")
    if checker.is_company_document(document):
        scope, context = "company", {}
    else:
        scope = "project"
        context = checker._get_context(request=None, view=None, scope="project", obj=document)
    return bool(
        checker._check_permission(user, "documents", "validate", scope, context)
        or checker._check_permission(user, "documents", "full_access", scope, context)
    )


def assert_can_validate_document(user, document):
    """Write-side guard of the validation actions, run after the document access check (404)."""
    if not can_validate_document(user, document):
        raise PermissionDenied("You do not have permission to validate this Document.")
    from rest_framework.exceptions import ValidationError as ApiValidationError

    from project_management.services.document_withdrawal import WITHDRAWN_MESSAGE, is_withdrawn

    if is_withdrawn(document):
        raise ApiValidationError({"detail": WITHDRAWN_MESSAGE})
