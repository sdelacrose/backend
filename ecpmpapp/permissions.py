from rest_framework.permissions import BasePermission
from django.http import JsonResponse
from django.db import connection
from django.conf import settings
from models.models import UserRole, RolePermission, User
from project_management.models import Project, ProjectTeamMembers
import logging

from ecpmpapp.custom_exceptions import InactiveLicenseException
from ecpmpapp.license_queries import get_active_license_sql_condition

logger = logging.getLogger(__name__)

class LicensePermission(BasePermission):
    """
    Custom permission to check if the user has a valid license for the requested product.
    """
    def has_permission(self, request, view):
        # Check if the user is authenticated
        if not request.user:
            return False

        # Fetch the product name from query params or view args
        if request.user.is_deleted or not request.user.is_active:
            return JsonResponse(
                {"detail": "Your account is inactive or has been deleted. Access denied."},
                status=410
            )
        user_id = request.user.id
        product_name = request.query_params.get("solution", "Energy Consultant Solution")
        
        active_license_condition = get_active_license_sql_condition()
        query = f"""
            SELECT 1
            FROM package_license_license AS l
            JOIN models_user AS u ON l.user_id = u.id
            JOIN package_license_product AS p ON l.product_id = p.id
            JOIN package_license_package AS pkg ON l.package_id = pkg.id
            WHERE u.id = %s
                AND p.name = %s
                AND {active_license_condition};
        """

        with connection.cursor() as cursor:
            cursor.execute(query, [user_id, product_name])
            result = cursor.fetchone()

        if not result:
            raise InactiveLicenseException()
        return True

class BaseModulePermission(BasePermission):
    """
    Base permission class for module-level permissions.
    This class can be extended by all apps to implement their specific permission logic.
    """
    
    def __init__(self, module, action, scope='company'):
        """
        Initialize permission with module, action, and scope.
        
        Args:
            module (str): The module name (e.g., 'calendar_event', 'action_item')
            action (str): The action (e.g., 'view', 'create', 'update', 'delete')
            scope (str): The scope ('company' or 'project')
        """
        self.module = module
        self.action = action
        self.scope = scope
    
    def has_permission(self, request, view):
        """
        Check if user has permission for the request.
        This method is called before the view is executed.
        """
        context = self._get_context(request, view)
        return self._check_permission(request.user, self.module, self.action, self.scope, context)
    
    def has_object_permission(self, request, view, obj):
        """
        Check if user has permission for a specific object.
        This method is called after the object is retrieved.
        Override this in subclasses for object-specific logic.
        """
        # Default implementation - use the same permission check
        context = self._get_context(request, view)
        return self._check_permission(request.user, self.module, self.action, self.scope, context)
    
    def _check_permission(self, user, module, action, scope, context):
        """
        Check if user has specific permission with fallback logic.
        """
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        user_role = self._get_user_role(user, scope, context)
        if not user_role:
            return False

        print(f'\n[DEBUG] BaseModulePermission: _check_permission: in module {module} user_role={user_role}\n', flush=True)
            
        permission_key = f"{module}.{action}"
        print(f'\n[DEBUG] BaseModulePermission: _check_permission: in module {module} permission_key={permission_key}\n', flush=True)

        if hasattr(user_role, 'role'):
            has_permission = user_role.role.role_permissions.filter(
                permission__key=permission_key
            ).exists()
            print(f'\n[DEBUG] BaseModulePermission: _check_permission: in module {module} has_permission={has_permission}\n', flush=True)
            return has_permission
        else:
            has_permission = user_role.role_permissions.filter(
                permission__key=permission_key
            ).exists()
            print(f'\n[DEBUG] BaseModulePermission: _check_permission: in module {module} has_permission={has_permission}\n', flush=True)
            return has_permission
    
    def _get_user_role(self, user, scope, context):
        """
        Get user's role for company scope.
        """
        if not user or not getattr(user, 'is_authenticated', False):
            return None
        
        if scope == 'company':
            if user.is_freelancer:
                return UserRole.objects.filter(
                    user=user,
                role__scope='company',
                role__type='solution',
                role__company__isnull=True  # Freelancers don't have a company
            ).first()
        
        
            # Company level: type=solution, scope=company
            return UserRole.objects.filter(
                user=user,
                role__scope='company',
                role__type='solution',
                role__company=user.company
            ).first()
        else:
            # Project level: type=solution, scope=project
            project = context.get('project')
            if not project:
                return None
            member = ProjectTeamMembers.objects.filter(
                project=project,
                team_member=user
            ).first()
            project_role = member.project_role if member else None
            return project_role
    
  
    
    def _get_context(self, request, view):
        """
        Get context for permission checking.
        Override this in subclasses for specific context logic.
        """
        return {}

class BaseObjectPermission(BaseModulePermission):
    """
    Base permission class for object-level permissions.
    Extends BaseModulePermission with object-specific logic.
    """
    
    def has_permission(self, request, view):
        """
        Allow all requests to proceed to object-level permission check.
        """
        return True
    
    def has_object_permission(self, request, view, obj):
        """
        Check permission for specific object.
        Override this in subclasses for specific object logic.
        """
        context = self._get_context(request, view)
        
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_permission(
                request.user, self.module, 'delete', self.scope, context
            )
        
        # For GET requests, use view permission
        return self._check_permission(
            request.user, self.module, 'view', self.scope, context
        )
    
    def _check_update_permission(self, user, obj, context):
        """
        Check if user can update this specific object.
        Override this in subclasses for specific update logic.
        """
        # Default implementation - check full update permission
        return self._check_permission(
            user, self.module, 'update', self.scope, context
        )

class CompanyLevelPermission(BaseModulePermission):
    """
    Permission class specifically for company-level operations.
    """
    
    def __init__(self, module, action):
        super().__init__(module, action, scope='company')
    
    def _get_context(self, request, view):
        # Company operations: type=solution, scope=company
        return {}

class ProjectLevelPermission(BaseModulePermission):
    """
    Permission class specifically for project-level operations.
    """
    
    def __init__(self, module, action):
        super().__init__(module, action, scope='project')
    
    def _get_context(self, request, view):
        """
        Get context for project-level permissions.
        Child classes should override this to provide the project.
        """
        return {}
