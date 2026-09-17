from rest_framework import serializers
from models.models import Permission, Role, RolePermission, UserRole


class RoleSerializer(serializers.ModelSerializer):
    permissions_json = serializers.JSONField(
        write_only=True,
        required=False,
        help_text="JSON object with module permissions structure"
    )

    class Meta:
        model = Role
        fields = [
            'id', 'name', 'description', 'company', 'type', 'scope', 'is_default', 
            'is_active', 'permissions_json', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'is_default', 'created_at', 'updated_at', 'company']

    def validate_permissions_json(self, value):
        """Validate the permissions JSON structure"""
        if not isinstance(value, dict):
            raise serializers.ValidationError("Permissions must be a JSON object")
        scope = self.initial_data.get('scope') if self.initial_data.get('scope') else self.instance.scope
        type = self.initial_data.get('type') if self.initial_data.get('type') else self.instance.type

        if not scope or not type:
            raise serializers.ValidationError("Scope and type are required")
        
        # Define expected module structure
        expected_modules = {}
        permissions = Permission.objects.filter(role_permissions__role__scope=scope, role_permissions__role__type=type) 
        for permission in permissions:
            module = permission.module
            if module not in expected_modules:
                expected_modules[module] = {}
            expected_modules[module][permission.key.split('.')[1]] = False

        for module, permissions in value.items():
            if module not in expected_modules:
                raise serializers.ValidationError(f"Unknown module: {module}")
            
            if not isinstance(permissions, dict):
                raise serializers.ValidationError(f"Module {module} permissions must be an object")
            
            for permission, enabled in permissions.items():
                if permission not in expected_modules[module]:
                    raise serializers.ValidationError(f"Unknown permission {permission} for module {module}")
                
                if not isinstance(enabled, bool):
                    raise serializers.ValidationError(f"Permission {permission} value must be boolean")
        
        return value

    def _convert_json_to_permission_keys(self, permissions_json, scope, type):
        """Convert JSON permissions to permission keys"""
        permission_keys = []
        
        for module, permissions in permissions_json.items():
            for permission, enabled in permissions.items():
                if enabled:
                    permission_key = f"{module}.{permission}"
                    permission_keys.append(permission_key)
        
        return list(set(permission_keys))  # Remove duplicates

    def create(self, validated_data):
        request_user = self.context['request'].user
        validated_data['company'] = request_user.company
        permissions_json = validated_data.pop('permissions_json', {})
        role = super().create(validated_data)
        
        # Convert JSON permissions to permission keys and assign them
        if permissions_json:
            permission_keys = self._convert_json_to_permission_keys(permissions_json, role.scope, role.type)
            
            for permission_key in permission_keys:
                try:
                    permission = Permission.objects.get(key=permission_key)
                    RolePermission.objects.get_or_create(role=role, permission=permission)
                except Permission.DoesNotExist:
                    raise serializers.ValidationError(f"Permission {permission_key} not found")
        
        return role

    def update(self, instance, validated_data):
        if instance.is_default:
            raise serializers.ValidationError("Default roles cannot be updated")

        permissions_json = validated_data.pop('permissions_json', None)
        role = super().update(instance, validated_data)
        
        if permissions_json is not None:
            # Convert JSON to permission keys
            permission_keys = self._convert_json_to_permission_keys(permissions_json, role.scope, role.type)
            # Get existing permission keys for this role
            existing_permission_keys = set(
                role.role_permissions.values_list('permission__key', flat=True)
            )
            # Get new permission keys from JSON
            new_permission_keys = set(permission_keys)
            
            # Find permissions to add (in new but not in existing)
            permissions_to_add = new_permission_keys - existing_permission_keys
            # Find permissions to remove (in existing but not in new)
            permissions_to_remove = existing_permission_keys - new_permission_keys
            # Add new permissions
            if permissions_to_add:
                permissions_to_add_objects = Permission.objects.filter(key__in=permissions_to_add)
                role_permissions = [
                    RolePermission(role=role, permission=permission)
                    for permission in permissions_to_add_objects
                ]
                RolePermission.objects.bulk_create(role_permissions)
            # Remove permissions that are no longer needed
            if permissions_to_remove:
                role.role_permissions.filter(
                    permission__key__in=permissions_to_remove
                ).delete()
        return role

    def to_representation(self, instance):
        """Convert role permissions to JSON format for API response"""
        representation = super().to_representation(instance)
        
        # Convert permissions to JSON format
        permissions_json = {}
        
        # Get all permissions for this role
        role_permissions = instance.role_permissions.select_related('permission').all()
        
        # Group permissions by module
        module_permissions = {}
        for role_permission in role_permissions:
            module = role_permission.permission.module
            permission_key = role_permission.permission.key
            
            if module not in module_permissions:
                module_permissions[module] = []
            
            module_permissions[module].append(permission_key)
        
        
        # Convert to JSON format
        for module, permissions in module_permissions.items():
            permissions_json[module] = {}
            
            # Define all possible permissions for this module
            all_permissions = self._get_all_module_permissions(module, instance.scope, instance.type)
            for permission in all_permissions:
                permission_key = f"{module}.{permission}"
                permissions_json[module][permission] = permission_key in permissions
        
        representation['permissions_json'] = permissions_json
        return representation

    def _get_all_module_permissions(self, module, scope, type):
        """Get all possible permissions for a module"""
        permissions = Permission.objects.filter(role_permissions__role__scope=scope, role_permissions__role__type=type, module=module)
        return [permission.key.split('.')[1] for permission in permissions]
