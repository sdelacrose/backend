from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from companyprofileapis.permissions import CompanyConfigurationsPermission
from ecpmpapp.utils import require_any_permission_multiple_modules
from human_resource_management.models import LeaveRequest
from human_resource_management.permissions import (
    resolve_authorized_solution_for_request,
    resolve_authorized_solution_for_write,
)
from human_resource_management.serializers.leave_type import (
    LeaveSubTypeSerializer,
    LeaveTypeSerializer,
)
from models.leave_type_sync import sync_leave_types_array
from models.models import CompanyConfig, LeaveSubType, LeaveType
from project_management.models.constants import COLLABORATIVE_SOLUTION
from project_management.utils import get_product_name_from_request


def _company_configs_for_request(request, *, product_name=None):
    """Mirrors CompanyConfigViewSet.get_queryset's own solution/company scoping, so
    leave-type management stays scoped exactly like every other company configuration.

    `product_name`: LOT RH13 follow-up (solution authorization) -- an optional
    override, used ONLY by callers that have already validated the client-
    supplied solution against this user's actual active license (see
    `human_resource_management.permissions.resolve_authorized_solution_for_
    request`). `None` (the default) preserves the EXACT prior behavior for
    every other existing caller across the app -- this function itself still
    trusts the raw query param when no override is given, unchanged."""
    if product_name is None:
        product_name = get_product_name_from_request(request)
    products = COLLABORATIVE_SOLUTION if product_name in COLLABORATIVE_SOLUTION else [product_name]
    if request.user.is_freelancer:
        return CompanyConfig.objects.filter(user=request.user, solution__in=products)
    return CompanyConfig.objects.filter(company_profile=request.user.company, solution__in=products)


class LeaveTypeViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveTypeSerializer

    def get_permissions(self):
        action_by_method = {
            "list": CompanyConfigurationsPermission("view", "company"),
            "retrieve": CompanyConfigurationsPermission("view", "company"),
            "create": CompanyConfigurationsPermission("create", "company"),
            "update": CompanyConfigurationsPermission("update", "company"),
            "partial_update": CompanyConfigurationsPermission("update", "company"),
            "destroy": CompanyConfigurationsPermission("delete", "company"),
            "admin_applicable": CompanyConfigurationsPermission("view", "company"),
        }
        permission = action_by_method.get(self.action)
        return [permission] if permission else super().get_permissions()

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return LeaveType.objects.none()

        has_access = require_any_permission_multiple_modules(
            self.request.user,
            {"company_configurations": ["view", "full_access"]},
            "company",
            self.request.query_params,
        )
        if not has_access:
            return LeaveType.objects.none()

        return LeaveType.objects.filter(
            company_config__in=_company_configs_for_request(
                self.request, product_name=resolve_authorized_solution_for_request(self.request)
            )
        ).order_by("id")

    @action(detail=False, methods=["get"], url_path="admin-applicable")
    def admin_applicable(self, request):
        # Read-only view of this company's OWN Admin-scope templates (never another
        # company's), so the Solution screen can show "what the Admin defines" without
        # exposing the Admin-side write API to company users at all.
        has_access = require_any_permission_multiple_modules(
            request.user,
            {"company_configurations": ["view", "full_access"]},
            "company",
            request.query_params,
        )
        if not has_access:
            return Response([])
        if request.user.is_freelancer:
            queryset = LeaveType.objects.filter(global_config__user=request.user)
        else:
            queryset = LeaveType.objects.filter(
                global_config__company_profile=request.user.company
            )
        serializer = self.get_serializer(queryset.order_by("id"), many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="applicable")
    def applicable(self, request):
        # Read-only, ACTIVE-only list for employees submitting a leave request
        # (LOT RH 3's type/sub-type picker) -- gated by leave_request create
        # permissions, not company_configurations (which only configuration
        # managers hold and which `list`/`retrieve` above require).
        has_access = require_any_permission_multiple_modules(
            request.user,
            {"leave_request": ["create", "create_without_approval", "full_access"]},
            "company",
            request.query_params,
        )
        if not has_access:
            return Response([])
        queryset = LeaveType.objects.filter(
            company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            )
        ).order_by("id")
        # LOT L04 (SST): an SST-managed type is never selectable from the
        # classic form -- the SST module creates its own LeaveRequest rows
        # directly, never through this picker.
        active_only = [
            lt for lt in queryset
            if lt.effective_value("is_active") is not False and not lt.is_sst_managed
        ]
        return Response(LeaveTypeSerializer(active_only, many=True).data)

    def perform_create(self, serializer):
        company_configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_write(self.request)
        )
        company_config = company_configs.first()
        if company_config is None:
            raise ValidationError(
                "No company configuration found for this company/solution; "
                "cannot create a leave type."
            )
        serializer.save(company_config=company_config)

    @staticmethod
    def _referenced_by_leave_requests(instance, name):
        # Two independent ways a request can point at this type, both checked:
        # the frozen `type` name string (matches historical requests created
        # before LOT RH 3's identity link, by the NAME in effect at the time --
        # never by this row's id) OR the stable `leave_type` FK (LOT RH 3,
        # requests created with the identity link, which survive a later
        # rename since they no longer depend on the name matching at all).
        # SET_NULL on that FK stops a delete from cascading, but silently
        # nulling it on an in-use type would still strand those requests
        # without the type/config their own display and recalculation rely on
        # -- deactivation is required for either, not just the legacy path.
        return LeaveRequest.objects.filter(
            Q(type=name) | Q(leave_type_id=instance.id),
            employee__user__company_id=instance.company_config.company_profile_id,
        ).exists()

    def perform_update(self, serializer):
        instance = serializer.instance
        new_name = serializer.validated_data.get("name", instance.name)
        if new_name != instance.name and self._referenced_by_leave_requests(
            instance, instance.name
        ):
            # Renaming would silently break perform_destroy's own name-based lookup for
            # every existing request still carrying the old name (it would stop matching
            # it, and the deletion-protection below would no longer see them) -- and the
            # requests themselves would display an orphaned label never seen in any
            # current config. Deactivating instead keeps the historical name intact.
            raise ValidationError(
                "This leave type cannot be renamed: it is referenced by existing leave "
                "requests under its current name. Deactivate it and create a new type "
                "instead."
            )
        serializer.save()

    def perform_destroy(self, instance):
        if self._referenced_by_leave_requests(instance, instance.name):
            raise ValidationError(
                "This leave type cannot be deleted: it is referenced by existing "
                "leave requests. Deactivate it instead."
            )
        company_config = instance.company_config
        instance.delete()
        sync_leave_types_array(company_config)


class LeaveSubTypeViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveSubTypeSerializer

    def get_permissions(self):
        action_by_method = {
            "list": CompanyConfigurationsPermission("view", "company"),
            "retrieve": CompanyConfigurationsPermission("view", "company"),
            "create": CompanyConfigurationsPermission("create", "company"),
            "update": CompanyConfigurationsPermission("update", "company"),
            "partial_update": CompanyConfigurationsPermission("update", "company"),
            "destroy": CompanyConfigurationsPermission("delete", "company"),
        }
        permission = action_by_method.get(self.action)
        return [permission] if permission else super().get_permissions()

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return LeaveSubType.objects.none()

        has_access = require_any_permission_multiple_modules(
            self.request.user,
            {"company_configurations": ["view", "full_access"]},
            "company",
            self.request.query_params,
        )
        if not has_access:
            return LeaveSubType.objects.none()

        queryset = LeaveSubType.objects.filter(
            leave_type__company_config__in=_company_configs_for_request(
                self.request, product_name=resolve_authorized_solution_for_request(self.request)
            )
        ).order_by("id")
        leave_type_id = self.request.query_params.get("leave_type")
        if leave_type_id:
            queryset = queryset.filter(leave_type_id=leave_type_id)
        return queryset

    def perform_create(self, serializer):
        leave_type = serializer.validated_data.get("leave_type")
        allowed_leave_types = LeaveType.objects.filter(
            company_config__in=_company_configs_for_request(
                self.request, product_name=resolve_authorized_solution_for_write(self.request)
            )
        )
        if leave_type is None or leave_type not in allowed_leave_types:
            raise PermissionDenied("This leave type does not belong to your company.")
        serializer.save()

    def perform_destroy(self, instance):
        # Unlike LeaveType, a sub-type has no frozen name snapshot on
        # LeaveRequest to fall back on -- the stable `leave_sub_type` FK (LOT
        # RH 3) is the only way a request ever references one, so this is the
        # sole check needed. SET_NULL stops a delete from cascading, but a
        # sub-type still in use must be deactivated instead, same as a type.
        if LeaveRequest.objects.filter(leave_sub_type_id=instance.id).exists():
            raise ValidationError(
                "This sub-type cannot be deleted: it is referenced by existing "
                "leave requests. Deactivate it instead."
            )
        instance.delete()
