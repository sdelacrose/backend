from rest_framework import serializers
from models.models import Role, UserRole, Permission


class UserRoleSerializer(serializers.ModelSerializer):
    role = serializers.PrimaryKeyRelatedField(queryset=Role.objects.all())
    class Meta:
        model = UserRole
        fields = [
            'id', 'user', 'role', 
            'assigned_by', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'assigned_by', 'created_at', 'updated_at']

    def validate_role(self, value):
        request_user = self.context['request'].user
        if value.company != request_user.company:
            raise serializers.ValidationError("Role does not belong to the same company")

        if value.scope == "project":
            raise serializers.ValidationError("Project roles cannot be assigned to users from company level")

        return value

    def validate_user(self, value):
        """L01-R: symmetric to validate_role above -- the ROLE side was
        already checked, but the BENEFICIARY (the assignee) never was, on
        EITHER create or update (DRF runs validate_<field> whenever that
        field is present in the payload, so this one method closes both
        paths at once). Same scoping as create()'s own freelancer/company
        branch: a company actor may only target a user of their OWN
        company; a freelancer actor may only ever target themselves (there
        is no notion of a freelancer having other members)."""
        request_user = self.context['request'].user
        if request_user.is_freelancer:
            if value.id != request_user.id:
                raise serializers.ValidationError("User does not belong to the same company")
        elif value.company_id != request_user.company_id:
            raise serializers.ValidationError("User does not belong to the same company")
        return value

    def validate(self, attrs):
        request_user = self.context['request'].user
        freelancer = None
        company = None
        if request_user.is_freelancer:
            freelancer = request_user
        else:
            company = request_user.company
        assignee = attrs['user']
        if attrs['role'].scope == "company":
            if UserRole.objects.filter(user=assignee, role__scope=attrs['role'].scope, role__type=attrs['role'].type, freelancer=freelancer, company=company).exists():
                raise serializers.ValidationError("User can only have one role per scope and type at company level")
        return attrs

    def create(self, validated_data):
        request_user = self.context['request'].user
        if request_user.is_freelancer:
            validated_data['freelancer'] = request_user
            validated_data['company'] = None
        else:
            validated_data['company'] = request_user.company
            validated_data['freelancer'] = None
        validated_data['assigned_by'] = request_user
        return super().create(validated_data)

    def _get_all_module_permissions(self, module, scope, type):
        """Get all possible permissions for a module"""
        cache = self.context.setdefault("_role_permission_template_cache", {})
        cache_key = (scope, type)
        if cache_key not in cache:
            template = {}
            permissions = (
                Permission.objects.filter(
                    role_permissions__role__scope=scope,
                    role_permissions__role__type=type,
                )
                .values_list("module", "key")
                .distinct()
            )
            for permission_module, permission_key in permissions:
                permission_name = (
                    permission_key.split(".", 1)[1]
                    if "." in permission_key
                    else permission_key
                )
                template.setdefault(permission_module, []).append(permission_name)
            cache[cache_key] = template
        return cache[cache_key].get(module, [])


    def get_permissions_json(self, instance):
        permissions_json = {}

        role_permissions = instance.role.role_permissions.all()

        # Group permissions by module
        module_permissions = {}
        for role_permission in role_permissions:
            module = role_permission.permission.module
            permission_key = (
                role_permission.permission.key.split(".", 1)[1]
                if "." in role_permission.permission.key
                else role_permission.permission.key
            )
            
            if module not in module_permissions:
                module_permissions[module] = set()
            
            module_permissions[module].add(permission_key)
        
        
        # Convert to JSON format
        for module in module_permissions.keys():
            permissions_json[module] = {}
            
            # Define all possible permissions for this module
            all_permissions = self._get_all_module_permissions(module, instance.role.scope, instance.role.type)
            granted_permissions = module_permissions.get(module, set())
            for permission in all_permissions:
                permissions_json[module][permission] = permission in granted_permissions
        
        return permissions_json
    
    
    def to_representation(self, instance):
        representation = super().to_representation(instance)
        
        if instance.user:
            representation['user'] = {
                'id': instance.user.id,
                'name': instance.user.full_name,
                'email': instance.user.email
            }
            
        if instance.assigned_by:
            representation['assigned_by'] = {
                'id': instance.assigned_by.id,
                'name': instance.assigned_by.full_name,
                'email': instance.assigned_by.email
            }
        
        if instance.role:
            representation['role'] = {
                'id': instance.role.id,
                'name': instance.role.name,
                'description': instance.role.description,
                'scope': instance.role.scope,
                'type': instance.role.type,
                'is_default': instance.role.is_default,
                'company': {
                    'id': instance.role.company.id,
                    'name': instance.role.company.company_name,
                } if instance.role.company else None,
                'freelancer': {
                    'id': instance.role.freelancer.id,
                    'name': instance.role.freelancer.full_name,
                } if instance.role.freelancer else None,
                'permissions_json': self.get_permissions_json(instance),
            }
            
        return representation 
