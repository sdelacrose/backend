from rest_framework import viewsets
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django_filters.rest_framework import DjangoFilterBackend
from django.db.models.functions import Concat, Coalesce
from django.db.models import Value, CharField, F, Count, Prefetch, Case, When
from django.db import transaction
from companyprofileapis.models import Opportunity
from project_management.models import ActionItem
from companyprofileapis.serializers import (
    OpportunitySerializer
)
from companyprofileapis.permissions import OpportunityOffersPermission
from companyprofileapis.filters import OpportunityFilter
from companyprofileapis.services.offer_pdf import create_or_replace_offer_pdf
from companyprofileapis.services.offer_send import (
    OFFER_INCLUDE_MODE_OFFER_ONLY,
    create_offer_send_package,
)
from companyprofileapis.services.opportunity_lifecycle import (
    OpportunityLifecycleAction,
)
from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter
from rest_framework.response import Response

class OpportunityViewSet(viewsets.ModelViewSet):
    queryset = Opportunity.objects.all()
    serializer_class = OpportunitySerializer
    filterset_class = OpportunityFilter
    filter_backends = [DjangoFilterBackend, TableOrderingFilter]

    ordering_fields = [
        "id",
        "created_by",
        "created_by__email",
        "created_by__first_name",
        "created_by__last_name",
        "created_by_full_name",
        "freelancer",
        "freelancer__email",
        "company_profile",
        "company_profile__company_name",
        "reference",
        "title",
        "details",
        "status",
        "customer_user",
        "customer_user__name",
        "customer_company",
        "customer_company__company_name",
        "country",
        "type_of_service",
        "revenue",
        "probability",
        "contract_date",
        "planned_delivery_start_date",
        "planned_delivery_end_date",
        "purchase_order",
        "approver",
        "approver__email",
        "sent_date",
        "value",
        "sales_phase",
        "close_reason",
        "created_at",
        "updated_at",
        "customer_name",
        "customer_type_order",
        "linked_projects_count",
        "linked_action_items_count",
    ]
    ordering = ["-id"]
    ordering_text_fields = {
        "created_by_full_name",
        "customer_name",
        "customer_type_order",
        "title",
        "status",
        "country",
        "type_of_service",
        "sales_phase",
    }
    ordering_natural_fields = ordering_text_fields

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [OpportunityOffersPermission(action='view', scope='company')]
        elif self.action in ['create', 'direct_offer']:
            return [OpportunityOffersPermission(action='create', scope='company')]
        elif self.action in [
            'update',
            'partial_update',
            'convert_to_offer',
            'convert_to_project',
            'decline_offer',
            'close_record',
            'send_offer',
        ]:
            return [OpportunityOffersPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            # REMOVED: Delete permissions are no longer available
            # Only users with full_access can delete opportunities/offers
            return [OpportunityOffersPermission(action='full_access', scope='company')]
        return super().get_permissions()

    def get_serializer(self, *args, **kwargs):
        serializer = super(OpportunityViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        serializer.context['include_history'] = self.request.query_params.get('include_history', False)
        return serializer

    def get_queryset(self):
        request_user = self.request.user
        if isinstance(request_user, AnonymousUser):
            return Opportunity.objects.none()

        has_opportunity_offers_full_access = require_any_permission_multiple_modules(
            request_user, 
            {'opportunity_offers': ['full_access']}, 
            'company', 
            self.request.query_params
        )

        has_opportunity_offers_view = require_any_permission_multiple_modules(
            request_user, 
            {'opportunity_offers': ['view']}, 
            'company', 
            self.request.query_params
        )

        has_opportunity_offers_view_created = require_any_permission_multiple_modules(
            request_user, 
            {'opportunity_offers': ['view_created']}, 
            'company', 
            self.request.query_params
        )
        # Users with full access or view permissions see all opportunities
        if has_opportunity_offers_full_access or has_opportunity_offers_view:
            if request_user.is_freelancer:
                queryset = Opportunity.objects.filter(freelancer=request_user)
            else:
                queryset = Opportunity.objects.filter(company_profile=request_user.company)
        
        # Users with view_created permissions see only their own opportunities
        elif has_opportunity_offers_view_created:
            if request_user.is_freelancer:
                queryset = Opportunity.objects.filter(freelancer=request_user, created_by=request_user)
            else:
                queryset = Opportunity.objects.filter(company_profile=request_user.company, created_by=request_user)
        else:
            return Opportunity.objects.none()

        queryset = queryset.annotate(
            created_by_full_name=Concat(
                F('created_by__first_name'),
                Value(' '),
                F('created_by__last_name'),
                output_field=CharField()
            ),
            customer_name=Coalesce(
                F('customer_user__name'),
                F('customer_company__company_name')
            ),
            customer_type_order=Case(
                When(customer_company__isnull=False, then=Value('company')),
                When(customer_user__isnull=False, then=Value('user')),
                default=Value(''),
                output_field=CharField(),
            ),
            linked_projects_count=Count("projects", distinct=True),
            linked_action_items_count=Count("action_items", distinct=True),
        ).select_related("offer_pdf_document")

        if self.action != "list":
            queryset = queryset.prefetch_related(
                "projects",
                "offer_line_items__available_product",
                Prefetch(
                    "action_items",
                    queryset=ActionItem.objects.prefetch_related("assignees"),
                ),
            )

        return queryset

    def get_object(self):
        obj = super().get_object()
        self.check_object_permissions(self.request, obj)
        return obj

    def _lifecycle_serializer(
        self,
        *,
        lifecycle_action,
        instance=None,
        data=None,
        partial=False,
    ):
        serializer = self.get_serializer(
            instance,
            data=data,
            partial=partial,
        )
        serializer.context["lifecycle_action"] = lifecycle_action
        return serializer

    def _generate_initial_offer_pdf(self, opportunity):
        try:
            create_or_replace_offer_pdf(opportunity)
        except Exception as exc:
            raise ValidationError(
                {
                    "pdf": (
                        "The Offer PDF could not be generated. "
                        "No Offer changes were saved."
                    )
                }
            ) from exc

    @action(detail=False, methods=["post"], url_path="direct-offer")
    @transaction.atomic
    def direct_offer(self, request):
        serializer = self._lifecycle_serializer(
            lifecycle_action=OpportunityLifecycleAction.CREATE_DIRECT_OFFER,
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        self._generate_initial_offer_pdf(serializer.instance)
        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    @action(detail=True, methods=["post"], url_path="convert-to-offer")
    @transaction.atomic
    def convert_to_offer(self, request, pk=None):
        opportunity = self.get_object()
        serializer = self._lifecycle_serializer(
            lifecycle_action=OpportunityLifecycleAction.CONVERT_TO_OFFER,
            instance=opportunity,
            data=request.data,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        self._generate_initial_offer_pdf(serializer.instance)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="convert-to-project")
    @transaction.atomic
    def convert_to_project(self, request, pk=None):
        offer = self.get_object()
        serializer = self._lifecycle_serializer(
            lifecycle_action=OpportunityLifecycleAction.CONVERT_TO_PROJECT,
            instance=offer,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        serializer.instance.action_items.clear()
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="decline")
    @transaction.atomic
    def decline_offer(self, request, pk=None):
        offer = self.get_object()
        serializer = self._lifecycle_serializer(
            lifecycle_action=OpportunityLifecycleAction.DECLINE_OFFER,
            instance=offer,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="close")
    @transaction.atomic
    def close_record(self, request, pk=None):
        record = self.get_object()
        serializer = self._lifecycle_serializer(
            lifecycle_action=OpportunityLifecycleAction.CLOSE,
            instance=record,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        serializer.instance.action_items.clear()
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="send")
    @transaction.atomic
    def send_offer(self, request, pk=None):
        offer = self.get_object()
        include_mode = request.data.get(
            "include_mode",
            OFFER_INCLUDE_MODE_OFFER_ONLY,
        )
        try:
            package_data = create_offer_send_package(
                offer,
                include_mode=include_mode,
                actor=request.user,
            )
        except ValueError as exc:
            raise ValidationError(str(exc))
        except Exception as exc:
            raise ValidationError(
                {
                    "package": (
                        "The Offer package could not be generated. "
                        "No Offer changes were saved."
                    )
                }
            ) from exc
        return Response(package_data)

    def destroy(self, request, *args, **kwargs):
        """
        Override destroy method to provide better error handling for delete operations.
        Since delete permissions have been removed, only users with full_access can delete.
        """
        instance = self.get_object()
        
        # Check if user has full_access permission
        has_full_access = require_any_permission_multiple_modules(
            request.user, 
            {'opportunity_offers': ['full_access']}, 
            'company', 
            request.query_params
        )
        
        if not has_full_access:
            raise PermissionDenied(
                "You do not have permission to delete opportunities/offers. "
                "Only users with full access can perform delete operations."
            )
        
        # Proceed with deletion
        self.perform_destroy(instance)
        return Response(status=204)
