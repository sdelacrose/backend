from rest_framework import viewsets
from companyprofileapis.serializers import RoleSerializer
from companyprofileapis.filters import RoleFilter
from models.models import Role
from rest_framework.exceptions import PermissionDenied
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from rest_framework.response import Response
from models.models import Permission
from rest_framework.decorators import action
    
class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.all()
    serializer_class = RoleSerializer
    search_fields = ['name', 'description']
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']
    filterset_class = RoleFilter

    #TODO: Add permissions check here
    def get_queryset(self):
        request_user = self.request.user
        if request_user.is_freelancer:
            return self.queryset.filter(freelancer=request_user)
        else:
            return self.queryset.filter(company=request_user.company)

    def perform_create(self, serializer):
        return PermissionDenied("Role Creation is not allowed on solution.")
    
    def perform_update(self, serializer):
        return PermissionDenied("Role Update is not allowed on solution.")
    
    def perform_destroy(self, instance):
        return PermissionDenied("Role Deletion is not allowed on solution.")
    
    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'type',
                openapi.IN_QUERY,
                description="Type of permission to get",
                type=openapi.TYPE_STRING,
                required=True
            ),
            openapi.Parameter(
                'scope',
                openapi.IN_QUERY,
                description="Scope of permission to get",
                type=openapi.TYPE_STRING,
                required=True
            )
        ]
    )

    @action(detail=False, methods=['get'], url_path='get-permissions-template')
    def get_permissions_template(self, request):
        # Get query parameters
        permission_type = request.query_params.get('type')
        scope = request.query_params.get('scope')
        
        # Build optimized queryset with select_related to avoid N+1 queries
        queryset = Permission.objects.all()
        
        # Apply filters directly in the queryset for better performance
        if permission_type:
            queryset = queryset.filter(role_permissions__role__type=permission_type)
        if scope:
            queryset = queryset.filter(role_permissions__role__scope=scope)
        
        # Use distinct to avoid duplicate permissions
        queryset = queryset.distinct()
        
        # Only select the fields we need
        permissions_data = queryset.values('module', 'key')
        
        # Create the response structure directly without serialization overhead
        permissions_by_module = {}
        for permission in permissions_data:
            module = permission['module']
            if module not in permissions_by_module:
                permissions_by_module[module] = {}
            
            # Extract the permission action from the key (e.g., "action_item.view" -> "view")
            permission_action = permission['key'].split('.')[1] if '.' in permission['key'] else permission['key']
            permissions_by_module[module][permission_action] = False

        return Response(permissions_by_module)

