from ecpmpapp.constants import StatusType
from ecpmpapp.permissions import BaseModulePermission
from django.db.models import Q
from project_management.models import Milestone, ActionItem
from project_management.models import Project
from project_management.purchase_order_access import (
    PURCHASE_ORDER_PERMISSION_MODULE,
    filter_purchase_orders_to_created_or_project_visibility,
    filter_purchase_orders_to_created_visibility,
    filter_purchase_orders_to_owner_scope,
    filter_purchase_orders_to_project_visibility,
    is_purchase_order_created_visible,
    is_purchase_order_in_owner_scope,
    is_purchase_order_project_visible,
)
from project_management.utils import get_project_id_from_request


def _action_item_assignee_filter(user):
    return Q(assignees=user)


def _is_action_item_assignee(obj, user):
    if not obj or not user or not hasattr(obj, "assignees"):
        return False

    return obj.assignees.filter(id=user.id).exists()


class ProjectPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('project', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, "project")
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user, context)
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, context)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, context)
        return False
    
    def _check_update_permission(self, user, context):
        if self._check_permission(user, 'project', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'project', 'update', self.scope, context):
            return True
        elif check_company_project_permission(self, user, 'full_access', context):
            return True
        return False
    
    def _check_delete_permission(self, user, context):
        if self._check_permission(user, 'project', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'project', 'delete', self.scope, context):
            return True
        elif check_company_project_permission(self, user, 'full_access', context):
            return True
        return False
    
    def _check_view_permission(self, user, context):
        print(f'[DEBUG] ProjectPermission: _check_view_permission: user={user}')
        print(f'[DEBUG] ProjectPermission: _check_view_permission: context={context}')
        print(f'[DEBUG] ProjectPermission: _check_view_permission: self.scope={self.scope}')

        if self._check_permission(user, 'project', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'project', 'view', self.scope, context):
            return True
        elif check_company_project_permission(self, user, 'full_access', context):
            return True
        elif check_company_project_permission(self, user, 'view_all_access', context):
            return True
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project = request.data.get('project')
            elif hasattr(view, 'action') and view.action in {
                'list',
                'filter_options',
                'board',
                'time',
                'calendar',
                'gantt',
            }:
                project = request.query_params.get('project')
                if project:
                    from project_management.models import Project
                    project = Project.objects.get(id=project)

            # For other actions, object level check
            elif obj:
                project = obj.id
            
            if project:
                context['project'] = project

        return context
    


class ProjectsPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('projects', action, scope)

    def has_permission(self, request, view):
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_list_permission(request.user)
        
        return True
    
    def _check_create_permission(self, user):
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        elif self._check_permission(user, 'projects', 'create', "company", {}):
            return True
        return False
    
    def _check_view_list_permission(self, user):
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        elif self._check_permission(user, 'projects', 'view', "company", {}):
            return True
        elif self._check_permission(user, 'projects', 'view_all_access', "company", {}):
            return True
        elif self._check_permission(user, 'projects', 'view', "company", {}):
            return True
        elif self._check_permission(user, 'projects', 'view_assigned_created', self.scope, {}):
            return True
        return False
    
    def _check_view_all_permission(self, user):
        print(f'[DEBUG] ProjectsPermission: _check_view_all_permission: user={user}')
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        elif check_company_project_permission(self, user, 'view_all_access', {}):
            return True
        elif self._check_permission(user, 'projects', 'view', "company", {}):
            return True
        elif self._check_permission(user, 'projects', 'view_assigned_created', self.scope, {}):
            return True
        return False
    
    def has_object_permission(self, request, view, obj):
        
        if request.method == 'GET':
            return self._check_view_all_permission(request.user)
        elif request.method in ['PUT', 'PATCH']:
            return self._check_create_permission(request.user)
        elif request.method == 'DELETE':
            return self._check_create_permission(request.user)
        return False


class ActionItemPermission(BaseModulePermission):
    READ_COLLECTION_ACTIONS = {
        'list',
        'filter_options',
        'board',
        'time',
        'calendar',
        'gantt',
    }

    def __init__(self, action='view', scope='company'):
        super().__init__('action_item', action, scope)

    def has_permission(self, request, view):
        if (
            request.method == 'POST'
            and getattr(view, 'action', None) == 'make_sub_action'
        ):
            return True
        context = self._get_context(request, view, self.scope, None)
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif (
            request.method == 'GET'
            and view.action in self.READ_COLLECTION_ACTIONS
        ):
            # For list operations, check view permission based on scope
            # Also check view_assigned_created permission
            if self._check_view_permission(request.user, None, context):
                return True
            elif self._check_view_assigned_created_permission(request.user, context):
                return True
            return False
        
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        if request.method in ['PUT', 'PATCH']:
            return self.can_update_object(request.user, obj)
        if (
            request.method == 'POST'
            and getattr(view, 'action', None) == 'make_sub_action'
        ):
            return self.can_update_object(request.user, obj)

        is_company_action_item = self._is_company_action_item(obj)
        if is_company_action_item:
            context = self._get_context(request, view, "company", obj)
        else:
            context = self._get_context(request, view, "project", obj)

        if request.method == 'DELETE':
            if is_company_action_item:
                return self._check_delete_permission(request.user, obj)
            else:
                return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            if is_company_action_item:
                return self._check_view_permission(request.user, obj, context)
            else:
                return self._check_view_permission_project(request.user, obj, context)
        return False

    def can_update_object(self, user, obj):
        if self._is_company_action_item(obj):
            permission = (
                self
                if self.scope == "company"
                else ActionItemPermission(
                    action="update",
                    scope="company",
                )
            )
            return permission._check_update_permission(user, obj)

        permission = (
            self
            if self.scope == "project"
            else ActionItemPermission(
                action="update",
                scope="project",
            )
        )
        context = permission._get_context(
            request=None,
            view=None,
            scope="project",
            obj=obj,
        )
        return permission._check_update_permission_project(
            user,
            obj,
            context,
        )
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                milestone_id = request.data.get('milestone')
                parent_action_item_id = request.data.get('parent_action_item')
                if milestone_id:
                    try:
                        milestone = Milestone.objects.get(id=milestone_id)
                        project = milestone.project
                    except Milestone.DoesNotExist:
                        project = None
                elif parent_action_item_id:
                    try:
                        parent_action_item = ActionItem.objects.get(id=parent_action_item_id)
                        project = parent_action_item.milestone.project if parent_action_item.milestone else None
                    except ActionItem.DoesNotExist:
                        project = None
            
            elif (
                hasattr(view, 'action')
                and view.action in self.READ_COLLECTION_ACTIONS
            ):
                project = request.query_params.get('project')
            
            elif obj:
                if obj.parent_action_item:
                    project = obj.parent_action_item.milestone.project if obj.parent_action_item.milestone else None
                else:
                    project = obj.milestone.project if obj.milestone else None
            
            if project:
                context['project'] = project
            
        return context
    def _is_company_action_item(self, obj):
        """Helper method to determine if action item is company-level"""
        if obj.parent_action_item:
            return self._is_company_action_item(obj.parent_action_item)
        return obj.milestone is None
    
    def _check_create_permission(self, user, context):
        # For company scope, only check company-level permissions
        if self.scope == 'company':
            if self._check_permission(user, 'action_item', 'full_access', "company", context):
                return True
            elif self._check_permission(user, 'action_item', 'create', "company", context):
                return True
        # For project scope, check project-level permissions
        elif self.scope == 'project':
            if check_company_project_permission(self, user, 'full_access', context):
                return True
            if self._check_permission(user, 'action_item', 'full_access', "project", context):
                return True
            elif self._check_permission(user, 'action_item', 'create', "project", context):
                return True
        
        return False
    
    def _check_update_permission(self, user, obj):
        # For company scope, only check company-level permissions
        if self.scope == 'company':
            if self._check_permission(user, 'action_item', 'full_access', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'update', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'update_assigned_created', "company", {}):
                if _is_action_item_assignee(obj, user):
                    return True
                elif hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
        return False
    
    def _check_delete_permission(self, user, obj):
        # For company scope, only check company-level permissions
        if self.scope == 'company':
            if self._check_permission(user, 'action_item', 'full_access', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'delete', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'delete_assigned_created', "company", {}):
                if _is_action_item_assignee(obj, user):
                    return True
                elif hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
        
        return False
    
    def _check_view_permission(self, user, obj, context):
        # For company scope, only check company-level permissions
        if self.scope == 'company':
            if self._check_permission(user, 'action_item', 'full_access', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'view', "company", {}):
                return True
            elif self._check_permission(user, 'action_item', 'view_assigned_created', "company", {}):
                if _is_action_item_assignee(obj, user):
                    return True
                elif obj and hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
                elif self._can_view_parent_via_visible_child(user, obj):
                    return True
        # For project scope, check project-level permissions
        elif self.scope == 'project':
            
            if check_company_project_permission(self, user, 'full_access', context):
                return True
            elif check_company_project_permission(self, user, 'view_all_access', context):
                return True
            elif self._check_permission(user, 'action_item', 'full_access', "project", context):
                return True
            elif self._check_permission(user, 'action_item', 'view', "project", context):
                return True
            elif self._check_permission(user, 'action_item', 'view_assigned_created', "project", context):
                if _is_action_item_assignee(obj, user):
                    return True
                elif obj and hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
                elif self._can_view_parent_via_visible_child(user, obj):
                    return True
        return False

    def _check_view_assigned_created_permission(self, user, context):
        """Check if user has view_assigned_created permission"""
        # For company scope, check company-level view_assigned_created permission
        if self.scope == 'company':
            return self._check_permission(user, 'action_item', 'view_assigned_created', "company", context)
        # For project scope, check project-level view_assigned_created permission
        elif self.scope == 'project':
            return self._check_permission(user, 'action_item', 'view_assigned_created', "project", context)
        return False


    def _check_create_permission_project(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'action_item', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'create', "project", context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):


        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'action_item', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'update', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'update_assigned_created', "project", context):
            if _is_action_item_assignee(obj, user):
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission_project(self, user, obj, context):

        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'action_item', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'delete', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'delete_assigned_created', "project", context):
            if _is_action_item_assignee(obj, user):
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission_project(self, user, obj, context):

        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'action_item', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'view', "project", context):
            return True
        elif self._check_permission(user, 'action_item', 'view_assigned_created', "project", context):
            if _is_action_item_assignee(obj, user):
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
            elif self._can_view_parent_via_visible_child(user, obj):
                return True
        return False

    def _can_view_parent_via_visible_child(self, user, obj):
        if not obj or not hasattr(obj, "sub_action_items"):
            return False

        return obj.sub_action_items.filter(
            _action_item_assignee_filter(user) | Q(created_by=user)
        ).exists()

class InvoicePermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('invoice', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        elif request.method == 'POST' and getattr(view, "action", None) == "export_package":
            return self._check_view_permission(request.user, obj)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')

            elif obj:
                project = obj.project if obj.project else None
            
            if project:
                context['project'] = project
        return context
    
    def is_company_invoice(self, obj):
        return obj.company_profile is not None

    def _check_create_permission(self, user):
        if self._check_permission(user, 'invoice', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'invoice', 'create', self.scope, {}):
            return True
        return False
    
    def _check_update_permission(self, user, obj):
        if self._check_permission(user, 'invoice', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'invoice', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'invoice', 'update_created', self.scope, {}):
            print(f'[DEBUG] InvoicePermission: update_created permission found')
            if hasattr(obj, 'created_by'):
                if obj.created_by == user:
                    print(f'[DEBUG] InvoicePermission: update_created permission granted for user {user}')
                    return True
                elif obj.created_by is None:
                    print(f'[DEBUG] InvoicePermission: invoice has no created_by, denying update_created access')
                    return False
                else:
                    print(f'[DEBUG] InvoicePermission: invoice created by different user, denying update_created access')
                    return False
            else:
                print(f'[DEBUG] InvoicePermission: invoice has no created_by field, denying update_created access')
                return False
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'invoice', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'invoice', 'delete', self.scope, {}):
            return True
        elif self._check_permission(user, 'invoice', 'delete_created', self.scope, {}):
            if hasattr(obj, 'created_by'):
                if obj.created_by == user:
                    return True
                elif obj.created_by is None:
                    return False
                else:
                    return False
            else:
                return False
        return False
    
    def _check_view_permission(self, user, obj):
        print(f'[DEBUG] InvoicePermission: _check_view_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'invoice', 'full_access', self.scope, {}):
            print(f'[DEBUG] InvoicePermission: full_access granted')
            return True
        elif self._check_permission(user, 'invoice', 'view', self.scope, {}):
            print(f'[DEBUG] InvoicePermission: view granted')
            return True
        elif self._check_permission(user, 'invoice', 'view_created', self.scope, {}):
            print(f'[DEBUG] InvoicePermission: view_created permission found')
            print(f'[DEBUG] InvoicePermission: obj: {obj}')
            
            # For list view (obj=None), allow access if user has view_created permission
            if obj is None:
                print(f'[DEBUG] InvoicePermission: list view, access granted for view_created')
                return True
            # For object view, check if user created this specific invoice
            elif obj and hasattr(obj, 'created_by'):
                if obj.created_by == user:
                    print(f'[DEBUG] InvoicePermission: user created this invoice, access granted')
                    return True
                elif obj.created_by is None:
                    print(f'[DEBUG] InvoicePermission: invoice has no created_by, denying access for view_created')
                    return True
                else:
                    print(f'[DEBUG] InvoicePermission: invoice created by different user, access denied')
                    return False
            else:
                print(f'[DEBUG] InvoicePermission: invoice has no created_by field, denying access for view_created')
                return False
        else:
            print(f'[DEBUG] InvoicePermission: no permissions found')
        return False


class PurchaseOrderPermission(BaseModulePermission):
    ACTION_PERMISSIONS = {
        "create": ("full_access", "create"),
        "view": ("full_access", "view", "view_created", "view_project"),
        "view_project": ("full_access", "view", "view_project"),
        "update": ("full_access", "update", "update_created"),
        "delete": ("full_access", "delete", "delete_created"),
    }

    def __init__(self, action="view", scope="company"):
        super().__init__(PURCHASE_ORDER_PERMISSION_MODULE, action, scope)

    def _has_any_permission(self, user, actions):
        return any(
            self._check_permission(
                user,
                PURCHASE_ORDER_PERMISSION_MODULE,
                action,
                self.scope,
                {},
            )
            for action in actions
        )

    def has_action_permission(self, user, action):
        return self._check_permission(
            user,
            PURCHASE_ORDER_PERMISSION_MODULE,
            action,
            self.scope,
            {},
        )

    def get_view_scope(self, user):
        if not user or not getattr(user, "is_authenticated", False):
            return "none"
        if self._has_any_permission(user, ("full_access", "view")):
            return "all"
        can_view_created = self.has_action_permission(user, "view_created")
        can_view_project = self.has_action_permission(user, "view_project")
        if can_view_created and can_view_project:
            return "created_or_project"
        if can_view_created:
            return "created"
        if can_view_project:
            return "project"
        return "none"

    def can_view_project_purchase_orders(self, user):
        return self._has_any_permission(
            user,
            self.ACTION_PERMISSIONS["view_project"],
        )

    def can_select_project_purchase_orders(self, user):
        return self._has_any_permission(
            user,
            self.ACTION_PERMISSIONS["view"],
        )

    def filter_queryset_for_view(self, queryset, user, view_scope=None):
        resolved_scope = view_scope or self.get_view_scope(user)
        if resolved_scope == "all":
            return filter_purchase_orders_to_owner_scope(queryset, user)
        if resolved_scope == "created":
            return filter_purchase_orders_to_created_visibility(queryset, user)
        if resolved_scope == "project":
            return filter_purchase_orders_to_project_visibility(queryset, user)
        if resolved_scope == "created_or_project":
            return filter_purchase_orders_to_created_or_project_visibility(
                queryset,
                user,
            )
        return queryset.none()

    def has_permission(self, request, view):
        actions = self.ACTION_PERMISSIONS.get(self.action, (self.action,))
        return self._has_any_permission(request.user, actions)

    def has_object_permission(self, request, view, obj):
        if not is_purchase_order_in_owner_scope(obj, request.user):
            return False

        if self.has_action_permission(request.user, "full_access"):
            return True

        if self.action in {"view", "view_project"}:
            if self.has_action_permission(request.user, "view"):
                return True
            if (
                self.action == "view"
                and self.has_action_permission(
                    request.user,
                    "view_created",
                )
                and is_purchase_order_created_visible(obj, request.user)
            ):
                return True
            return self.has_action_permission(
                request.user,
                "view_project",
            ) and is_purchase_order_project_visible(obj, request.user)

        if self.action in {"update", "delete"}:
            if self.has_action_permission(request.user, self.action):
                return True
            return self.has_action_permission(
                request.user,
                f"{self.action}_created",
            ) and obj.created_by_id == request.user.id

        return False


class ContactPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('contact_management', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            print(f'[DEBUG] ContactPermission: has_permission: GET | request.user: {request.user} | view: {view}')
            return self._check_view_permission(request.user, None)
        elif request.method == 'DELETE':
            print(f'[DEBUG] ContactPermission: has_permission: DELETE | request.user: {request.user} | view: {view}')
            return self._check_delete_permission(request.user, None)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        if request.method in ['PUT', 'PATCH'] and not hasattr(obj, 'is_deleted'):
            return self._check_update_permission(request.user, obj)
        elif request.method == 'PATCH' and hasattr(obj, 'is_deleted'):
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _get_context(self, request, view):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if self.scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')


            
            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user):
        if self._check_permission(user, 'contact_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'contact_management', 'create', self.scope, {}):
            return True
        
        return False
    
    def _check_update_permission(self, user, obj):

        if self._check_permission(user, 'contact_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'contact_management', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'contact_management', 'recycle', self.scope, {}):
            return True
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'contact_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'contact_management', 'delete', self.scope, {}):
            return True
        print(f'[DEBUG] ContactPermission: _check_delete_permission: no permission found')
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'contact_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'contact_management', 'view', self.scope, {}):
            print(f'[DEBUG] ContactPermission: _check_view_permission: view granted')
            return True
        return False


class DocumentPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('documents', action, scope)

    def has_permission(self, request, view):
        print(f'[DEBUG] DocumentPermission: has_permission: request.method: {request.method} | view: {view}')
        context = self._get_context(request, view, self.scope)
        
        if request.method == 'POST':
            print('[DEBUG] DocumentPermission: has_permission: POST')
            
            return self._check_create_permission(request.user, context)

        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user, None, context)
        # For other operations, allow to proceed to object-level check
        print(f'[DEBUG] DocumentPermission: has_permission: True')
        return True
    
    def has_object_permission(self, request, view, obj):
        is_company_document = self.is_company_document(obj)
        if is_company_document:
            context = self._get_context(request, view, "company", obj)
        else:
            context = self._get_context(request, view, "project", obj)

        print(f'[DEBUG] DocumentPermission: has_object_permission: context: {context}')

        if request.method in ['PUT', 'PATCH']:
            return self.has_update_permission(request, view, obj)
        elif request.method == 'DELETE':
            if is_company_document:
                return self._check_delete_permission(request.user, obj)
            else:
                return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            print(f'[DEBUG] DocumentPermission: has_object_permission: GET | is_company_document: {is_company_document} | context: {context}')
            if is_company_document:
                return self._check_view_permission(request.user, obj, context)
            else:
                return self._check_view_permission_project(request.user, obj, context)
        return False

    def has_update_permission(self, request, view, obj):
        if self.is_company_document(obj):
            return self._check_update_permission(request.user, obj)

        context = self._get_context(request, view, "project", obj)
        return self._check_update_permission_project(request.user, obj, context)

    def can_update_object(self, user, obj):
        if self.is_company_document(obj):
            permission = (
                self
                if self.scope == "company"
                else DocumentPermission(action="update", scope="company")
            )
            return permission._check_update_permission(user, obj)

        permission = (
            self
            if self.scope == "project"
            else DocumentPermission(action="update", scope="project")
        )
        context = permission._get_context(
            request=None,
            view=None,
            scope="project",
            obj=obj,
        )
        return permission._check_update_permission_project(user, obj, context)

    def _check_mixed_list_view_permission(self, user):
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        if check_company_project_permission(self, user, 'view_all_access', {}):
            return True

        company_checks = (
            self._check_permission(user, 'project', 'full_access', 'company', {}),
            self._check_permission(user, 'documents', 'full_access', 'company', {}),
            self._check_permission(user, 'documents', 'view', 'company', {}),
            self._check_permission(
                user,
                'documents',
                'view_assigned_created',
                'company',
                {},
            ),
            self._check_permission(
                user,
                'documents',
                'view_created',
                'company',
                {},
            ),
        )

        project_checks = (
            self._check_permission(user, 'documents', 'full_access', 'project', {}),
            self._check_permission(user, 'documents', 'view', 'project', {}),
            self._check_permission(
                user,
                'documents',
                'view_assigned_created',
                'project',
                {},
            ),
            self._check_permission(
                user,
                'documents',
                'view_created',
                'project',
                {},
            ),
        )

        return any(company_checks) or any(project_checks)
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                folder_id = request.data.get('folder')
                print(f'[DEBUG] DocumentPermission: _get_context: folder_id: {folder_id}')
                if folder_id:
                    from project_management.models import Folder
                    try:
                        folder = Folder.objects.get(id=folder_id)
                        print(f'[DEBUG] DocumentPermission: _get_context: folder: {folder}, folder.project: {folder.project}')
                        if folder.project:
                            project = folder.project
                    except Exception as e:
                        print(f'[DEBUG] DocumentPermission: _get_context: Error getting folder: {e}')
            
            elif hasattr(view, 'action') and view.action == 'list':
                name = request.query_params.get('name')
                company_profile_id = request.query_params.get('folder__company_profile')
                project_id = request.query_params.get('folder__project')

                if company_profile_id:
                    from models.models import CompanyProfile
                    company_profile = CompanyProfile.objects.get(id=company_profile_id)
                    context['company_profile'] = company_profile
                elif project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
                    context['project'] = project

                print(f'[DEBUG] DocumentPermission: _get_context: name: {name}')
                if name and '_company_root_folder' in name:
                    # Company folder - extract company profile id from name
                    try:
                        company_profile_id = name.split('_')[0]
                        from models.models import CompanyProfile
                        company_profile = CompanyProfile.objects.get(id=company_profile_id)
                        context['company_profile'] = company_profile
                    except (ValueError, IndexError, CompanyProfile.DoesNotExist):
                        pass
                elif name and '_root_folder' in name:
                    # Project folder - extract project id from name
                    try:
                        project_id = name.split('_')[0]
                        from project_management.models import Project
                        project = Project.objects.get(id=project_id)
                        context['project'] = project
                    except (ValueError, IndexError, Project.DoesNotExist):
                        pass
                else:
                    pass
        
            elif hasattr(obj, "folder") and obj.folder:
                project = obj.folder.project if obj.folder.project else None
            elif hasattr(obj, "project"):
                project = obj.project
            else:
                project = None

            print(f'[DEBUG] DocumentPermission: _get_context: project: {project}')
            if project:
                context['project'] = project
        
        print(f'[DEBUG] DocumentPermission: _get_context: final context: {context}')
        return context
    
    def is_company_document(self, obj):
        if hasattr(obj, 'folder') and obj.folder:
            return obj.folder.company_profile is not None
        if hasattr(obj, 'company_profile') and obj.company_profile:
            return obj.company_profile is not None
        return False

    def _check_create_permission(self, user, context):
        print(f'\n\n[DEBUG] DocumentPermission: _check_create_permission: context: {context} | user: {user} | scope: {self.scope}\n\n')
        
        # Check company-level permissions first
        if check_company_project_permission(self, user, 'full_access', context):
            print('[DEBUG] DocumentPermission: _check_create_permission: company projects full_access granted')
            return True
        if self._check_permission(user, 'documents', 'full_access', "company", context):
            print('[DEBUG] DocumentPermission: _check_create_permission: company documents full_access granted')
            return True
        elif self._check_permission(user, 'documents', 'create', "company", context):
            print('[DEBUG] DocumentPermission: _check_create_permission: company documents create granted')
            return True
        
        # Check project-level permissions
        if self._check_permission(user, 'documents', 'full_access', self.scope, context):
            print('[DEBUG] DocumentPermission: _check_create_permission: project documents full_access granted')
            return True
        elif self._check_permission(user, 'documents', 'create', self.scope, context):
            print('[DEBUG] DocumentPermission: _check_create_permission: project documents create granted')
            return True
        
        print('[DEBUG] DocumentPermission: _check_create_permission: no permissions granted')
        return False
    
    def _check_update_permission(self, user, obj):
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        if self._check_permission(user, 'documents', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'documents', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'documents', 'update_assigned_created', self.scope, {}):
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        elif self._check_permission(user, 'documents', 'sync_sharepoint', self.scope, {}):
            return True
        
        return False
    
    def _check_delete_permission(self, user, obj):
        if check_company_project_permission(self, user, 'full_access', {}):
            return True
        if self._check_permission(user, 'documents', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'documents', 'delete', self.scope, {}):
            return True
        elif self._check_permission(user, 'documents', 'delete_assigned_created', self.scope, {}):
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        
        return False
    
    def _check_view_permission(self, user, obj, context):
        if self.scope == 'mixed':
            return self._check_mixed_list_view_permission(user)

        if self.scope == 'company':
            if check_company_project_permission(self, user, 'full_access', {}):
                return True
            if self._check_permission(user, 'project', 'full_access', self.scope, {}):
                return True
            if check_company_project_permission(self, user, 'view_all_access', {}):
                return True
            if self._check_permission(user, 'documents', 'full_access', self.scope, {}):
                return True
            elif self._check_permission(user, 'documents', 'view', self.scope, {}):
                return True
            elif self._check_permission(user, 'documents', 'view_assigned_created', self.scope, {}):
                if obj is None:
                    return True
                if hasattr(obj, 'assignee') and obj.assignee == user:
                    return True
                elif hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
        
        if self.scope == 'project':
            if check_company_project_permission(self, user, 'full_access', context):
                return True
            if self._check_permission(user, 'documents', 'full_access', self.scope, context):
                return True
            elif self._check_permission(user, 'documents', 'view', self.scope, context):
                return True
            elif self._check_permission(user, 'documents', 'view_assigned_created', self.scope, context):
                if obj is None:
                    return True
                if hasattr(obj, 'assignee') and obj.assignee == user:
                    return True
                elif hasattr(obj, 'created_by') and obj.created_by == user:
                    return True
        return False

    def _check_create_permission_project(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'documents', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'create', "project", context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'documents', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'update', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'update_assigned_created', "project", context):
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'documents', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'delete', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'delete_assigned_created', "project", context):
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        
        return False
    
    def _check_view_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'documents', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'documents', 'view', "project", context):
            return True
        
        return False


class ECPMPConfigPermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('ecpmp_configurations', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope)

        if request.method == 'POST' and view.action in {
            'sync_with_admin',
            'reset_config',
        }:
            return True
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None, context)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, self.scope, obj)

        if request.method == 'POST' and view.action == 'sync_with_admin':
            return self._check_sync_admin_permission(request.user, obj, context)
        if request.method == 'POST' and view.action == 'reset_config':
            return self._check_reset_permission(request.user, obj, context)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    try:
                        project = Project.objects.get(id=project_id)
                    except Project.DoesNotExist:
                        pass
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            
            elif obj:
                project = obj.project if hasattr(obj, 'project') else None

            if project:
                context['project'] = project

        return context
    
    def _check_create_permission(self, user, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', self.scope, context):
            return True
        return False
    
    def _check_update_permission(self, user, obj, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', self.scope, context):
            return True
        if self._check_permission(user, 'ecpmp_configurations', 'update', self.scope, context):
            return True
        return False
    
    def _check_delete_permission(self, user, obj, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', self.scope, context):
            return True
        return False
    
    def _check_view_permission(self, user, obj, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', 'project', context):
            return True
        elif self._check_permission(user, 'ecpmp_configurations', 'view', 'project', context):
            return True
        return False
    
    def _check_sync_admin_permission(self, user, obj, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', self.scope, context):
            return True
        if self._check_permission(user, 'ecpmp_configurations', 'sync_admin', self.scope, context):
            return True
        return False
    
    def _check_reset_permission(self, user, obj, context):
        if self._check_permission(user, 'ecpmp_configurations', 'full_access', self.scope, context):
            return True
        if self._check_permission(user, 'ecpmp_configurations', 'reset', self.scope, context):
            return True
        return False
    

class GlobalConfigPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('global_configurations', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        if self._check_permission(user, 'global_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'global_configurations', 'view', self.scope, {}):
            return True
        
        return False


class CompanyPublicHolidayPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('company_configurations', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        if request.method in ['POST', 'PUT', 'PATCH', 'DELETE']:
            return self._check_manage_permission(request.user)
        return False

    def has_object_permission(self, request, view, obj):
        user_company_id = getattr(getattr(request.user, "company", None), "id", None)
        if not user_company_id or obj.company_profile_id != user_company_id:
            return False
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        if request.method in ['PUT', 'PATCH', 'DELETE']:
            return self._check_manage_permission(request.user)
        return False

    def _check_view_permission(self, user):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'view', self.scope, {}):
            return True

        return False

    def _check_manage_permission(self, user):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'update', self.scope, {}):
            return True

        return False
    

class PhasePermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('phases_management', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        # For create operations, check create permission
        if request.method == 'POST':
            if getattr(view, "action", None) in {"move_up", "move_down"}:
                return True
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, context=context)
        
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        if request.method in ['PUT', 'PATCH'] or (
            request.method == 'POST'
            and getattr(view, "action", None) in {"move_up", "move_down"}
        ):
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            
            elif obj:
                project = obj.project if hasattr(obj, 'project') else None

            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'phases_management', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'phases_management', 'create', self.scope, context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'phases_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'phases_management', 'update', "project", context):
            return True
        
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'phases_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'phases_management', 'delete', "project", context):
            return True
        
        return False
    
    def _check_view_permission_project(self, user, obj=None, context={}):
        print(f'\n\n\n=============== [DEBUG] PhasePermission: _check_view_permission_project: context={context}\n\n\n', flush=True)
        print(f'\n\n\n=============== [DEBUG] PhasePermission: _check_view_permission_project: user={user}\n\n\n', flush=True)
        print(f'\n\n\n=============== [DEBUG] PhasePermission: _check_view_permission_project: check_company_project_permission={check_company_project_permission(self, user, "full_access", context)}\n\n\n', flush=True)
        if check_company_project_permission(self, user, 'full_access', context):
            print(f'\n\n\n=============== [DEBUG] PhasePermission: _check_view_permission_project: full_access granted\n\n\n', flush=True)
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            print(f'\n\n\n=============== [DEBUG] PhasePermission: _check_view_permission_project: view_all_access granted\n\n\n', flush=True)
            return True
        if self._check_permission(user, 'phases_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'phases_management', 'view', "project", context):
            return True
        
        return False


class MilestonePermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('milestone_management', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)

        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, context=context)
        
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            project = obj.project if obj and hasattr(obj, 'project') else None
            if project is None and hasattr(view, 'action') and view.action in ['create', 'list']:
                project = get_project_id_from_request(request)

            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'milestone_management', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'milestone_management', 'create', self.scope, context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'milestone_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'milestone_management', 'update', "project", context):
            return True
        
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'milestone_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'milestone_management', 'delete', "project", context):
            return True
        
        return False
    
    def _check_view_permission_project(self, user, obj=None, context={}):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'milestone_management', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'milestone_management', 'view', "project", context):
            return True
        return False


class FundingPermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('funding', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, None, context)
        
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)

        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            
            elif obj:
                project = obj.project if hasattr(obj, 'project') else None

            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'funding', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'funding', 'create', self.scope, context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'funding', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'update', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'update_created', "project", context):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'funding', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'delete', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'delete_created', "project", context):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'funding', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'view', "project", context):
            return True
        elif self._check_permission(user, 'funding', 'view_created', "project", context):
            if obj is None:
                return True
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False


class SiteReportingPermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('site_reporting', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, None, context)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        print(f'[DEBUG] SiteReportingPermission: has_object_permission: context={context}')
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        print(f'[DEBUG] SiteReportingPermission: _get_context: scope={scope}')
        print(f'[DEBUG] SiteReportingPermission: _get_context: obj={obj}')
        print(f'[DEBUG] SiteReportingPermission: _get_context: view.action={view.action}')
        print(f'[DEBUG] SiteReportingPermission: _get_context: request.data={request.data}')
        print(f'[DEBUG] SiteReportingPermission: _get_context: request.query_params={request.query_params}')
        if scope == 'project':
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            
            elif obj:
                print(f'[DEBUG] SiteReportingPermission: _get_context: obj={obj}')
                print(f'[DEBUG] SiteReportingPermission: _get_context: obj.project={obj.project}')
                project = obj.project if hasattr(obj, 'project') else None

            
            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'site_reporting', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'site_reporting', 'create', self.scope, context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'site_reporting', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'update', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'update_assigned_created', "project", context):
            if hasattr(obj, 'assignees') and user in obj.assignees.all():
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'site_reporting', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'delete', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'delete_assigned_created', "project", context):
            if hasattr(obj, 'assignees') and user in obj.assignees.all():
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission_project(self, user, obj, context):
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: context={context}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: user={user}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: self._check_permission(user, "projects", "full_access", "company", context)={self._check_permission(user, "projects", "full_access", "company", context)}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: self._check_permission(user, "projects", "view_all_access", "company", context)={self._check_permission(user, "projects", "view_all_access", "company", context)}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: self._check_permission(user, "site_reporting", "full_access", "project", context)={self._check_permission(user, "site_reporting", "full_access", "project", context)}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: self._check_permission(user, "site_reporting", "view", "project", context)={self._check_permission(user, "site_reporting", "view", "project", context)}')
        print(f'[DEBUG] SiteReportingPermission: _check_view_permission_project: self._check_permission(user, "site_reporting", "view_assigned_created", "project", context)={self._check_permission(user, "site_reporting", "view_assigned_created", "project", context)}')
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'site_reporting', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'view', "project", context):
            return True
        elif self._check_permission(user, 'site_reporting', 'view_assigned_created', "project", context):
            if obj is None:
                return True
            if hasattr(obj, 'assignees') and user in obj.assignees.all():
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False


class ObjectAssessmentPermission(BaseModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('object_assessments', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        print(f'[DEBUG] ObjectAssessmentPermission: has_permission: context: {context}')
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, None, context)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        print(f'[DEBUG] ObjectAssessmentPermission: has_object_permission: context: {context}')

        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False

    def can_update_object(self, user, obj):
        context = {"project": obj.project}
        return self._check_update_permission_project(user, obj, context)
    
    def _get_context(self, request, view, scope, obj=None):
        """
        Get context for permission checking.
        For project-level permissions, we need to provide the project object.
        """
        context = {}
        print(f'[DEBUG] ObjectAssessmentPermission: _get_context: scope: {scope}')

        if scope == 'project':
            print(f'[DEBUG] ObjectAssessmentPermission: _get_context: query_params: {request.query_params}')
            print(f'[DEBUG] ObjectAssessmentPermission: _get_context: request data: {request.data}')
            print(f'[DEBUG] ObjectAssessmentPermission: _get_context: obj: {obj}')
            # Try to get project from different sources
            project = None
            
            # For create action, try to get project from request data or query params
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
            
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            
            elif obj:
                project = obj.project if hasattr(obj, 'project') else None

            elif request.query_params.get('project'):
                project = request.query_params.get('project')
            
            if project:
                context['project'] = project
        
        return context
    
    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'object_assessments', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'object_assessments', 'create', self.scope, context):
            return True
        
        return False
    
    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'object_assessments', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'update', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'update_assigned_created', "project", context):
            
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, 'object_assessments', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'delete', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'delete_assigned_created', "project", context):
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, 'object_assessments', 'full_access', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'view', "project", context):
            return True
        elif self._check_permission(user, 'object_assessments', 'view_assigned_created', "project", context):
            if obj is None:
                return True
            if hasattr(obj, 'assignee') and obj.assignee == user:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False


class MatrixModulePermission(BaseModulePermission):
    def __init__(self, module, action='view', scope='project'):
        super().__init__(module, action, scope)
        self.module = module

    def has_permission(self, request, view):
        context = self._get_context(request, view, self.scope, None)
        if request.method == 'POST' and getattr(view, 'action', None) == 'create':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            return self._check_view_permission_project(request.user, None, context)
        return True

    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, "project", obj)
        if request.method == 'POST' and self.action == 'create':
            return self._check_create_permission(request.user, context)
        if request.method in ['PUT', 'PATCH'] or (
            request.method == 'POST' and getattr(view, 'action', None) != 'create'
        ):
            return self._check_update_permission_project(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission_project(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission_project(request.user, obj, context)
        return False

    def _get_context(self, request, view, scope, obj=None):
        context = {}
        if scope == 'project':
            project = None
            if hasattr(view, 'action') and view.action == 'create':
                project_id = request.data.get('project')
                if project_id:
                    project = Project.objects.filter(id=project_id).first()
            elif hasattr(view, 'action') and view.action == 'list':
                project = request.query_params.get('project')
            elif obj:
                project = obj.project if hasattr(obj, 'project') else None
            elif request.query_params.get('project'):
                project = request.query_params.get('project')
            if project:
                context['project'] = project
        return context

    def _check_create_permission(self, user, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, self.module, 'full_access', self.scope, context):
            return True
        return self._check_permission(user, self.module, 'create', self.scope, context)

    def _check_update_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, self.module, 'full_access', "project", context):
            return True
        return self._check_permission(user, self.module, 'update', "project", context)

    def _check_delete_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if self._check_permission(user, self.module, 'full_access', "project", context):
            return True
        return self._check_permission(user, self.module, 'delete', "project", context)

    def _check_view_permission_project(self, user, obj, context):
        if check_company_project_permission(self, user, 'full_access', context):
            return True
        if check_company_project_permission(self, user, 'view_all_access', context):
            return True
        if self._check_permission(user, self.module, 'full_access', "project", context):
            return True
        return self._check_permission(user, self.module, 'view', "project", context)


class CompanyMatrixPermission(BaseModulePermission):
    def __init__(self, module, action='view', scope='company'):
        super().__init__(module, action, scope)

    def has_permission(self, request, view):
        if request.method == 'POST' and getattr(view, 'action', None) == 'create':
            return self._check_create_permission(request.user)
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        return True

    def has_object_permission(self, request, view, obj):
        if not self._belongs_to_company(request.user, obj):
            return False
        if request.method in ['PUT', 'PATCH'] or (
            request.method == 'POST' and getattr(view, 'action', None) != 'create'
        ):
            return self._check_update_permission(request.user)
        if request.method == 'DELETE':
            return self._check_delete_permission(request.user)
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        return False

    def _belongs_to_company(self, user, obj):
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if getattr(user, 'is_freelancer', False) or not getattr(user, 'company_id', None):
            return False
        return getattr(obj, 'company_profile_id', None) == user.company_id

    def _check_create_permission(self, user):
        if self._check_permission(user, self.module, 'full_access', self.scope, {}):
            return True
        return self._check_permission(user, self.module, 'create', self.scope, {})

    def _check_update_permission(self, user):
        if self._check_permission(user, self.module, 'full_access', self.scope, {}):
            return True
        return self._check_permission(user, self.module, 'update', self.scope, {})

    def _check_delete_permission(self, user):
        if self._check_permission(user, self.module, 'full_access', self.scope, {}):
            return True
        return self._check_permission(user, self.module, 'delete', self.scope, {})

    def _check_view_permission(self, user):
        if self._check_permission(user, self.module, 'full_access', self.scope, {}):
            return True
        return self._check_permission(user, self.module, 'view', self.scope, {})


class SolutionDemandPermission(MatrixModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('solution_demands', action, scope)


class RequirementPermission(MatrixModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('requirements', action, scope)


class TransportPermission(MatrixModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('transports', action, scope)


class BillOfMaterialPermission(MatrixModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('bill_of_material', action, scope)


class AvailableProductPartPermission(CompanyMatrixPermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('available_product_parts', action, scope)


class MachinePermission(CompanyMatrixPermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('machines', action, scope)


class EquipmentPermission(CompanyMatrixPermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('equipments', action, scope)


class CompanySolutionSpacePermission(CompanyMatrixPermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('company_assembly_catalog', action, scope)


class ProjectSolutionSpacePermission(MatrixModulePermission):
    def __init__(self, action='view', scope='project'):
        super().__init__('project_assembly_catalog', action, scope)


class TimeBookingPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('time_booking', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _check_create_permission(self, user):
        print(f'[DEBUG] TimeBookingPermission: _check_create_permission: user={user}')
        
        if self._check_permission(user, 'time_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: full_access granted for create')
            return True
        elif self._check_permission(user, 'time_booking', 'create', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: create granted')
            return True
        elif self._check_permission(user, 'time_booking', 'create_without_approval', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: create_without_approval granted')
            return True
        else:
            print(f'[DEBUG] TimeBookingPermission: no create permissions found')
        return False
    
    def _check_update_permission(self, user, obj):
        print(f'[DEBUG] TimeBookingPermission: _check_update_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'time_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: full_access granted for update')
            return True
        if self._check_permission(user, 'time_booking', 'review', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: review permission found')
            # Check if this is a status update (approval/rejection)
            if hasattr(obj, 'status') and obj.status in [StatusType.PENDING.value, StatusType.APPROVED.value, StatusType.REJECTED.value]:
                print(f'[DEBUG] TimeBookingPermission: status update detected, review permission granted')
                return True
            else:
                print(f'[DEBUG] TimeBookingPermission: not a status update, review permission not applicable')
        if self._check_permission(user, 'time_booking', 'update', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: update granted')
            return True
        if self._check_permission(user, 'time_booking', 'update_created', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: update_created permission found')
            if hasattr(obj, 'created_by') and obj.created_by == user:
                print(f'[DEBUG] TimeBookingPermission: user created this record, update granted')
                return True
            else:
                print(f'[DEBUG] TimeBookingPermission: user did not create this record')
        return False
    
    def _check_delete_permission(self, user, obj):
        print(f'[DEBUG] TimeBookingPermission: _check_delete_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'time_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: full_access granted for delete')
            return True
        elif self._check_permission(user, 'time_booking', 'delete', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: delete granted')
            return True
        elif self._check_permission(user, 'time_booking', 'delete_created', self.scope, {}):
            print(f'[DEBUG] TimeBookingPermission: delete_created permission found')
            if hasattr(obj, 'created_by') and obj.created_by == user:
                print(f'[DEBUG] TimeBookingPermission: user created this record, delete granted')
                return True
            else:
                print(f'[DEBUG] TimeBookingPermission: user did not create this record')
        else:
            print(f'[DEBUG] TimeBookingPermission: no delete permissions found')
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'time_booking', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'time_booking', 'view', self.scope, {}):
            return True
        elif self._check_permission(user, 'time_booking', 'view_created', self.scope, {}):
            # For list operations (obj is None), allow access - filtering happens in queryset
            if obj is None:
                return True
            # For object operations, check if user created this specific record
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        elif self._check_permission(user, 'time_booking', 'review', self.scope, {}):
            return True
        return False


class ExpenseBookingPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('expense_booking', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        if request.method in ['PUT', 'PATCH']:
            requested_status = request.data.get('status')
            is_review = requested_status in [
                StatusType.APPROVED.value,
                StatusType.REJECTED.value,
            ]
            return self._check_update_permission(request.user, obj, is_review=is_review)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _check_create_permission(self, user):
        print(f'[DEBUG] ExpenseBookingPermission: _check_create_permission: user={user}')
        
        if self._check_permission(user, 'expense_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: full_access granted for create')
            return True
        elif self._check_permission(user, 'expense_booking', 'create', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: create granted')
            return True
        elif self._check_permission(user, 'expense_booking', 'create_without_approval', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: create_without_approval granted')
            return True
        else:
            print(f'[DEBUG] ExpenseBookingPermission: no create permissions found')
        return False
    
    def _check_update_permission(self, user, obj, is_review=False):
        print(f'[DEBUG] ExpenseBookingPermission: _check_update_permission: user={user}, obj={obj}')

        has_full_access = self._check_permission(
            user, 'expense_booking', 'full_access', self.scope, {}
        )

        if is_review:
            if hasattr(obj, 'created_by') and obj.created_by == user:
                print('[DEBUG] ExpenseBookingPermission: self-review denied')
                return False
            if has_full_access or self._check_permission(
                user, 'expense_booking', 'review', self.scope, {}
            ):
                print('[DEBUG] ExpenseBookingPermission: review permission granted')
                return True
            print('[DEBUG] ExpenseBookingPermission: review permission denied')
            return False

        if has_full_access:
            print(f'[DEBUG] ExpenseBookingPermission: full_access granted for update')
            return True
        
        if self._check_permission(user, 'expense_booking', 'update', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: update granted')
            return True
        if self._check_permission(user, 'expense_booking', 'update_created', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: update_created permission found')
            if hasattr(obj, 'created_by') and obj.created_by == user:
                print(f'[DEBUG] ExpenseBookingPermission: user created this record, update granted')
                return True
            else:
                print(f'[DEBUG] ExpenseBookingPermission: user did not create this record')
        return False
    
    def _check_delete_permission(self, user, obj):
        print(f'[DEBUG] ExpenseBookingPermission: _check_delete_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'expense_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: full_access granted for delete')
            return True
        elif self._check_permission(user, 'expense_booking', 'delete', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: delete granted')
            return True
        elif self._check_permission(user, 'expense_booking', 'delete_created', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: delete_created permission found')
            if hasattr(obj, 'created_by') and obj.created_by == user:
                print(f'[DEBUG] ExpenseBookingPermission: user created this record, delete granted')
                return True
            else:
                print(f'[DEBUG] ExpenseBookingPermission: user did not create this record')
        return False
    
    def _check_view_permission(self, user, obj):
        print(f'[DEBUG] ExpenseBookingPermission: _check_view_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'expense_booking', 'full_access', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: full_access granted for view')
            return True
        elif self._check_permission(user, 'expense_booking', 'view', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: view granted')
            return True
        elif self._check_permission(user, 'expense_booking', 'view_created', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: view_created permission found')
            # For list operations (obj is None), allow access - filtering happens in queryset
            if obj is None:
                print(f'[DEBUG] ExpenseBookingPermission: list view, access granted for view_created')
                return True
            # For object operations, check if user created this specific record
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                print(f'[DEBUG] ExpenseBookingPermission: user created this record, view granted')
                return True
        elif self._check_permission(user, 'expense_booking', 'review', self.scope, {}):
            print(f'[DEBUG] ExpenseBookingPermission: review permission found for view')
            return True
        else:
            print(f'[DEBUG] ExpenseBookingPermission: no view permissions found')
        return False
    
class SharePointProjectPermission(ProjectPermission):
    def _get_context(self, request, view):
        context = {}
        if self.scope == 'project':
            # Get project_id from request data (POST request)
            project_id = request.data.get('project_id')
            if project_id:
                try:
                    from project_management.models import Project
                    project = Project.objects.get(id=project_id)
                    context['project'] = project
                except Project.DoesNotExist:
                    pass
        return context

def check_company_project_permission(self, user, permission_type, context=None):
    """
    Utility function to check company-level project permissions with proper logic for freelancers vs company owners.
    
    Args:
        self: The permission class instance (to access _check_permission method)
        user: The user to check permissions for
        permission_type: The permission type to check ('full_access', 'view_all_access', etc.)
        context: Optional context containing project information
    
    Returns:
        bool: True if user has the permission, False otherwise
    """
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    print(f'\n\n\n=============== [DEBUG] check_company_project_permission: user={user}\n\n\n', flush=True)
    print(f'\n\n\n=============== [DEBUG] check_company_project_permission: permission_type={permission_type}\n\n\n', flush=True)
    print(f'\n\n\n=============== [DEBUG] check_company_project_permission: context={context}\n\n\n', flush=True)

    # For company-scoped operations, grant access if user has company-level permission
    if not context or 'project' not in context:
        print(f'\n\n\n=============== [DEBUG] check_company_project_permission: no context or project in context\n\n\n', flush=True)
        return self._check_permission(user, 'projects', permission_type, "company", {})
    
    project = context['project']
    if isinstance(project, int) or isinstance(project, str):
        try:
            project = Project.objects.get(id=project)
        except Project.DoesNotExist:
            print(f'\n\n\n=============== [DEBUG] check_company_project_permission: project does not exist\n\n\n', flush=True)
            return False
    print(f'\n\n\n=============== [DEBUG] check_company_project_permission: project={project}\n\n\n', flush=True)
    print(f'\n\n\n=============== [DEBUG] check_company_project_permission: type(project)={type(project)}\n\n\n', flush=True)
    # If user is a freelancer, only grant access if they created the project
    if user.is_freelancer:
        if hasattr(project, 'user') and project.user == user:
            print(f'\n\n\n=============== [DEBUG] check_company_project_permission: user is a freelancer and created the project\n\n\n', flush=True)
            return self._check_permission(user, 'projects', permission_type, "company", {})
        return False
    
    # If user is a company owner, grant access if project belongs to their company
    else:
        print(f'\n\n\n=============== [DEBUG] check_company_project_permission: user is a company owner\n\n\n', flush=True)
        print(f'\n\n\n=============== [DEBUG] check_company_project_permission: project.company_profile={project.company_profile}\n\n\n', flush=True)
        print(f'\n\n\n=============== [DEBUG] check_company_project_permission: user.company={user.company}\n\n\n', flush=True)
        if hasattr(project, 'company_profile') and project.company_profile == user.company:
            print(f'\n\n\n=============== [DEBUG] check_company_project_permission: user is a company owner and project belongs to their company\n\n\n', flush=True)
            return self._check_permission(user, 'projects', permission_type, "company", {})
        return False


class DocumentIssuerLookupPermission(BaseModulePermission):
    """Select the external issuing company of a Document (Client / Supplier origin).

    The use case is "which external company issued this document", never browsing the Network:
    it needs the right to create or edit Documents (company scope, or the project scope of the
    optional ?project=<id>) and NEVER requires contact_management.view.
    """

    ACTIONS = ("full_access", "create", "update")

    def __init__(self):
        super().__init__("documents", "create", "company")

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False):
            return False
        if any(
            self._check_permission(user, "documents", action, "company", {})
            for action in self.ACTIONS
        ):
            return True
        project_id = request.query_params.get("project")
        if not str(project_id or "").isdigit():
            return False
        project = Project.objects.filter(pk=project_id).first()
        if project is None:
            return False
        return any(
            self._check_permission(user, "documents", action, "project", {"project": project})
            for action in self.ACTIONS
        )
