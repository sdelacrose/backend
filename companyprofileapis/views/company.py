import logging

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models import Q
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from companyprofileapis.filters import CompanySearchFilter
from companyprofileapis.permissions import CompanyConfigurationsPermission
from companyprofileapis.serializers import (
    CompanyConfigSerializer,
    CompanyGetProfileSerializer,
    CompanyProfileSearchSerializer,
)
from companyprofileapis.services.configuration_visibility import (
    ProjectConfigurationVisibilityError,
    validate_project_configuration_values,
    validate_project_configuration_visibility,
)
from companyprofileapis.services.offer_validity import (
    OfferValidityPeriodInUseError,
    validate_offer_validity_period_removal,
)
from ecpmpapp.constants import StatusType
from ecpmpapp.license_queries import get_active_license_user_ids_by_product_names
from ecpmpapp.ordering import TableOrderingFilter
from ecpmpapp.utils import require_any_permission_multiple_modules
from models.configuration_sync import (
    PRESERVE_LOCAL,
    ConfigurationSyncError,
    configuration_values_from_instance,
    preserve_local_configuration,
    replace_configuration,
    validate_sync_strategy,
)
from models.configuration_templates import (
    COMPANY_CONFIGURATION_FIELDS,
    get_company_configuration_defaults,
)
from models.models import CompanyConfig, CompanyProfile, GlobalConfig, User
from project_management.models import ActionItem
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.services.public_holiday_sync import (
    sync_company_public_holidays_from_global,
)
from project_management.utils import get_product_name_from_request

logger = logging.getLogger(__name__)

class CompanyProfileViewSet(viewsets.ViewSet):
    """
    A ViewSet to get the company profile and users belonging to the company.
    """
    queryset = CompanyProfile.objects.all()
    serializer_class = CompanyGetProfileSerializer

    def list(self, request, *args, **kwargs):
        request_user = self.request.user
        if request_user.is_freelancer:
            raise PermissionDenied("You do not have permission to view company profiles.")

        company = request_user.company
        if company:
            data = CompanyProfile.objects.filter(id=company.id)
            return Response(CompanyGetProfileSerializer(data, many=True).data)
        return Response([])

    @action(detail=False, methods=['get'], url_path='users')
    def get_company_users(self, request):
        request_user = self.request.user
        if request_user.is_freelancer:
            raise PermissionDenied("You do not have permission to get company users.")

        # Check query parameters - these should INCLUDE additional users, not exclude them
        include_deleted = request.query_params.get('include_deleted', '').lower() in ['true', '1']
        include_unlicensed = request.query_params.get('include_unlicensed', '').lower() in ['true', '1']
        include_inactive = request.query_params.get('include_inactive', '').lower() in ['true', '1']
        include_permanently_deleted = request.query_params.get('is_permanently_deleted', '').lower() in ['true', '1']

        company = request_user.company
        if company:
            # Start with base filter for company WITHOUT status filters initially
            users = User.objects.filter(company=company)
            
            # License filtering - INCLUSIVE approach
            if not include_unlicensed:
                # Only apply license restriction if include_unlicensed is False
                product_name = get_product_name_from_request(request)
                if product_name in COLLABORATIVE_SOLUTION:
                    products = COLLABORATIVE_SOLUTION
                else:
                    products = [product_name]
                
                active_license_user_ids = get_active_license_user_ids_by_product_names(products)
                
                # Handle empty license list properly
                if active_license_user_ids:
                    users = users.filter(id__in=active_license_user_ids)
                else:
                    # If no one has licenses and we're not including unlicensed users,
                    # return empty queryset
                    users = users.none()
            
            # Apply status filters at the end, similar to get_associated_users
            status_filter_conditions = Q()
            
            if not include_inactive:
                status_filter_conditions &= Q(is_active=True)
            if not include_deleted:
                status_filter_conditions &= Q(is_deleted=False)
            if not include_permanently_deleted:
                status_filter_conditions &= Q(is_permanently_deleted=False)
            
            if status_filter_conditions:
                users = users.filter(status_filter_conditions)
            
            user_data = [{"id": user.id, "name": user.full_name, "email": user.email} for user in users]
            return Response(user_data, status=200)
        return Response({"detail": "No company found."}, status=404)

from rest_framework import generics

class CompanySearchView(generics.ListAPIView):
    queryset = CompanyProfile.objects.filter(company_searchable=True)
    filterset_class = CompanySearchFilter
    serializer_class = CompanyProfileSearchSerializer
    filter_backends = [
        filters.SearchFilter,
        DjangoFilterBackend,
        TableOrderingFilter,
    ]
    search_fields = ['company_name', 'sharemundo_links_id', "company_web_address", "company_country", "products",
                     "services", "expertise"]
    ordering_fields = [
        "company_name",
        "sharemundo_links_id",
        "company_tell_no",
        "company_address",
        "company_town",
        "post_code",
        "company_country",
        "products",
        "services",
        "expertise",
        "main_area_of_expertise",
        "sub_area_of_expertise",
    ]
    ordering_natural_fields = {
        "sharemundo_links_id",
    }
    ordering_localized_fields = {
        "main_area_of_expertise",
        "sub_area_of_expertise",
    }
    ordering_localized_array_fields = {"sub_area_of_expertise"}

    def get_queryset(self):
        queryset = CompanyProfile.objects.filter(company_searchable=True)

        user = self.request.user
        if user.company:
            queryset = queryset.exclude(id=user.company.id)

        filter_data_present = any(
            self.request.query_params.get(field)
            for field in self.filterset_class.get_filters().keys()
        )
        search_data_present = self.request.query_params.get('search')

        if filter_data_present or search_data_present:
            return queryset.order_by('-id')

        return queryset.none()


class CompanyConfigViewSet(viewsets.ModelViewSet):
    queryset = CompanyConfig.objects.all()
    serializer_class = CompanyConfigSerializer

    def get_permissions(self):
        if self.action == 'list':
            return [CompanyConfigurationsPermission('view', 'company')]
        elif self.action == 'retrieve':
            return [CompanyConfigurationsPermission('view', 'company')]
        elif self.action == 'create':
            return [CompanyConfigurationsPermission('create', 'company')]
        elif self.action in ['update', 'partial_update']:
            return [CompanyConfigurationsPermission('update', 'company')]
        elif self.action == 'destroy':
            return [CompanyConfigurationsPermission('delete', 'company')]
        elif self.action == 'reset_config':
            return [CompanyConfigurationsPermission('reset', 'company')]
        elif self.action == 'sync_with_global':
            return [CompanyConfigurationsPermission('sync_admin', 'company')]
        return super().get_permissions()

    def get_queryset(self):
        """
        Filter company configurations based on user permissions:
        - Users with view or full_access permissions see company configurations
        - Freelancers see their own configurations
        - Company users see their company's configurations
        """
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return CompanyConfig.objects.none()

        # Check permissions
        has_company_config_full_access = require_any_permission_multiple_modules(
            user, 
            {'company_configurations': ['full_access']}, 
            'company', 
            self.request.query_params
        )
        has_company_config_view = require_any_permission_multiple_modules(
            user, 
            {'company_configurations': ['view']}, 
            'company', 
            self.request.query_params
        )

        # If no permissions, return empty queryset
        if not (has_company_config_full_access or has_company_config_view):
            return CompanyConfig.objects.none()

        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
            
        if user.is_freelancer:
            # Freelancers see their own configurations
            return CompanyConfig.objects.filter(user=user, solution__in=products).order_by('-id')
        else:
            # Company users see their company's configurations
            return CompanyConfig.objects.filter(company_profile=user.company, solution__in=products).order_by('-id')

    def get_object(self):
        obj = super().get_object()
        self.check_object_permissions(self.request, obj)
        return obj

    def _validate_company_roles_update(self, company_config, company_roles):
        serializer = self.get_serializer(
            instance=company_config,
            data={"company_roles": company_roles},
            partial=True,
        )
        serializer.is_valid(raise_exception=True)

    @transaction.atomic
    def perform_update(self, serializer):
        """
        Update company configuration with permission checking.
        The permission class already handles the permission check, so we just need to
        ensure the user is updating their own configuration.
        """
        request_user = self.request.user
        instance = serializer.instance
        previous_status_options = list(instance.status_options or [])
        previous_labels = list(instance.labels or [])
        
        # Get the fields that were updated in this request
        updated_fields = set(serializer.validated_data.keys())
        
        # Check if user is updating their own configuration
        if request_user.is_freelancer:
            if instance.user == request_user:
                # Save the current instance
                updated_instance = serializer.save()
                if 'status_options' in updated_fields:
                    self._normalize_company_action_items(
                        updated_instance,
                        previous_status_options,
                    )
                if 'labels' in updated_fields:
                    self._normalize_company_action_item_labels(
                        updated_instance,
                        previous_labels,
                    )
                # Propagate only changed fields to other solutions
                self._propagate_changes_to_other_solutions(updated_instance, updated_fields)
                return
        else:
            if instance.company_profile == request_user.company:
                # Save the current instance
                updated_instance = serializer.save()
                if 'status_options' in updated_fields:
                    self._normalize_company_action_items(
                        updated_instance,
                        previous_status_options,
                    )
                if 'labels' in updated_fields:
                    self._normalize_company_action_item_labels(
                        updated_instance,
                        previous_labels,
                    )
                # Propagate only changed fields to other solutions
                self._propagate_changes_to_other_solutions(updated_instance, updated_fields)
                return
        
        raise PermissionDenied("You do not have permission to update this company's configuration.")
    
    def _propagate_changes_to_other_solutions(self, updated_instance, updated_fields):
        """
        Propagate only the changed fields from one solution config to all other solutions 
        for the same company/freelancer.
        
        Args:
            updated_instance: The instance that was just updated
            updated_fields: Set of field names that were updated in the request
        """
        if not updated_fields:
            return  # No fields to update
            
        if updated_instance.user:
            # For freelancer
            other_configs = CompanyConfig.objects.filter(
                user=updated_instance.user
            ).exclude(id=updated_instance.id)
        else:
            # For company
            other_configs = CompanyConfig.objects.filter(
                company_profile=updated_instance.company_profile
            ).exclude(id=updated_instance.id)

        other_configs = list(other_configs.select_for_update())
        
        # Define all possible config fields that can be propagated
        config_fields = [
            'contract_type', 'international_standard', 'major_project_model', 'commercial_model', 'supporting_agreement',
            'team_members_roles',
            'company_roles', 'funding_strategy', 'status_options', 'reminder_settings',
            'invoice_status', 'expense_types',
            'project_types', 'document_types', 'document_status',
            'document_internal_review_status', 'document_external_review_status',
            'transmittal_category',
            'priority_levels', 'project_priority_levels',
            'opportunity_sales_phase', 'offer_validity_periods', 'labels',
            'leave_types', 'document_disciplines', 'nested_configurations',
            'configuration_visibility',
            'skills'
        ]
        
        # Only copy fields that were updated in the request
        fields_to_update = {field: getattr(updated_instance, field) 
                           for field in config_fields 
                           if field in updated_fields}
        
        if not fields_to_update:
            return  # No relevant fields to update

        if 'offer_validity_periods' in fields_to_update:
            for config in other_configs:
                try:
                    validate_offer_validity_period_removal(
                        config,
                        fields_to_update['offer_validity_periods'],
                    )
                except OfferValidityPeriodInUseError as exc:
                    raise serializers.ValidationError(
                        {
                            "offer_validity_periods": {
                                "code": "configuration_value_in_use",
                                "values": list(exc.values),
                            }
                        }
                    ) from exc
            
        # Update only the changed fields in other configs
        for config in other_configs:
            if 'configuration_visibility' in fields_to_update:
                try:
                    validate_project_configuration_visibility(
                        config,
                        config.configuration_visibility,
                        fields_to_update['configuration_visibility'],
                    )
                except ProjectConfigurationVisibilityError as exc:
                    raise serializers.ValidationError(
                        {
                            "configuration_visibility": {
                                config_key: {
                                    "code": "configuration_value_in_use",
                                    "values": values,
                                }
                                for config_key, values in exc.blocked_values.items()
                            }
                        }
                    ) from exc
            previous_status_options = list(config.status_options or [])
            previous_labels = list(config.labels or [])
            for field, value in fields_to_update.items():
                setattr(config, field, value)
            config.save()
            if 'status_options' in fields_to_update:
                self._normalize_company_action_items(config, previous_status_options)
            if 'labels' in fields_to_update:
                self._normalize_company_action_item_labels(config, previous_labels)

    def _normalize_company_action_items(self, config, previous_status_options):
        removed_statuses = self._get_removed_statuses(previous_status_options, config.status_options)
        if not removed_statuses:
            return

        status_filters = Q()
        for status in removed_statuses:
            status_filters |= Q(status__iexact=status)

        if config.user:
            updated_count = ActionItem.objects.filter(
                Q(freelancer=config.user)
                | Q(parent_action_item__freelancer=config.user),
            ).filter(status_filters).update(status=StatusType.OPEN.value)
            logger.info(
                "CompanyConfig status normalization: freelancer_id=%s removed=%s updated=%s",
                getattr(config.user, "id", None),
                removed_statuses,
                updated_count,
            )
        else:
            updated_count = ActionItem.objects.filter(
                Q(company_profile=config.company_profile)
                | Q(parent_action_item__company_profile=config.company_profile),
            ).filter(status_filters).update(status=StatusType.OPEN.value)
            logger.info(
                "CompanyConfig status normalization: company_id=%s removed=%s updated=%s",
                getattr(config.company_profile, "id", None),
                removed_statuses,
                updated_count,
            )

    def _normalize_company_action_item_labels(self, config, previous_labels):
        removed_labels = self._get_removed_plain_values(previous_labels, config.labels)
        if not removed_labels:
            return

        removed_normalized = {label.strip().lower() for label in removed_labels}

        if config.user:
            queryset = ActionItem.objects.filter(
                Q(freelancer=config.user)
                | Q(parent_action_item__freelancer=config.user),
            ).distinct()
            scope_label = "freelancer_id"
            scope_id = getattr(config.user, "id", None)
        else:
            queryset = ActionItem.objects.filter(
                Q(company_profile=config.company_profile)
                | Q(parent_action_item__company_profile=config.company_profile),
            ).distinct()
            scope_label = "company_id"
            scope_id = getattr(config.company_profile, "id", None)

        items_to_update = []
        for action_item in queryset.only("id", "labels"):
            current_labels = [
                str(label).strip()
                for label in (action_item.labels or [])
                if str(label).strip()
            ]
            cleaned_labels = [
                label
                for label in current_labels
                if label.lower() not in removed_normalized
            ]
            if cleaned_labels != current_labels:
                action_item.labels = cleaned_labels
                items_to_update.append(action_item)

        if items_to_update:
            ActionItem.objects.bulk_update(items_to_update, ["labels"])

        logger.info(
            "CompanyConfig label normalization: %s=%s removed=%s updated=%s",
            scope_label,
            scope_id,
            removed_labels,
            len(items_to_update),
        )

    def _get_removed_statuses(self, previous_status_options, current_status_options):
        previous = {
            opt.split(":", 1)[0].strip().lower()
            for opt in (previous_status_options or [])
            if isinstance(opt, str) and opt.strip()
        }
        current = {
            opt.split(":", 1)[0].strip().lower()
            for opt in (current_status_options or [])
            if isinstance(opt, str) and opt.strip()
        }
        return sorted(previous - current)

    def _get_removed_plain_values(self, previous_values, current_values):
        previous = {
            str(item).strip().lower()
            for item in (previous_values or [])
            if str(item).strip()
        }
        current = {
            str(item).strip().lower()
            for item in (current_values or [])
            if str(item).strip()
        }
        return sorted(previous - current)

    def create(self, request, *args, **kwargs):
        """
        Disable the ability to create company config.
        The permission class will handle the permission check.
        """
        return Response({"error": "Company config creation is not allowed."}, status=405)

    def destroy(self, request, *args, **kwargs):
        """
        Disable the ability to delete company config.
        The permission class will handle the permission check.
        """
        return Response({"error": "Company config deletion is not allowed."}, status=405)

    @action(detail=False, methods=['post'], url_path='reset')
    @transaction.atomic
    def reset_config(self, request):
        """Reset company configurations to canonical system defaults."""
        user = request.user

        try:
            if user.is_freelancer:
                company_configs = CompanyConfig.objects.filter(user=user)
            else:
                company_configs = CompanyConfig.objects.filter(company_profile=user.company)

            reset_values = get_company_configuration_defaults()
            for company_config in company_configs.select_for_update():
                proposed_visibility = reset_values["configuration_visibility"]
                try:
                    validate_project_configuration_values(
                        company_config,
                        reset_values,
                        proposed_visibility,
                    )
                except ProjectConfigurationVisibilityError as exc:
                    raise serializers.ValidationError(
                        {
                            "configuration_visibility": {
                                config_key: {
                                    "code": "configuration_value_in_use",
                                    "values": values,
                                }
                                for config_key, values in exc.blocked_values.items()
                            }
                        }
                    ) from exc

                previous_status_options = list(company_config.status_options or [])
                previous_labels = list(company_config.labels or [])
                serializer = self.get_serializer(
                    instance=company_config,
                    data=reset_values,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                reset_config = serializer.save()
                self._normalize_company_action_items(
                    reset_config,
                    previous_status_options,
                )
                self._normalize_company_action_item_labels(
                    reset_config,
                    previous_labels,
                )

            return Response({"message": "All company configurations have been reset."}, status=200)

        except CompanyConfig.DoesNotExist:
            return Response({"error": "No company configuration found for this user's company."}, status=404)

    @action(detail=False, methods=['post'], url_path='sync')
    @transaction.atomic
    def sync_with_global(self, request):
        """Synchronize company configurations using the selected merge strategy."""
        user = request.user

        try:
            strategy = validate_sync_strategy(
                request.data.get("strategy", PRESERVE_LOCAL)
            )
            if user.is_freelancer:
                global_config = GlobalConfig.objects.get(user=user)
                company_configs = CompanyConfig.objects.filter(user=user)
            else:
                global_config = GlobalConfig.objects.get(company_profile=user.company)
                company_configs = CompanyConfig.objects.filter(company_profile=user.company)

            template_values = configuration_values_from_instance(
                global_config,
                COMPANY_CONFIGURATION_FIELDS,
            )
            for company_config in company_configs.select_for_update():
                local_values = configuration_values_from_instance(
                    company_config,
                    COMPANY_CONFIGURATION_FIELDS,
                )
                if strategy == PRESERVE_LOCAL:
                    synced_values = preserve_local_configuration(
                        local_values,
                        template_values,
                        field_names=COMPANY_CONFIGURATION_FIELDS,
                    )
                else:
                    synced_values = replace_configuration(
                        template_values,
                        field_names=COMPANY_CONFIGURATION_FIELDS,
                    )

                proposed_visibility = synced_values.get(
                    "configuration_visibility",
                    {},
                )
                try:
                    validate_project_configuration_values(
                        company_config,
                        synced_values,
                        proposed_visibility,
                    )
                except ProjectConfigurationVisibilityError as exc:
                    raise serializers.ValidationError(
                        {
                            "configuration_visibility": {
                                config_key: {
                                    "code": "configuration_value_in_use",
                                    "values": values,
                                }
                                for config_key, values in exc.blocked_values.items()
                            }
                        }
                    ) from exc

                previous_status_options = list(company_config.status_options or [])
                previous_labels = list(company_config.labels or [])
                serializer = self.get_serializer(
                    instance=company_config,
                    data=synced_values,
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                synced_config = serializer.save()
                self._normalize_company_action_items(
                    synced_config,
                    previous_status_options,
                )
                self._normalize_company_action_item_labels(
                    synced_config,
                    previous_labels,
                )

            public_holiday_summary = sync_company_public_holidays_from_global(
                company_profile=getattr(user, "company", None),
                acting_user=user,
                preserve_local=strategy == PRESERVE_LOCAL,
            )

            return Response(
                {
                    "message": "All company configurations have been synced with global configuration.",
                    "strategy": strategy,
                    "public_holidays": public_holiday_summary,
                },
                status=200,
            )

        except ConfigurationSyncError as exc:
            return Response(
                {"strategy": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except GlobalConfig.DoesNotExist:
            return Response({"error": "No global configuration found for this user's company."}, status=404)
        except CompanyConfig.DoesNotExist:
            return Response({"error": "No company configuration found for this user's company."}, status=404)
