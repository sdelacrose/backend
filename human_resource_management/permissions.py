from ecpmpapp.permissions import BaseModulePermission

class LeaveRequestPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('leave_request', action, scope)

    def has_permission(self, request, view):
        print(f'[DEBUG] LeaveRequestPermission: has_permission: method={request.method}, action={getattr(view, "action", "unknown")}')
        
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action in {'list', 'calendar'}:
            if view.action == 'list' and self._is_review_queue(request):
                return self._check_permission(
                    request.user,
                    'leave_request',
                    'review',
                    self.scope,
                    request.query_params,
                )
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None, request)
        # For other operations, allow to proceed to object-level check
        return True
    
    def has_object_permission(self, request, view, obj):
        print(f'[DEBUG] LeaveRequestPermission: has_object_permission: method={request.method}, obj={obj}')
        
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj, request)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj, request)
        return False
    
    def _check_create_permission(self, user):
        print(f'[DEBUG] LeaveRequestPermission: _check_create_permission: user={user}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for create')
            return True
        elif self._check_permission(user, 'leave_request', 'create', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: create granted')
            return True
        elif self._check_permission(user, 'leave_request', 'create_without_approval', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: create_without_approval granted')
            return True
        else:
            print(f'[DEBUG] LeaveRequestPermission: no create permissions found')
        
        return False
    
    def _check_update_permission(self, user, obj, request=None):
        print(f'[DEBUG] LeaveRequestPermission: _check_update_permission: user={user}, obj={obj}')

        if request is not None and 'status' in request.data:
            employee_user = getattr(getattr(obj, 'employee', None), 'user', None)
            if employee_user is None or employee_user == user:
                return False
            if user.is_freelancer or employee_user.company_id != user.company_id:
                return False
            return self._check_permission(
                user,
                'leave_request',
                'review',
                self.scope,
                request.query_params,
            )
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for update')
            return True
        elif self._check_permission(user, 'leave_request', 'update', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: update granted')
            return True
        else:
            print(f'[DEBUG] LeaveRequestPermission: no update permissions found')
        
        return False

    @staticmethod
    def _is_review_queue(request):
        return str(request.query_params.get('review_queue', '')).lower() in {
            'true',
            '1',
        }
    
    def _check_delete_permission(self, user, obj):
        print(f'[DEBUG] LeaveRequestPermission: _check_delete_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for delete')
            return True
        elif self._check_permission(user, 'leave_request', 'view', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view granted')
            return True
        elif self._check_permission(user, 'leave_request', 'view_created', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view_created permission found')
            if hasattr(obj, 'employee') and obj.employee and obj.employee.user == user:
                print(f'[DEBUG] LeaveRequestPermission: user is employee, view granted')
                return True
        
        return False
    
    def _check_view_permission(self, user, obj, request=None):
        print(f'[DEBUG] LeaveRequestPermission: _check_view_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'leave_request', 'full_access', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: full_access granted for view')
            return self._is_leave_in_view_scope(user, obj)
        if self._check_permission(user, 'leave_request', 'view', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view granted')
            return self._is_leave_in_view_scope(user, obj)
        if self._check_permission(user, 'leave_request', 'view_created', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: view_created permission found')
            # For list operations (obj is None), allow access - filtering happens in queryset
            if obj is None:
                print(f'[DEBUG] LeaveRequestPermission: list view, access granted for view_created')
                return True
            # For object operations, check if user is the employee of this leave request
            elif hasattr(obj, 'employee') and obj.employee and obj.employee.user == user:
                print(f'[DEBUG] LeaveRequestPermission: user is employee, view granted')
                return True
        if self._check_permission(user, 'leave_request', 'review', self.scope, {}):
            print(f'[DEBUG] LeaveRequestPermission: review permission found for view')
            if not self._is_leave_in_view_scope(user, obj):
                return False
            if (
                obj is not None
                and request is not None
                and self._is_review_queue(request)
                and obj.employee.user == user
            ):
                return False
            return True

        print(f'[DEBUG] LeaveRequestPermission: no view permissions found')
        
        return False

    @staticmethod
    def _is_leave_in_view_scope(user, obj):
        if obj is None:
            return True

        employee_user = getattr(getattr(obj, 'employee', None), 'user', None)
        if employee_user is None:
            return False
        if user.is_freelancer:
            return employee_user == user
        return bool(
            user.company_id
            and employee_user.company_id == user.company_id
        )


class StaffObservationPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('staff_observation', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST':
            return self._check_create_permission(request.user)
        # For other operations, allow to proceed to object-level check
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user, None)
        
        return True
    
    def has_object_permission(self, request, view, obj):
        print("has_object_permission", request.method, obj)
        if request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user, obj)
        elif request.method == 'DELETE':
            return self._check_delete_permission(request.user, obj)
        elif request.method == 'GET':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _check_create_permission(self, user):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'create', self.scope, {}):
            return True
        
        return False
    
    def _check_update_permission(self, user, obj):
        print("check update permission", self.scope, user, obj)
        print(self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}))
        print(self._check_permission(user, 'staff_observation', 'update', self.scope, {}))
        print(self._check_permission(user, 'staff_observation', 'update_created', self.scope, {}))
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'update_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'delete', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'delete_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'staff_observation', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'view', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_observation', 'view_created', self.scope, {}):
            if obj is None:
                return True
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False


class ResourceAllocationPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('resources.resource_allocation', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        if self._check_permission(user, 'resources.resource_allocation', 'view', self.scope, {}):
            return True
        
        return False


class StaffManagementPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('staff_management', action, scope)

    def has_permission(self, request, view):
        if request.method == 'GET' and view.action == 'list':
            return self._check_view_permission(request.user)
        elif request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user)
        return True

    def has_object_permission(self, request, view, obj):
        if request.method == 'GET':
            return self._check_view_permission(request.user)
        elif request.method in ['PUT', 'PATCH']:
            return self._check_update_permission(request.user)
        return False
    
    def _check_view_permission(self, user):
        if self._check_permission(user, 'staff_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_management', 'view', self.scope, {}):
            return True
        
        return False
    
    def _check_update_permission(self, user):
        if self._check_permission(user, 'staff_management', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_management', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'staff_management', 'default_management_update', self.scope, {}):
            return True
        
        return False
