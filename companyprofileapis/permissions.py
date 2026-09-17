from ecpmpapp.permissions import BaseModulePermission

class OpportunityOffersPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('opportunity_offers', action, scope)

    def has_permission(self, request, view):
        if self.action == 'create':
            return self._check_create_permission(request.user)

        if self.action == 'view' and view.action == 'list':
            return self._check_view_permission(request.user, None)

        return True

    def has_object_permission(self, request, view, obj):
        if self.action == 'update':
            return self._check_update_permission(request.user, obj)
        elif self.action == 'full_access':
            return self._check_permission(
                request.user,
                'opportunity_offers',
                'full_access',
                self.scope,
                {},
            )
        elif self.action == 'view':
            return self._check_view_permission(request.user, obj)
        return False
    
    def _check_create_permission(self, user):
        if self._check_permission(user, 'opportunity_offers', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'create', self.scope, {}):
            return True
        
        return False
    
    def _check_update_permission(self, user, obj):
        if self._check_permission(user, 'opportunity_offers', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'update', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'update_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'opportunity_offers', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'delete', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'delete_created', self.scope, {}):
            if hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'opportunity_offers', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'view', self.scope, {}):
            return True
        elif self._check_permission(user, 'opportunity_offers', 'view_created', self.scope, {}):
            if obj is None:
                return True
            elif hasattr(obj, 'created_by') and obj.created_by == user:
                return True
        return False



class NoticeBoardPermission(BaseModulePermission):
    def __init__(self, action='view', scope='company'):
        super().__init__('notice_board', action, scope)

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
        print(f'[DEBUG] NoticeBoardPermission: _check_create_permission: user={user}')
        
        if self._check_permission(user, 'notice_board', 'full_access', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: full_access granted for create')
            return True
        elif self._check_permission(user, 'notice_board', 'create', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: create granted')
            return True
        else:
            print(f'[DEBUG] NoticeBoardPermission: no create permissions found')
        return False
    
    def _check_update_permission(self, user, obj):
        print(f'[DEBUG] NoticeBoardPermission: _check_update_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'notice_board', 'full_access', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: full_access granted for update')
            return True
        elif self._check_permission(user, 'notice_board', 'update', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: update granted')
            return True
        else:
            print(f'[DEBUG] NoticeBoardPermission: no update permissions found')
        return False
    
    def _check_delete_permission(self, user, obj):
        print(f'[DEBUG] NoticeBoardPermission: _check_delete_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'notice_board', 'full_access', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: full_access granted for delete')
            return True
        elif self._check_permission(user, 'notice_board', 'delete', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: delete granted')
            return True
        else:
            print(f'[DEBUG] NoticeBoardPermission: no delete permissions found')
        return False
    
    def _check_view_permission(self, user, obj):
        print(f'[DEBUG] NoticeBoardPermission: _check_view_permission: user={user}, obj={obj}')
        
        if self._check_permission(user, 'notice_board', 'full_access', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: full_access granted')
            return True
        elif self._check_permission(user, 'notice_board', 'view', self.scope, {}):
            print(f'[DEBUG] NoticeBoardPermission: view granted')
            return True
        else:
            print(f'[DEBUG] NoticeBoardPermission: no permissions found')
        return False


class CompanyConfigurationsPermission(BaseModulePermission):
    """
    Permission class for company configurations.
    """
    def __init__(self, action='view', scope='company'):
        super().__init__('company_configurations', action, scope)

    def has_permission(self, request, view):
        # For create operations, check create permission
        if request.method == 'POST' and not view.action == 'reset_config' and not view.action == 'sync_with_global':
            return self._check_create_permission(request.user)
        elif request.method == 'GET' and view.action == 'list':
            # For list operations, check if user has any view permissions
            return self._check_view_permission(request.user, None)
        # For custom actions, check specific permissions
        elif hasattr(view, 'action'):
            if view.action == 'reset_config':
                print("DEBUG: reset_config")
                return self._check_reset_permission(request.user, None)
            elif view.action == 'sync_with_global':
                return self._check_sync_admin_permission(request.user, None)
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
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'create', self.scope, {}):
            return True
        return False
    
    def _check_update_permission(self, user, obj):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'update', self.scope, {}):
            return True
        return False
    
    def _check_delete_permission(self, user, obj):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'delete', self.scope, {}):
            return True
        return False
    
    def _check_view_permission(self, user, obj):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'view', self.scope, {}):
            return True
        return False

    def _check_sync_admin_permission(self, user, obj):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'sync_admin', self.scope, {}):
            return True
        return False

    def _check_reset_permission(self, user, obj):
        if self._check_permission(user, 'company_configurations', 'full_access', self.scope, {}):
            return True
        elif self._check_permission(user, 'company_configurations', 'reset', self.scope, {}):
            return True
        return False
