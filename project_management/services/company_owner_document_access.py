"""Inherit an actual company owner's existing documentary rights within its company."""
from models.models import CompanyProfile, UserRole
from project_management.models import Project


def documentary_owner_company_id(user):
    if not user or not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', False):
        return None
    if getattr(user, 'is_deleted', False) or getattr(user, 'is_permanently_deleted', False):
        return None
    company_id = getattr(user, 'company_id', None)
    if not company_id or not CompanyProfile.objects.filter(pk=company_id, admin_user_id=user.pk).exists():
        return None
    entitled = UserRole.objects.filter(
        user=user, role__company_id=company_id, role__scope='company',
        role__type='solution', role__is_active=True,
        role__role_permissions__permission__key='documents.full_access',
    ).exists()
    return company_id if entitled else None


class DocumentaryOwnerPermissionMixin:
    def _check_permission(self, user, module, action, scope, context):
        if scope == 'project' and (
            (module == 'documents' and action != 'manage_cycle')
            or (module == 'ecpmp_configurations' and action == 'view')
        ):
            company_id = documentary_owner_company_id(user)
            project = (context or {}).get('project')
            project_id = getattr(project, 'pk', project)
            if company_id and project_id and Project.objects.filter(pk=project_id, company_profile_id=company_id).exists():
                return True
        return super()._check_permission(user, module, action, scope, context)
