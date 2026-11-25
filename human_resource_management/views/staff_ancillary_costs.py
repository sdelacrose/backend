from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from human_resource_management.models.staff import (
    CompanyChargeRateHistory, CompanyCostItemType, StaffAncillaryCostHistory,
)
from human_resource_management.permissions import (
    _same_company, can_update_company_compensation_config,
    can_view_company_compensation_config, can_view_staff_compensation,
)
from human_resource_management.serializers.staff_ancillary_costs import (
    CompanyCostItemTypeSerializer, serialize_ancillary_cost_event,
    serialize_charge_rate_event,
)
from human_resource_management.services.staff_ancillary_costs import (
    StaffAncillaryCostError, create_ancillary_cost_event, create_charge_rate_event,
)
from models.models import Staff


class CompanyChargeRateHistoryViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """L00-B3.2: company-wide, append-only, list+create only -- operates
    implicitly on the REQUESTER'S OWN company (never accepts a company id
    in the request), so cross-company access is impossible by
    construction, not merely checked after the fact."""

    def _company(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            raise PermissionDenied("No company associated with this account.")
        return company

    def list(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_view_company_compensation_config(request.user, company):
            raise PermissionDenied("You do not have permission to view this company's charge rates.")
        entries = CompanyChargeRateHistory.objects.filter(company_profile=company).order_by("-effective_from", "-created_at")
        return Response([serialize_charge_rate_event(e) for e in entries])

    def create(self, request, *args, **kwargs):
        company = self._company(request)
        supersedes = None
        supersedes_id = request.data.get("supersedes")
        if supersedes_id:
            supersedes = get_object_or_404(CompanyChargeRateHistory, pk=supersedes_id)
            if supersedes.company_profile_id != company.id:
                raise ValidationError({"supersedes": "Must reference an entry for this same company."})

        effective_from = None
        effective_from_raw = request.data.get("effective_from")
        if effective_from_raw:
            effective_from = parse_date(str(effective_from_raw))
            if effective_from is None:
                raise ValidationError({"effective_from": "Must be a valid ISO date (YYYY-MM-DD)."})

        try:
            entry = create_charge_rate_event(
                company, actor=request.user, event_type=request.data.get("event_type"),
                effective_from=effective_from,
                employee_rate_percent=request.data.get("employee_rate_percent"),
                employer_rate_percent=request.data.get("employer_rate_percent"),
                supersedes=supersedes, note=request.data.get("note", ""),
            )
        except StaffAncillaryCostError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_charge_rate_event(entry), status=status.HTTP_201_CREATED)


class CompanyCostItemTypeViewSet(viewsets.ModelViewSet):
    """L00-B3.2: plain, mutable per-company catalog (not event-sourced --
    there is nothing historical about "the set of benefit categories this
    company has defined"). Deactivating a type never touches or deletes
    any StaffAncillaryCostHistory row referencing it (on_delete=PROTECT).
    Implicitly scoped to the requester's own company, same as the charge
    rate history above -- never accepts a company id."""
    serializer_class = CompanyCostItemTypeSerializer
    http_method_names = ["get", "post", "patch", "head", "options"]
    # L00-B3.2: a plain list, never paginated -- matches the convention of
    # every other list+create endpoint in this lot (StaffCompensationHistory/
    # CompanyChargeRateHistory/StaffAncillaryCostHistory all return a plain
    # array via a custom list()); a per-company catalog is never large
    # enough to need pagination, and the frontend treats this response as a
    # plain array.
    pagination_class = None

    def get_queryset(self):
        company = getattr(self.request.user, "company", None)
        if company is None:
            return CompanyCostItemType.objects.none()
        return CompanyCostItemType.objects.filter(company_profile=company).order_by("name")

    def list(self, request, *args, **kwargs):
        company = getattr(request.user, "company", None)
        if not company or not can_view_company_compensation_config(request.user, company):
            raise PermissionDenied("You do not have permission to view this company's cost item catalog.")
        return super().list(request, *args, **kwargs)

    def create(self, request, *args, **kwargs):
        company = getattr(request.user, "company", None)
        if not company or not can_update_company_compensation_config(request.user, company):
            raise PermissionDenied("You do not have permission to modify this company's cost item catalog.")
        return super().create(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        company = getattr(request.user, "company", None)
        if not company or not can_update_company_compensation_config(request.user, company):
            raise PermissionDenied("You do not have permission to modify this company's cost item catalog.")
        return super().partial_update(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(company_profile=self.request.user.company, created_by=self.request.user)


class StaffAncillaryCostHistoryViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """L00-B3.2: per-employee, append-only -- same list(?staff=<id>)+create
    only convention as StaffCompensationHistoryViewSet, same permission
    gate (can_view/update_staff_compensation -- at least as sensitive as
    the live compensation figure itself, never the generic staff_
    management permission)."""

    def _target_staff(self, request, staff_id):
        if not staff_id:
            raise ValidationError({"staff": "This field/query parameter is required."})
        staff = get_object_or_404(Staff, pk=staff_id)
        if request.user != staff.user and not _same_company(request.user, staff):
            raise PermissionDenied("You do not have permission to access this staff profile's ancillary costs.")
        return staff

    def list(self, request, *args, **kwargs):
        staff = self._target_staff(request, request.query_params.get("staff"))
        if not can_view_staff_compensation(request.user, staff):
            raise PermissionDenied("You do not have permission to view this employee's ancillary costs.")
        entries = (
            StaffAncillaryCostHistory.objects.filter(staff=staff)
            .select_related("cost_item_type", "created_by")
            .order_by("cost_item_type_id", "-effective_from", "-created_at")
        )
        return Response([serialize_ancillary_cost_event(e) for e in entries])

    def create(self, request, *args, **kwargs):
        staff = self._target_staff(request, request.data.get("staff"))

        cost_item_type_id = request.data.get("cost_item_type")
        cost_item_type = None
        if cost_item_type_id:
            cost_item_type = get_object_or_404(CompanyCostItemType, pk=cost_item_type_id)
            if cost_item_type.company_profile_id != getattr(staff.user, "company_id", None):
                raise ValidationError({"cost_item_type": "Must belong to this employee's own company."})

        supersedes = None
        supersedes_id = request.data.get("supersedes")
        if supersedes_id:
            supersedes = get_object_or_404(StaffAncillaryCostHistory, pk=supersedes_id)
            if supersedes.staff_id != staff.id:
                raise ValidationError({"supersedes": "Must reference an entry for this same employee."})
            if cost_item_type is None:
                cost_item_type = supersedes.cost_item_type

        if cost_item_type is None:
            raise ValidationError({"cost_item_type": "This field is required."})

        effective_from = None
        effective_from_raw = request.data.get("effective_from")
        if effective_from_raw:
            effective_from = parse_date(str(effective_from_raw))
            if effective_from is None:
                raise ValidationError({"effective_from": "Must be a valid ISO date (YYYY-MM-DD)."})

        try:
            entry = create_ancillary_cost_event(
                staff, actor=request.user, cost_item_type=cost_item_type,
                event_type=request.data.get("event_type"), effective_from=effective_from,
                amount=request.data.get("amount"), currency=request.data.get("currency"),
                frequency=request.data.get("frequency"),
                included_in_consolidated_cost=request.data.get("included_in_consolidated_cost"),
                nature=request.data.get("nature", "estimated"),
                supersedes=supersedes, note=request.data.get("note", ""),
            )
        except StaffAncillaryCostError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_ancillary_cost_event(entry), status=status.HTTP_201_CREATED)
