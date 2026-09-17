
from rest_framework.generics import ListAPIView
from django.contrib.auth.models import AnonymousUser

from models.models import GlobalConfig
from project_management.serializers import GlobalConfigSerializer
from project_management.permissions import GlobalConfigPermission
from ecpmpapp.utils import require_any_permission_multiple_modules

class GlobalConfigListView(ListAPIView):
    serializer_class = GlobalConfigSerializer
    
    def get_permissions(self):
        # For ListAPIView, we only have 'list' action
        return [GlobalConfigPermission(action='view', scope='company')]

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return GlobalConfig.objects.none()
        has_company_permissions = require_any_permission_multiple_modules(
            self.request.user, 
            {'global_configurations': ['view', 'full_access']}, 
            'company', 
            self.request.query_params
        )
        if has_company_permissions:
            return GlobalConfig.objects.filter(company_profile=self.request.user.company).order_by('-id')
        elif self.request.user.is_freelancer:
            return GlobalConfig.objects.filter(user=self.request.user).order_by('-id')
        return GlobalConfig.objects.none()
