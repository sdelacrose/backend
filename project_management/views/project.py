import mimetypes
from datetime import datetime
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from django.db.models import Min
from rest_framework import viewsets
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework import status, serializers
from rest_framework.parsers import MultiPartParser
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings

from rest_framework import viewsets, filters
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django_filters.rest_framework import DjangoFilterBackend
from django.utils.decorators import method_decorator
from django.shortcuts import get_object_or_404
from django.db.models import Sum, Q
from django.core.exceptions import PermissionDenied
from models.models import GlobalConfig, ConnectionUser, CompanyProfile, User, Staff
from project_management.models import (
    Milestone, Project, ECPMPConfig, ActionItem, 
    ObjectAssessment, ProjectTeamMembers, ProjectTime, Folder,
    TimeCard, ExpenseCard, ProjectPhase, ProjectFunding, PurchaseOrder
)
from project_management.filters import MilestoneFilter, ActionItemFilter, ObjectAssessmentFilter, ProjectFilter
from project_management.serializers import (
    MilestoneSerializer, ProjectSerializer, ECPMPCONFIGSerializer, 
    LandingScreenSerializer, ActionItemSerializer, ObjectAssessmentSerializer,
    ContactSerialier, CSVUploadSerializer, ProjectClientSerializer,
    ProjectPhaseSerializer, ProjectFundingSerializer
)
from chat.models import ChatRoom, MessageRead
from project_management.serializers import GlobalConfigSerializer
from project_management.utils import get_product_name_from_request, has_project_permission, calculate_project_progress
from ecpmpapp.custom_exceptions import ProjectAccessException
from ecpmpapp.constants import UserType, StatusType, S3_URL_TEMPLATE
from project_management.currency_converter import ECBCurrencyConverter
from project_management.utils import symbol_to_currency_code, get_currency_from_record
from django.db.models import Sum, F, Q, CharField, Subquery, OuterRef
from django.db.models.functions import Coalesce, Concat, NullIf
from django.db.models import Value
from django.contrib.postgres.aggregates import StringAgg  # If using PostgreSQL
from project_management.permissions import ProjectsPermission, ProjectPermission, PurchaseOrderPermission
from ecpmpapp.utils import require_any_permission_multiple_modules, check_for_freelancer_or_company_owner
from django.db.models import Sum, F, Q, CharField, Subquery, OuterRef, FloatField, TextField, Count
from ecpmpapp.ordering import TableOrderingFilter


def _is_truthy_param(value):
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class ProjectViewSet(viewsets.ModelViewSet):
    def destroy(self, request, *args, **kwargs):
        # Preflight: a project whose documents carry validation evidence is never deleted
        # (the cascade would erase versions, workflows and approvals). Refused before any write.
        from django.core.exceptions import ValidationError as DjangoValidationError
        from project_management.services.document_workflow import (
            PROJECT_EVIDENCE_MESSAGE,
            project_has_evidence,
        )

        project = self.get_object()
        if project_has_evidence(project):
            return Response(
                {"detail": PROJECT_EVIDENCE_MESSAGE},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            return super().destroy(request, *args, **kwargs)
        except DjangoValidationError as error:
            return Response(
                {"detail": " ".join(error.messages)},
                status=status.HTTP_400_BAD_REQUEST,
            )

    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, TableOrderingFilter]
    filterset_class = ProjectFilter
    search_fields = ['project_name', 'project_description']
    ordering_fields = [
        'id',
        'project_name',
        'type_of_service',
        'project_start_date',
        'project_end_date',
        'contract_value',
        'phases_budget',
        'created_by',
        'client__contact_user__name',
        'client__contact_company__company_name',
        'country',
        'project_status',
        'budget',
        'total_po_earning',
        'user__username',
        "contact_name",
        "project_status",
        "project_description",
        "allocated_budget",
        "project_description_clean",
        "client_name_sort",
        "owner_full_name",
        "po_num",

    ]
    ordering = ['-id']
    ordering_text_fields = {
        "project_name",
        "type_of_service",
        "project_description",
        "project_description_clean",
        "country",
        "client_name_sort",
        "owner_full_name",
        "project_status",
    }
    ordering_natural_fields = ordering_text_fields

    def get_serializer(self, *args, **kwargs):
        serializer = super(ProjectViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_permissions(self):
        print(f'[DEBUG] ProjectViewSet: self.action={self.action}')
        if self.action in ['list']:
            return [ProjectsPermission(action='view', scope='company')]
        elif self.action in ['create', 'upload_file_to_s3']:
            return [ProjectsPermission(action='create', scope='company')]
        elif self.action in ['retrieve', 'stats', 'project_stats', 'funding_screen', 'evm_data', 'landing_screen', 'team_members']:
            return [ProjectPermission(action='view', scope='project')]
        elif self.action in ['update', 'partial_update']:
            return [ProjectPermission(action='update', scope='project')]
        elif self.action == 'destroy':
            return [ProjectPermission(action='delete', scope='project')]

        return super().get_permissions()

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'ordering',
                openapi.IN_QUERY,
                description="Fields to order by, comma separated. Prefix with '-' for descending.",
                type=openapi.TYPE_STRING,
                example="start_date,-budget"
            ),
        ]
    )
    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return Project.objects.none()
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]

        has_projects_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['view', 'view_all_access', 'full_access']}, 
            'company', 
            self.request.query_params
        )

        has_projects_assigned_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'projects': ['view_assigned_created']}, 
            'company', 
            self.request.query_params
        )        
        try:
            if has_projects_company_permissions and not user.is_freelancer:
                projects = Project.objects.filter(
                    Q(user__company=user.company, solution__in=products) | Q(team_members__team_member=user, solution__in=products)
                ).distinct().order_by('-id')
            elif has_projects_assigned_created_permissions and not user.is_freelancer:
                projects = Project.objects.filter(
                    Q(team_members__team_member=user, solution__in=products)
                ).distinct()
            elif user.is_freelancer:
                projects = Project.objects.filter(
                    Q(team_members__team_member=user, solution__in=products)
                ).distinct()
            else:
                return Project.objects.none()
        except Exception:
            projects = Project.objects.none()

        if _is_truthy_param(self.request.query_params.get("company_only")):
            if user.is_freelancer or not user.company_id:
                return Project.objects.none()
            projects = projects.filter(company_profile_id=user.company_id)

        milestone_budget_subquery = (
            Milestone.objects.filter(project_id=OuterRef('pk'))
            .order_by()
            .values('project_id')
            .annotate(total=Sum('allocated_budget'))
            .values('total')[:1]
        )
        visible_purchase_orders = PurchaseOrderPermission(
            action="view",
            scope="company",
        ).filter_queryset_for_view(
            PurchaseOrder.objects.filter(project_id=OuterRef('pk')),
            user,
        )
        visible_po_total_subquery = (
            visible_purchase_orders.order_by()
            .values('project_id')
            .annotate(total=Sum('amount_excl_vat'))
            .values('total')[:1]
        )
        projects = projects.annotate(
            # Milestone represents the UI's work phases. A subquery keeps this
            # total independent of the purchase-order join below, avoiding
            # multiplied aggregates. ProjectPhase payment milestones are excluded.
            phases_budget=Subquery(
                milestone_budget_subquery,
                output_field=FloatField(),
            ),
            total_po_earning=NullIf(
                Subquery(
                    visible_po_total_subquery,
                    output_field=FloatField(),
                ),
                Value(0.0),
                output_field=FloatField(),
            ),
            project_description_clean=F('project_description'),
            po_num=Count('purchase_orders', distinct=True),
            client_name_sort=Coalesce(
                F('client__contact_company__company_name'),
                Concat(
                    F('client__contact_user__name'),
                    Value(' '),
                    F('client__contact_user__surname'),
                    output_field=TextField(),
                ),
                output_field=TextField(),
            ),
            owner_full_name=Concat(
                F('user__first_name'),
                Value(' '),
                F('user__last_name'),
                output_field=TextField(),
            ),
        ).prefetch_related("machines")
        if _is_truthy_param(self.request.query_params.get("related_to_me")):
            projects = projects.filter(
                Q(user=user) | Q(created_by=user) | Q(team_members__team_member=user)
            ).distinct()

        if self.action == 'list':
            if self.request.query_params.get("project_status") is not None:
                return projects

            return projects.exclude(project_status__iexact="Closed")
        
        return projects

    def get_object(self):
        obj = get_object_or_404(
            Project.objects.prefetch_related("machines"),
            pk=self.kwargs['pk'],
        )
        self.check_object_permissions(self.request, obj)
        has_company_projects_permissions = False
        if check_for_freelancer_or_company_owner(self.request.user, obj):
            has_company_projects_permissions = require_any_permission_multiple_modules(
                self.request.user, 
                {'projects': ['full_access', 'view_all_access', 'view']}, 
                'company', 
                self.request.query_params
            )
        has_project_level_permissions = require_any_permission_multiple_modules(
            self.request.user, 
            {'project': ['view', 'full_access']}, 
            'project', 
            {"project": self.kwargs['pk']}
        )
        if not (has_company_projects_permissions or has_project_level_permissions):
            raise ProjectAccessException()
        
        user = self.request.user
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]
        if obj.solution not in products:
            raise ProjectAccessException()

        if not user.is_freelancer:
            if obj.user.company == user.company:
                return obj
        # Check if the user is the project owner
        if obj.user == user:
            return obj

        # Check if the user is a team member of the project
        team_member = obj.team_members.filter(team_member=user).first()
        if team_member:
            return obj

        raise ProjectAccessException()

    @action(detail=False, methods=["get"])
    def stats(self, request, pk=None):
        """Returns project stats for a specific project ID (pk)."""

        projects = self.filter_queryset(self.get_queryset())

        project_stats = []
        total_projects = projects.count()
        total_booked_hour = 0
        total_booked_cost = 0
        total_allocated_hour = 0
        total_allocated_budget = 0
        total_open_action_items = 0
        
        # Get company currency for conversion
        user = request.user
        company_currency_symbol = '€'  # Default
        if not user.is_freelancer and user.company:
            company_currency_symbol = user.company.currency or '€'
        elif user.is_freelancer and hasattr(user, 'freelancer_profile'):
            company_currency_symbol = user.freelancer_profile.currency or '€'
        
        company_currency_code = symbol_to_currency_code(company_currency_symbol)
        
        # Initialize currency converter for fallback cases
        converter = ECBCurrencyConverter()
        
        for project in projects:
            milestones = Milestone.objects.filter(project=project)
            action_items = ActionItem.objects.filter(
                Q(milestone__in=milestones) | Q(parent_action_item__milestone__in=milestones)
            )
            allocated_hours = (action_items.filter(parent_action_item__isnull=True).aggregate(allocated_hours=Sum('allocated_time'))['allocated_hours'] or 0)
            total_allocated_hour += allocated_hours
            total_allocated_budget += (action_items.filter(parent_action_item__isnull=True).aggregate(allocated_budget=Sum('allocated_budget'))['allocated_budget'] or 0)

            milestone_budget_status = []
            total_planned_value = action_items.filter(
                status__iexact="closed", 
                target_date__lte=datetime.now().date(), 
                parent_action_item__isnull=True
            ).aggregate(planned_value=Sum('allocated_budget'))['planned_value'] or 0

            # Get expense cards for actual cost calculation - USE PRE-CONVERTED AMOUNTS
            expense_cards = ExpenseCard.objects.filter(Q(action_item__in=action_items))
            actual_cost = 0
            for expense_card in expense_cards:
                if expense_card.total <= 0:
                    continue
                
                # Check if converted amount is available
                if expense_card.converted_amount is not None and expense_card.converted_currency == company_currency_code:
                    # Use pre-converted amount
                    actual_cost += expense_card.converted_amount
                else:
                    # Fallback to real-time conversion (for old records) and SAVE IT
                    expense_amount = expense_card.total or 0
                    expense_currency_symbol = get_currency_from_record(expense_card)
                    expense_currency_code = symbol_to_currency_code(expense_currency_symbol)
                    
                    if expense_currency_code.upper() == company_currency_code.upper():
                        converted_amount = expense_amount
                    else:
                        converted_amount = converter.convert_currency(expense_amount, expense_currency_code, company_currency_code)
                        if converted_amount is None:
                            continue
                    
                    # Save the converted amount for future use
                    expense_card.currency = expense_currency_code
                    expense_card.converted_amount = converted_amount
                    expense_card.converted_currency = company_currency_code
                    expense_card.save(update_fields=['currency', 'converted_amount', 'converted_currency'])
                    
                    actual_cost += converted_amount

            for milestone in milestones:
                total_action_item_budget = ActionItem.objects.filter(milestone=milestone).aggregate(
                    total_budget=Sum('allocated_budget')
                )['total_budget'] or 0
                
                if total_action_item_budget < milestone.allocated_budget or 0:
                    budget_status = "yellow"
                elif total_action_item_budget > milestone.allocated_budget or 0:
                    budget_status = "red"
                else:
                    budget_status = "green"
                milestone_budget_status.append({
                    "id": milestone.id,
                    "milestone_name": milestone.name,
                    "allocated_budget": milestone.allocated_budget,
                    "total_action_item_budget": total_action_item_budget,
                    "budget_status": budget_status,
                    "allocated_hour": milestone.allocated_time or 0,
                    "spent_hour": TimeCard.objects.filter(milestone=milestone).aggregate(total_hours=Sum('hours'))['total_hours'] or 0
                })

            # Action Item Stats
            total_action_items = action_items.count()
            closed_action_items = action_items.filter(status='closed').count()
            open_action_items = total_action_items - closed_action_items

            total_open_action_items += open_action_items

            # Booked Hours from TimeCards
            booked_hours = TimeCard.objects.filter(
                Q(milestone__in=milestones) |
                Q(action_item__in=action_items),
                status=StatusType.APPROVED.value
            ).aggregate(total_hours=Sum('hours'))['total_hours'] or 0
            total_booked_hour += booked_hours

            # Booked Costs from ExpenseCards - USE PRE-CONVERTED AMOUNTS
            expense_cards_for_booked_cost = ExpenseCard.objects.filter(
                                            Q(action_item__in=action_items) |
                                            Q(milestone__in=milestones),
                                        status=StatusType.APPROVED.value
                                    ).distinct()
            
            booked_cost = 0
            for expense_card in expense_cards_for_booked_cost:
                # print (expense_card, expense_card.currency,expense_card.total, expense_card.converted_amount, expense_card.converted_currency, company_currency_code)
                
                if expense_card.total <= 0:
                    continue
                
                # Check if converted amount is available
                if expense_card.converted_amount is not None and expense_card.converted_currency == company_currency_code:
                    # Use pre-converted amount
                    
                    booked_cost += expense_card.converted_amount
                    # print(booked_cost)
                    
                else:
                    expense_amount = expense_card.total or 0
                    expense_currency_symbol = get_currency_from_record(expense_card)
                    expense_currency_code = symbol_to_currency_code(expense_currency_symbol)
                    
                    if expense_currency_code.upper() == company_currency_code.upper():
                        converted_amount = expense_amount
                    else:
                        converted_amount = converter.convert_currency(expense_amount, expense_currency_code, company_currency_code)
                        if converted_amount is None:
                            continue
                    
                    # Save the converted amount for future use
                    expense_card.currency = expense_currency_code
                    expense_card.converted_amount = converted_amount
                    expense_card.converted_currency = company_currency_code
                    expense_card.save(update_fields=['currency', 'converted_amount', 'converted_currency'])
                    
                    booked_cost += converted_amount
                  
            total_booked_cost += booked_cost

            project_stats.append(
                {
                    "project_id": project.id,
                    "project_name": project.project_name,
                    "project_currency": project.currency,
                    "booked_hours": booked_hours,
                    "allocated_hours": allocated_hours,
                    "booked_cost": round(booked_cost, 2), 
                    "open_action_items": open_action_items,
                    "closed_action_items": closed_action_items,
                    "project_progress": round(calculate_project_progress(project), 2),
                    "milestones_budget_status": milestone_budget_status,
                    "revenue": project.contract_value,
                    "bac": project.contract_value,
                    "pv": total_planned_value,
                    "ac": round(actual_cost, 2) 
                }
            )

        data = {
            "projects": project_stats,
            "total_projects": total_projects,
            "total_booked_hour": total_booked_hour,
            "total_booked_cost": round(total_booked_cost, 2),  
            "total_allocated_hour": total_allocated_hour,
            "total_allocated_budget": total_allocated_budget,
            "total_open_action_items": total_open_action_items,
            "company_currency": company_currency_code,
            "company_currency_symbol": company_currency_symbol,
        }
        return Response(data)

    @action(detail=True, methods=["get"], url_path="team-members")
    def team_members(self, request, pk=None):
        project = self.get_object()
        queryset = project.team_members.select_related("team_member").order_by(
            "team_member__first_name", "team_member__last_name", "id"
        )
        if _is_truthy_param(request.query_params.get("related_to_me")):
            queryset = queryset.filter(team_member=request.user)
        page = self.paginate_queryset(queryset)
        memberships = page if page is not None else queryset
        data = [
            {
                "id": membership.id,
                "role": membership.role,
                "team_member": {
                    "id": membership.team_member_id,
                    "first_name": membership.team_member.first_name,
                    "last_name": membership.team_member.last_name,
                    "email": membership.team_member.email,
                },
            }
            for membership in memberships
        ]
        if page is not None:
            return self.get_paginated_response(data)
        return Response(data)
    


    @action(detail=True, methods=['get'])
    def project_stats(self, request, pk=None):
        project = self.get_object()

        project_action_items = ActionItem.objects.filter(
            Q(milestone__project=project) | Q(parent_action_item__milestone__project=project)
        )
        project_milestones = Milestone.objects.filter(project=project)
        project_team_members = project.team_members.all()
        
        total_booked_hours = TimeCard.objects.filter(
            Q(action_item__in=project_action_items) | Q(milestone__in=project_milestones), status=StatusType.APPROVED.value
        ).aggregate(total_hours=Sum('hours'))['total_hours'] or 0
        total_booked_cost = ExpenseCard.objects.filter(
            Q(action_item__in=project_action_items) | Q(milestone__in=project_milestones), status=StatusType.APPROVED.value
        ).aggregate(total_cost=Sum('total'))['total_cost'] or 0

        open_action_items = project_action_items.exclude(status="closed").count()

        team_members = []

        for team_member in project_team_members:
            action_items = team_member.team_member.multi_assigned_action_items.filter(
                Q(milestone__project=project) | Q(parent_action_item__milestone__project=project)
            )
            time_allocation = action_items.filter(parent_action_item__isnull=True).aggregate(
                total_hours=Sum('allocated_time')
            )['total_hours'] or 0

            booked_hours = TimeCard.objects.filter(
                Q(action_item__in=action_items) | Q(milestone__in=project_milestones), status=StatusType.APPROVED.value
            ).aggregate(total_hours=Sum('hours'))['total_hours'] or 0
            team_members.append({
                "id": team_member.team_member.id,
                "name": team_member.team_member.full_name,
                "allocated_hours": time_allocation,
                "booked_hours": booked_hours
            })


        action_items = []

        for action_item in project_action_items:
            booked_hours = TimeCard.objects.filter(
                action_item=action_item, status=StatusType.APPROVED.value
            ).aggregate(total_hours=Sum('hours'))['total_hours'] or 0

            booked_cost = ExpenseCard.objects.filter(
                action_item=action_item, status=StatusType.APPROVED.value
            ).aggregate(total_cost=Sum('total'))['total_cost'] or 0
            action_items.append({
                "id": action_item.id,
                "name": action_item.name,
                "booked_hours": booked_hours,
                "booked_cost": booked_cost,
                "allocated_hours": action_item.allocated_time,
                "allocated_budget": action_item.allocated_budget
            })

        return Response({
            "total_booked_hours": total_booked_hours,
            "total_booked_cost": total_booked_cost,
            "open_action_items": open_action_items,
            "team_members": team_members,
            "action_items": action_items,
            "total_allocated_hours": (
                project_action_items.filter(parent_action_item__isnull=True).aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
            ),
            "total_allocated_budget": (
                project_action_items.filter(parent_action_item__isnull=True).aggregate(total_budget=Sum('allocated_budget'))['total_budget'] or 0
            ),
            "project_currency": project.currency
        })

    @action(detail=False, methods=['get'], url_path='landing-screen')
    def landing_screen(self, request):
        # Get the filtered and paginated queryset
        queryset = self.filter_queryset(self.get_queryset())
        paginator = self.paginate_queryset(queryset)
        if paginator is not None:
            projects = paginator
        else:
            projects = queryset

        # Get the aggregated totals
        totals = queryset.aggregate(
            total_project_hours=Sum('project_times__total_project_hours'),
            total_general_hours=Sum('project_times__total_general_hours'),
            total_project_expenses=Sum('project_times__total_project_expenses'),
            total_expenses=Sum('project_times__total_expenses')
        )

        # Serialize the projects
        serializer = LandingScreenSerializer(projects, many=True)

        # Include the aggregate totals in the response
        response_data = {
            'results': serializer.data,
            'totals': {
                'total_hours': (totals['total_project_hours'] or 0) + (totals['total_general_hours'] or 0),
                'total_expenses': (totals['total_expenses'] or 0) + (totals['total_project_expenses'] or 0),
                'total_active_contracts': queryset.filter(project_status__iexact="In Progress").count()
            }
        }

        # Paginate the response if needed
        if paginator is not None:
            return self.get_paginated_response(response_data)
        
        return Response(response_data)

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'project_id',
                openapi.IN_QUERY,
                description="Project id",
                type=openapi.TYPE_INTEGER,
                required=True
            )
        ]
    )
    @action(detail=False, methods=['get'], url_path='funding-screen')
    def funding_screen(self, request):
        project_id = request.query_params.get('project_id')

        if not project_id:
            return Response({'error': 'project_id is required query param'}, status=400)
        project = None
        try:
            project = get_object_or_404(Project, pk=project_id)
            if not has_project_permission("GET", self.request.user, project, get_product_name_from_request(self.request)):
                raise PermissionDenied("You do not have permission to access project funding screen data.")
        except Project.DoesNotExist:
            return Response({'error': 'Invalid Project ID'}, status=404)
        
        contact_person_details = {}
        if hasattr(project, "client"):
            contact_person_details = ProjectClientSerializer(project.client, context={"is_funding_screen": True}).data
        
        consulting_object_details = {
            "start_date": project.project_start_date.strftime("%Y-%m-%d"),
            "postal_code": project.postcode if project.postcode else None,
            "town": project.town if project.town else None,
            "address": project.address if project.address else None,
            "country": project.country if project.country else None,
        }
        private_individual_details = {
            "private_individual_type": "Owner (self user)"
        }
        energy_consultation_details = {
            "username": request.user.username,
            "first_name": request.user.first_name,
            "last_name": request.user.last_name,
            "email": request.user.email,
            "number": request.user.mobile_phone
        }
        return Response({
            "private_individual_details": private_individual_details,
            "authorizing_details": {
                "name_of_organization": project.user.company.company_name if not project.user.is_freelancer else "",
                "contact_person_details": contact_person_details,
            },
            "consulting_object_details": consulting_object_details,
            "energy_consultation_details": energy_consultation_details
        }, 200)


    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    def perform_create(self, serializer):
        serializer.save()

    @transaction.atomic
    def perform_update(self, serializer):
        project = serializer.save()
        from aas_integration.services import mark_project_out_of_date

        mark_project_out_of_date(project, getattr(self.request.user, "id", None))

    @action(detail=False, methods=['post'], serializer_class=CSVUploadSerializer,  parser_classes=[MultiPartParser])
    def upload_file_to_s3(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        file_obj = serializer.validated_data['file']

        s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
        )

        # Create a unique file name
        file_name = f"uploads/{file_obj.name}"
        content_type, _ = mimetypes.guess_type(file_obj.name)
        try:
            # Upload the file to S3
            s3_client.upload_fileobj(
                file_obj,
                settings.AWS_STORAGE_BUCKET_NAME,
                file_name,
                ExtraArgs={ "ContentType": content_type or "application/octet-stream", "ContentDisposition": "inline"},
            )
            # Construct the file URL
            s3_url = S3_URL_TEMPLATE.format(bucket=settings.AWS_STORAGE_BUCKET_NAME, region=settings.AWS_S3_REGION_NAME, file_name=file_name)

            return Response({"s3_url": s3_url}, status=status.HTTP_201_CREATED)

        except (BotoCoreError, ClientError) as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    

    @action(detail=True, methods=['get'], url_path='evm-data')
    def evm_data(self, request, pk=None):
        """
        Returns EVM related data for a specific project.
        """
        try:
            project = self.get_object()  # Already validates access
        except Project.DoesNotExist:
            return Response({"detail": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        serializer = ProjectSerializer()
        return Response({
            "evm": serializer.get_new_evm_related_data(project),
            "evm_timeseries_forecast": serializer.get_evm_timeseries_forecast(project)
        })
