from django.db import connection
from django.db.models import Q
from django.contrib.auth.models import AnonymousUser
from django.http import JsonResponse
from django.shortcuts import get_object_or_404

from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from project_management.utils import get_product_name_from_request

from rest_framework import viewsets, status, generics, filters
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.response import Response
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi

from companyprofileapis.serializers import CompanyGetProfileSerializer, UserSerializer
from project_management.models import Milestone, ActionItem, Project
from human_resource_management.models import LeaveRequest
from human_resource_management.serializers import LeaveRequestSerializer
from ecpmpapp.utils import reschedule_event, get_frontend_url_for_solution, get_user_active_solution
from ecpmpapp.license_queries import (
    get_active_license_sql_condition,
    get_active_license_user_ids_by_product_names,
)
from ecpmpapp.ordering import TableOrderingFilter
from ecpmpapp.constants import MS_OAUTH_AUTHORIZE_URL, UserType, StatusType
from models.models import User, Connection, ConnectionUser
from userapis.models import UserDashboardConfig
from userapis.serializers import UserDashboardConfigSerializer, PublicUserSerializer, UserRoleSerializer
from userapis.filters import UserSearchFilter, AssociatedUsersFilter
from userapis.permissions import ResourcesPermission

def api_login_required(view_func):
    def _wrapped_view(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        return JsonResponse({"error": "Authentication required"}, status=401)

    return _wrapped_view


def get_user_license_info(user_id, product_name):
    active_license_condition = get_active_license_sql_condition()
    query = f"""
        SELECT pkg.id, pkg.expires_at, pkg.status, pkg.billing_plan, pkg.is_freetrial
        FROM package_license_license AS l
        JOIN package_license_product AS p ON l.product_id = p.id
        JOIN package_license_package AS pkg ON l.package_id = pkg.id
        WHERE l.user_id = %s
            AND p.name = %s
            AND {active_license_condition}
        ORDER BY pkg.expires_at ASC
        LIMIT 1;
    """

    with connection.cursor() as cursor:
        cursor.execute(query, [user_id, product_name])
        result = cursor.fetchone()

    if not result:
        return None

    return {
        "id": result[0],
        "renewal_date": result[1],
        "expires_at": result[1],
        "status": result[2],
        "billing_plan": result[3],
        "is_freetrial": result[4],
        "auto_renewal": result[2] == "Auto Renew",
        "product_name": product_name,
    }


class UserInfoViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    filterset_class = AssociatedUsersFilter

    def get_permissions(self):
        if self.action == 'projects_with_action_items':
            return [ResourcesPermission(action='view', scope='company')]
        return super().get_permissions()

    def get_queryset(self):
        """
        Allow users to view only their own data.
        """
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return User.objects.none()

        if user.is_freelancer:
            return User.objects.filter(id=user.id)

        if hasattr(user, UserType.COMPANY.value):
            return User.objects.filter(company=user.company)

        return User.objects.none()

    def get_object(self):
        obj = get_object_or_404(User, pk=self.kwargs['pk'])
        user = self.request.user

        if user.is_freelancer and obj.id != user.id:
            raise PermissionDenied("Freelancers can only access their own account.")

        if hasattr(user, UserType.COMPANY.value) and obj.company != user.company:
            raise PermissionDenied("You do not have permission to access this user's profile.")
        return obj

    def list(self, request, *args, **kwargs):
        """
        Get user info (retrieve view).
        """
        user = self.request.user
        product_name = get_product_name_from_request(request)
        license_info = get_user_license_info(user.id, product_name)
        company = user.company
        company_data = CompanyGetProfileSerializer(company).data if company else None
        user_data = UserSerializer(user).data

        include_user_roles = request.query_params.get("include_user_roles", "true").lower() not in {
            "0",
            "false",
            "no",
        }

        user_roles = []
        if include_user_roles:
            user_roles = user.user_company_roles.select_related(
                "user",
                "assigned_by",
                "role",
                "role__company",
                "role__freelancer",
            ).prefetch_related("role__role_permissions__permission")
        
        has_admin_access = user.is_superuser or user.groups.filter(name__in=[
            UserType.ADMIN.value,
            UserType.COMPANY_ADMIN.value,
            UserType.COMPANY_OWNER.value
        ]).exists()
        user_data['has_admin_access'] = has_admin_access

        return Response({
            "user": user_data,
            "company": company_data,
            "user_roles": UserRoleSerializer(user_roles, many=True).data,
            "license": license_info,
        })

    def update(self, request, *args, **kwargs):
        """
        Full update (PUT) user info - only self can update.
        """
        # Ensure the user can only update their own information
        if request.user.id != int(self.kwargs['pk']):
            return Response({"error": "You can only update your own information."}, status=status.HTTP_403_FORBIDDEN)

        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        """
        Partial update (PATCH) user info - only self can update.
        """
        print(request.user.id)
        print(self.kwargs)
        # Ensure the user can only update their own information
        if request.user.id != int(self.kwargs['pk']):
            return Response({"error": "You can only update your own information."}, status=status.HTTP_403_FORBIDDEN)

        return super().partial_update(request, *args, **kwargs)

    # Disallow user creation and deletion
    def create(self, request, *args, **kwargs):
        """
        Disable the ability to create users.
        """
        return Response({"error": "User creation is not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        """
        Disable the ability to delete users.
        """
        return Response({"error": "User deletion is not allowed."}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    @action(detail=True, methods=["get"])
    def projects_with_action_items(self, request, pk=None):
        """Return projects where the user is a team member and assigned action items."""
        from project_management.serializers import ActionItemSerializer

        request_user = request.user
        user = get_object_or_404(User, id=pk)

        # **Permission Checks**
        # Freelancers can only access their own data
        if request_user.is_freelancer and user != request_user:
            return Response({"error": "Freelancers can only access their own data."}, status=403)

        # Company users need resources permission to access other users' data
        if not request_user.is_freelancer:
            # Check if user is trying to access data from a different company
            if request_user.company and request_user.company != user.company:
                return Response({"error": "You can only access data for your own company."}, status=403)
            
            permission = ResourcesPermission(action='view', scope='company')
            if not permission.has_permission(request, self):
                return Response({"error": "You need resources permission to access other users' data."}, status=403)

        # Fetch projects where the user is a team member
        projects = Project.objects.filter(
            Q(team_members__team_member=user) | Q(user=user)
        ).distinct()

        # Build response
        response_data = []
        for project in projects:
            action_items_list = []
            action_items = ActionItem.objects.filter(
                Q(milestone__project=project, assignee=user) | Q(parent_action_item__milestone__project=project, assignee=user)
            ).distinct()
            for action_item in action_items:
                if action_item.parent_action_item:
                    ac = ActionItemSerializer(action_item.parent_action_item).data
                    action_items_list.append(ac)
                else:
                    action_items_list.append(ActionItemSerializer(action_item).data)
            response_data.append({project.project_name: action_items_list})

        return Response(response_data, status=200)

    def get_accepted_connections(self, links_id, target_type):
        connected_companies = Connection.objects.filter(
            Q(
                Q(initiator_id=links_id, target_type=target_type) |
                Q(target_id=links_id, initiator_type=target_type)
            ),
            status=StatusType.ACCEPTED.value
        ).values_list("initiator_id", "target_id")

        # Extract connected company/freelancer IDs excluding the request user's company
        return {
            company_id
            for initiator_id, target_id in connected_companies
            for company_id in [initiator_id, target_id]
            if company_id != links_id
        }
    
    @swagger_auto_schema(
        method='get',
        manual_parameters=[
            openapi.Parameter(
                'email', openapi.IN_QUERY,
                description="Return only users whose email contains this text",
                type=openapi.TYPE_STRING
            ),
            openapi.Parameter(
                'name', openapi.IN_QUERY,
                description="Return only users whose first or last name contains this text",
                type=openapi.TYPE_STRING
            ),
            openapi.Parameter(
                'usertype', openapi.IN_QUERY,
                description="Either 'freelancer' or 'internal_company_users'",
                type=openapi.TYPE_STRING,
                enum=['freelancer', 'company', 'internal_company_users']
            ),
            openapi.Parameter(
                'availability', openapi.IN_QUERY,
                description="'true' = only users with zero active projects; 'false' = only users with at least one",
                type=openapi.TYPE_BOOLEAN
            ),
        ]
    )
    @action(detail=False, methods=["get"])
    def get_associated_users(self, request):
        user = request.user
        
        include_deleted = request.query_params.get('include_deleted', '').lower() in ['true', '1']
        include_unlicensed = request.query_params.get('include_unlicensed', '').lower() in ['true', '1']
        include_inactive = request.query_params.get('include_inactive', '').lower() in ['true', '1']
        include_permanently_deleted = request.query_params.get('is_permanently_deleted', '').lower() in ['true', '1']
        
        if user.company:
            company_links_id = user.company.sharemundo_links_id
            
            # Get all companies connected to the request user's company
            connection_user_ids = ConnectionUser.objects.filter(
                Q(status=StatusType.ACCEPTED.value) &
                Q(connection__initiator_type=UserType.COMPANY.value, connection__target_type=UserType.COMPANY.value) &
                Q(Q(connection__initiator_id=company_links_id) | Q(connection__target_id=company_links_id)) &
                ~Q(user__company=user.company)
            ).values_list('user__id', flat=True)
            
            # Build base filter conditions WITHOUT status filters initially
            associated_internal_users = User.objects.filter(id__in=connection_user_ids).distinct()
            
            connected_freelancer_ids = self.get_accepted_connections(user.company.sharemundo_links_id, "freelancer")
            connected_freelancer_objs = User.objects.filter(sharemundo_links_id__in=connected_freelancer_ids).distinct()
            
            internal_company_users = User.objects.filter(company=user.company).distinct()
            
            associated_users = connected_freelancer_objs | associated_internal_users | internal_company_users
            
        # Case 2: User is a freelancer
        elif user.is_freelancer:
            connected_freelancer_ids = self.get_accepted_connections(user.sharemundo_links_id, UserType.FREELANCER.value)
            
            associated_users = User.objects.filter(
                Q(sharemundo_links_id__in=connected_freelancer_ids) | Q(id=user.id)
            )
            
        else:
            return Response(
                {"error": "Invalid role. Only company users or freelancers can access this data."},
                status=400
            )
        
        if not include_unlicensed:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION

            active_license_user_ids = get_active_license_user_ids_by_product_names(products)
            
            if active_license_user_ids:
                associated_users = associated_users.filter(id__in=active_license_user_ids)
            else:
                associated_users = associated_users.none()
        
        
        status_filter_conditions = Q()
        
        if not include_inactive:
            status_filter_conditions &= Q(is_active=True)
        if not include_deleted:
            status_filter_conditions &= Q(is_deleted=False)
        if not include_permanently_deleted:
            status_filter_conditions &= Q(is_permanently_deleted=False)
        
        if status_filter_conditions:
            associated_users = associated_users.filter(status_filter_conditions)
        
        associated_users = AssociatedUsersFilter(
            data=request.query_params,
            queryset=associated_users,
            request=request
        ).qs
        
        serializer = UserSerializer(associated_users, many=True)
        return Response(serializer.data)

class UserSearchView(generics.ListAPIView):
    queryset = User.objects.filter(user_searchable=True)
    serializer_class = PublicUserSerializer
    filterset_class = UserSearchFilter
    filter_backends = [DjangoFilterBackend, TableOrderingFilter]
    ordering_fields = [
        "first_name",
        "last_name",
        "company__company_name",
        "mobile_phone",
        "sharemundo_links_id",
        "company__sharemundo_links_id",
        "city",
        "post_code",
        "professional_headline",
        "availability_location",
        "qualifications",
        "spoken_languages",
        "open_to_new_challenges",
        "main_area_of_expertise",
        "sub_area_of_expertise",
    ]
    ordering_natural_fields = {
        "first_name",
        "last_name",
        "company__company_name",
        "mobile_phone",
        "sharemundo_links_id",
        "company__sharemundo_links_id",
        "city",
        "post_code",
        "professional_headline",
        "availability_location",
        "qualifications",
    }
    ordering_localized_fields = {
        "main_area_of_expertise",
        "sub_area_of_expertise",
    }
    ordering_localized_array_fields = {"sub_area_of_expertise"}

    def get_queryset(self):
        queryset = User.objects.filter(user_searchable=True)

        user = self.request.user
        if user.company:
            queryset = queryset.exclude(id__in=user.company.users.all().values_list("id", flat=True))

        if user.is_freelancer:
            queryset = queryset.exclude(id=user.id)

        filter_data_present = any(
            self.request.query_params.get(field)
            for field in self.filterset_class.get_filters().keys()
        )

        if filter_data_present:
            return queryset.order_by('-id')

        return queryset.none()


class UserDashboardConfigurationViewSet(viewsets.ModelViewSet):
    queryset = UserDashboardConfig.objects.all()
    serializer_class = UserDashboardConfigSerializer

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return UserDashboardConfig.objects.none()
        return UserDashboardConfig.objects.filter(user=user)

    def perform_create(self, serializer):
        if UserDashboardConfig.objects.filter(user=self.request.user).exists():
            raise PermissionDenied("You already have a dashboard configured.")
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        if serializer.instance.user != self.request.user:
            raise PermissionDenied("You are not allowed to update this dashboard.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.user != self.request.user:
            raise PermissionDenied("You are not allowed to delete this dashboard.")
        instance.delete()
