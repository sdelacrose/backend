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
