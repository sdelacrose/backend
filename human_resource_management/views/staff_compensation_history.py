from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from human_resource_management.models.staff import StaffCompensationHistory
from human_resource_management.permissions import _same_company, can_view_staff_compensation
from human_resource_management.serializers.staff_compensation_history import (
    serialize_compensation_event,
)
from human_resource_management.services.staff_compensation import (
    StaffCompensationError, create_compensation_event,
)
from models.models import Staff


class StaffCompensationHistoryViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """L00-B3.1: the dedicated, append-only source of truth for an
    employee's compensation -- list (?staff=<id>) and create only, never
    update/delete (same "a correction is a new row" precedent as
    StaffQuotiteHistoryViewSet, RH12). Gated on the SAME per-employee
    compensation rights as the rest of L00-A (can_view_staff_compensation/
    can_update_staff_compensation via services.staff_compensation) --
    deliberately NOT the generic StaffManagementPermission/staff_management.
    view, which was exactly the gap L00-A closed for the live salary/
    hourly_rate fields themselves; this history is at least as sensitive."""

    def _target_staff(self, request, staff_id):
        if not staff_id:
            raise ValidationError({"staff": "This field/query parameter is required."})
        staff = get_object_or_404(Staff, pk=staff_id)
        if request.user != staff.user and not _same_company(request.user, staff):
            raise PermissionDenied("You do not have permission to access this staff profile's compensation history.")
        return staff

    def list(self, request, *args, **kwargs):
        staff = self._target_staff(request, request.query_params.get("staff"))
        if not can_view_staff_compensation(request.user, staff):
            raise PermissionDenied("You do not have permission to view this employee's compensation history.")
        entries = (
            StaffCompensationHistory.objects.filter(staff=staff)
            .select_related("created_by")
            .order_by("-effective_from", "-created_at")
        )
        return Response([serialize_compensation_event(e) for e in entries])

    def create(self, request, *args, **kwargs):
        staff = self._target_staff(request, request.data.get("staff"))

        event_type = request.data.get("event_type")
        supersedes = None
        supersedes_id = request.data.get("supersedes")
        if supersedes_id:
            supersedes = get_object_or_404(StaffCompensationHistory, pk=supersedes_id)
            if supersedes.staff_id != staff.id:
                raise ValidationError({"supersedes": "Must reference an entry for this same employee."})

        effective_from_raw = request.data.get("effective_from")
        effective_from = None
        if effective_from_raw:
            effective_from = parse_date(str(effective_from_raw))
            if effective_from is None:
                raise ValidationError({"effective_from": "Must be a valid ISO date (YYYY-MM-DD)."})

        try:
            entry = create_compensation_event(
                staff, actor=request.user, event_type=event_type, effective_from=effective_from,
                gross_monthly_salary=request.data.get("gross_monthly_salary"),
                hourly_rate=request.data.get("hourly_rate"),
                currency=request.data.get("currency"),
                basis=request.data.get("basis"),
                supersedes=supersedes,
                note=request.data.get("note", ""),
            )
        except StaffCompensationError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_compensation_event(entry), status=status.HTTP_201_CREATED)
