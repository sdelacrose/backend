from project_management.models.constants import COLLABORATIVE_SOLUTION

from rest_framework import viewsets
from django.contrib.auth.models import AnonymousUser
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.db.models import BooleanField, Case, CharField, F, Q, Value, When
from django.db.models.functions import Coalesce, Concat
from django.core.exceptions import PermissionDenied
from project_management.models import (
    ProjectPhase
)
from project_management.serializers import (
    ProjectPhaseSerializer
)
from project_management.filters import ProjectPhaseFilter
from project_management.utils import (
    get_product_name_from_request,
    get_project_id_from_request,
)
from project_management.permissions import MilestonePermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter

class ProjectPhaseViewSet(viewsets.ModelViewSet):
    queryset = ProjectPhase.objects.all()
    serializer_class = ProjectPhaseSerializer
    filter_backends = [DjangoFilterBackend, TableOrderingFilter]
    filterset_class = ProjectPhaseFilter
    ordering_fields = [
        "name",
        "date",
        "created_by",
        "created_by_name",
        "created_at",
        "status",
        "is_payment",
        "linked_purchase_orders",
        "po_type",
        "id",
    ]
    ordering = ["-created_at"]
    ordering_text_fields = {
        "name",
        "created_by_name",
        "status",
        "linked_purchase_orders",
        "po_type",
    }
    ordering_natural_fields = {
        "name",
        "created_by_name",
        "linked_purchase_orders",
    }

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [MilestonePermission(action='view', scope='project')]
        elif self.action == 'create':
            return [MilestonePermission(action='create', scope='project')]
        elif self.action in ['update', 'partial_update']:
            return [MilestonePermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [MilestonePermission(action='delete', scope='project')]
        return super().get_permissions()

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ProjectPhase.objects.none()
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['full_access', 'view_all_access']}, 
            'company', 
            self.request.query_params
        )
        project_id = get_project_id_from_request(self.request)
        has_phases_permission = require_any_permission_multiple_modules(
            user, 
            {'milestone_management': ['view', 'full_access']}, 
            'project', 
            {'project': project_id}
        )
        query_filters = Q()
        if not user.is_freelancer and has_projects_company_permissions:
            query_filters = (
                Q(project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
            )
        elif has_phases_permission:
            query_filters = Q(project__team_members__team_member=user, project__solution__in=products)
        else:
            return ProjectPhase.objects.none()
        
        queryset = (
            ProjectPhase.objects.filter(query_filters)
            .select_related('purchase_order')
            .distinct()
        )
        
        # `project` is canonical; `projectId` remains supported for old clients.
        if project_id:
            queryset = queryset.filter(project_id=project_id)

        queryset = queryset.annotate(
            created_by_name=Concat(
                Coalesce(F("created_by__first_name"), Value("")),
                Value(" "),
                Coalesce(F("created_by__last_name"), Value("")),
                output_field=CharField(),
            ),
            is_payment=Case(
                When(amount__isnull=False, amount__gt=0, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            ),
            # Sort key for linked PO column (serializer still returns nested objects).
            linked_purchase_orders=F("purchase_order__reference"),
            po_type=F("purchase_order__po_type"),
        )
        
        return queryset.order_by('-id')

    def get_serializer(self, *args, **kwargs):
        serializer = super(ProjectPhaseViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        serializer.context['request'] = self.request
        return serializer

    def get_object(self):
        obj = get_object_or_404(ProjectPhase, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        product_name = get_product_name_from_request(self.request)

        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]

        if obj.project.solution not in products:
            raise PermissionDenied("You do not have permission to access this project phase.")

        return obj
