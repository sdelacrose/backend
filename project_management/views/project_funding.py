from project_management.models.constants import COLLABORATIVE_SOLUTION

from rest_framework import viewsets
from django.contrib.auth.models import AnonymousUser

from rest_framework import viewsets
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import OrderingFilter
from django.shortcuts import get_object_or_404
from django.db.models import Q, Count, Min
from django.core.exceptions import PermissionDenied
from project_management.models import (
    ProjectFunding
)
from project_management.serializers import (
    ProjectFundingSerializer
)
from project_management.utils import get_product_name_from_request
from project_management.permissions import FundingPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.constants import UserType

class ProjectFundingViewSet(viewsets.ModelViewSet):
    queryset = ProjectFunding.objects.all()
    serializer_class = ProjectFundingSerializer
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['project']
    ordering_fields = ['id', 'project', 'name', 'budget', 'created_by', 'created_at', 'updated_at', 'document_type', 'num_documents']
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [FundingPermission(action='view', scope='project')]
        elif self.action == 'create':
            return [FundingPermission(action='create', scope='project')]
        elif self.action in ['update', 'partial_update']:
            return [FundingPermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [FundingPermission(action='delete', scope='project')]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ProjectFunding.objects.none()
        
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]

        # Extract projectId and create project dict
        project_id = self.request.query_params.get('projectId')
        project_dict = {'project': project_id} if project_id else {}

        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            project_dict
        )

        has_funding_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'funding': ['view', 'full_access']}, 
            'project', 
            project_dict
        )

        has_funding_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'funding': ['view_created', 'update_created', 'delete_created']}, 
            'project', 
            project_dict 
        )
        
        query_filters = Q()
        if not user.is_freelancer and has_projects_company_permissions:
            query_filters = (
                Q(project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
            )
        elif has_funding_project_permissions:
            query_filters = Q(project__team_members__team_member=user, project__solution__in=products)
        elif has_funding_created_permissions:
            query_filters = query_filters | Q(created_by=user)
        else:
            return ProjectFunding.objects.none()
        
        queryset = ProjectFunding.objects.filter(query_filters).distinct()
        
        # Apply project filter if projectId is provided
        if project_id:
            queryset = queryset.filter(project_id=project_id)
        
        # Annotate with document_type (from the first document) and number of documents
        queryset = queryset.annotate(
            document_type=Min('documents__document_type'),
            num_documents=Count('documents', distinct=True)
        )
        
        return queryset

    def get_serializer(self, *args, **kwargs):
        serializer = super(ProjectFundingViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['request'] = self.request
        return serializer

    def get_object(self):
        obj = get_object_or_404(ProjectFunding, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj
