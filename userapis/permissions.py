from ecpmpapp.permissions import (
    BaseModulePermission,
)
from django.db.models import Q
from project_management.models import Project


def _is_event_assignee(obj, user):
    if not obj or not user or not hasattr(obj, "assignees"):
        return False
    return obj.assignees.filter(id=user.id).exists()


def _event_assignee_or_creator_filter(user):
    return Q(created_by=user) | Q(assignees=user) | Q(user=user)


def _resolve_project(value):
    if isinstance(value, Project):
        return value
    if not value:
        return None
    try:
        return Project.objects.filter(id=int(value)).first()
    except (TypeError, ValueError):
        return None

class CalendarEventPermission(BaseModulePermission):
    """
    Permission class for calendar events.
    """
    def __init__(self, action='view', scope='company'):
        super().__init__('calendar_events', action, scope)

    def has_permission(self, request, view):
        context = self._get_context(request, view)
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user, context)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None, context)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        context = self._get_context(request, view, obj)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj, context)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj, context)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj, context)
        return False
    
    def _get_context(self, request, view, obj=None):
        if self.scope != 'project':
            return {}

        project = None
        if obj is not None:
            project = getattr(obj, 'project', None)
        elif request.method == 'POST':
            project = request.data.get('project')
        elif request.method == 'GET' and getattr(view, 'action', None) == 'list':
            project = request.query_params.get('project')

        project = _resolve_project(project)
        return {'project': project} if project else {}

    def filter_queryset(self, queryset, user, context=None):
        context = context or {}

        if self.scope == 'project':
            project = context.get('project')
            if not project:
                return queryset.none()
            queryset = queryset.filter(project=project)
            if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
                return queryset
            if self._check_permission(user, 'calendar_events', 'view', self.scope, context):
                return queryset
            if self._check_permission(user, 'calendar_events', 'view_assigned_created', self.scope, context):
                return queryset.filter(_event_assignee_or_creator_filter(user)).distinct()
            return queryset.none()

        if getattr(user, 'is_freelancer', False):
            queryset = queryset.filter(freelancer=user, project__isnull=True)
            if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
                return queryset
            if self._check_permission(user, 'calendar_events', 'view', self.scope, context):
                return queryset
            if self._check_permission(user, 'calendar_events', 'view_assigned_created', self.scope, context):
                return queryset
            return queryset.none()

        queryset = queryset.filter(company_profile=user.company, project__isnull=True)
        if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
            return queryset
        if self._check_permission(user, 'calendar_events', 'view', self.scope, context):
            return queryset
        if self._check_permission(user, 'calendar_events', 'view_assigned_created', self.scope, context):
            return queryset.filter(_event_assignee_or_creator_filter(user)).distinct()
        return queryset.none()

    def _check_create_permission(self, user, context):
        if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'create', self.scope, context):
            return True
        return False
    
    def _check_update_permission(self, user, obj, context):
        if getattr(obj, 'is_private', False) and getattr(obj, 'user', None) != user:
            return False
        if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'update', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'update_assigned_created', self.scope, context):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
            elif _is_event_assignee(obj, user):
                return True
        print(f'[DEBUG] CalendarEventPermission: _check_update_permission: user={user}, obj={obj}')
        return False
    
    def _check_delete_permission(self, user, obj, context):
        if getattr(obj, 'is_private', False) and getattr(obj, 'user', None) != user:
            return False
        if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'delete', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'delete_assigned_created', self.scope, context):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
            elif _is_event_assignee(obj, user):
                return True
        return False
    
    def _check_view_permission(self, user, obj, context):
        if self._check_permission(user, 'calendar_events', 'full_access', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'view', self.scope, context):
            return True
        elif self._check_permission(user, 'calendar_events', 'view_assigned_created', self.scope, context):
            # For list operations (obj is None), allow access - filtering happens in queryset
            if obj is None:
                return True
            # For object operations, check if user created or is assigned to this event
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
            elif getattr(obj, 'user_id', None) == user.id:
                return True
            elif _is_event_assignee(obj, user):
                return True
        return False


class ResourcesPermission(BaseModulePermission):
    """
    Permission class for resources module.
    Used for projects_with_action_items endpoint.
    """
    def __init__(self, action='view', scope='company'):
        super().__init__('resources', action, scope)

    def has_permission(self, request, view):
        # For GET requests, check view permission
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        return True
    
    def has_object_permission(self, request, view, obj):
        # For GET requests, check view permission
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        if self._check_permission(user, 'resources', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'resources', 'view', self.scope, {}):
            return True
        return False
