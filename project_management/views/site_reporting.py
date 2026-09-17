from project_management.models.constants import COLLABORATIVE_SOLUTION
from rest_framework import viewsets, filters
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models import CharField, Count, F, IntegerField, Q, Value
from django.db.models.functions import Coalesce, Concat, NullIf
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.conf import settings
from rest_framework.response import Response
from project_management.models import SiteReport
from project_management.serializers import SiteReportSerializer
from project_management.permissions import SiteReportingPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.constants import UserType
from project_management.utils import get_product_name_from_request
from ecpmpapp.ordering import TableOrderingFilter


class SiteReportViewSet(viewsets.ModelViewSet):
    queryset = SiteReport.objects.all().order_by('-id')
    serializer_class = SiteReportSerializer
    filterset_fields = ['project']
    filter_backends = [
        DjangoFilterBackend,
        TableOrderingFilter,
        filters.SearchFilter,
    ]
    search_fields = ['name']
    ordering_fields = [
        "name",
        "date_time",
        "observer__team_member__first_name",
        "observer__team_member__last_name",
        "observer_name",
        "num_attendees",
        "attendee_sort_count",
        "num_observations",
        "id",
    ]
    ordering = ["-id"]
    ordering_text_fields = {"name", "observer_name"}
    ordering_natural_fields = {"name", "observer_name"}

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [SiteReportingPermission(action='view', scope='project')]
        elif self.action == 'create':
            return [SiteReportingPermission(action='create', scope='project')]
        elif self.action in ['update', 'partial_update']:
            return [SiteReportingPermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [SiteReportingPermission(action='delete', scope='project')]
        return super().get_permissions()

    def get_queryset(self):
        query_filters = Q()

        user = self.request.user
        if isinstance(user, AnonymousUser):
            return SiteReport.objects.none()
        
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

        has_site_reporting_project_permissions = require_any_permission_multiple_modules(
            user, 
            {'site_reporting': ['view', 'full_access']}, 
            'project', 
            self.request.query_params
        )

        has_site_reporting_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'site_reporting': ['view_assigned_created', 'update_assigned_created', 'delete_assigned_created']}, 
            'site_reporting', 
            self.request.query_params
        )
        if not user.is_freelancer and has_projects_company_permissions:
            query_filters = (
                Q(project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))
            )
        elif has_site_reporting_project_permissions:
            query_filters = Q(project__team_members__team_member=user, project__solution__in=products)
        elif has_site_reporting_created_permissions:
            query_filters = query_filters | Q(created_by=user) | Q(assignees=user)
        else:
            return SiteReport.objects.none()

        queryset = SiteReport.objects.filter(query_filters).distinct()
        queryset = queryset.annotate(
            observer_name=Concat(
                Coalesce(F("observer__team_member__first_name"), Value("")),
                Value(" "),
                Coalesce(F("observer__team_member__last_name"), Value("")),
                output_field=CharField(),
            ),
            num_attendees=Count('present', distinct=True),
            attendee_sort_count=NullIf(
                Count("present", distinct=True),
                Value(0),
                output_field=IntegerField(),
            ),
            num_observations=NullIf(
                Count("observations", distinct=True),
                Value(0),
                output_field=IntegerField(),
            )
        )
        # Apply name search filter if a search term is provided
        search_term = self.request.query_params.get('search', None)
        if search_term:
            queryset = queryset.filter(name__icontains=search_term)
        return queryset.order_by('-id')


    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data

        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)

    def get_object(self):
        obj = get_object_or_404(SiteReport, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj
