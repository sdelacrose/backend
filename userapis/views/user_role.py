from rest_framework import viewsets
from userapis.serializers import UserRoleSerializer
from models.models import UserRole
from rest_framework.exceptions import PermissionDenied
from userapis.filters import UserRoleFilter

class UserRoleViewSet(viewsets.ModelViewSet):
    queryset = UserRole.objects.all()
    serializer_class = UserRoleSerializer
    ordering_fields = ['created_at', 'updated_at']
    ordering = ['-created_at']
    filterset_class = UserRoleFilter
    # Read-only on the solution: role assignment is managed by admin-server-backend
    # (RolesPermission-protected). Any write verb answers 405 and never reaches the serializer.
    http_method_names = ['get', 'head', 'options']

    #TODO: Add permissions check here
    def get_queryset(self):
        return (
            self.queryset.filter(user=self.request.user)
            .select_related(
                "user",
                "assigned_by",
                "role",
                "role__company",
                "role__freelancer",
            )
            .prefetch_related("role__role_permissions__permission")
        )
    
    def get_object(self):
        obj = super().get_object()
        request = self.request
        if obj.user != request.user:
            raise PermissionDenied("You do not have permission to access this user role")
        return obj
