from django.contrib.auth.models import AnonymousUser
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from human_resource_management.permissions import StaffManagementPermission
from human_resource_management.serializers.staff_quotite_history import (
    serialize_quotite_history_entry,
)
from human_resource_management.services.leave_accrual import (
    LeaveAccrualQuotiteError,
    create_quotite_history_entry,
)
from models.models import Staff
from human_resource_management.models.leave_entitlement import StaffQuotiteHistory


class StaffQuotiteHistoryViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """LOT RH12: the dedicated contractual source of truth for an employee's
    quotite -- list (?staff=<id>) and create only, never update/delete (same
    "never rewrite history" precedent as LeaveAccrualCredit/
    LeaveTypeAccrualRateHistory -- a correction is a new row). Reuses
    `staff_management`'s existing cumulative view/update permissions and
    company isolation exactly like editing `weekly_hours` directly already
    requires -- never a wider grant, never touches salary visibility (see
    StaffManagementPermission itself, unchanged)."""

    def get_permissions(self):
        if self.action == "list":
            return [StaffManagementPermission(action="view", scope="company")]
        return [StaffManagementPermission(action="update", scope="company")]

    def _staff_queryset(self, request):
        if isinstance(request.user, AnonymousUser):
            return Staff.objects.none()
        if request.user.is_freelancer:
            return Staff.objects.filter(user=request.user)
        return Staff.objects.filter(user__company=request.user.company)

    def get_queryset(self):
        request = self.request
        staff_id = request.query_params.get("staff")
        allowed_staff = self._staff_queryset(request)
        queryset = StaffQuotiteHistory.objects.filter(
            staff__in=allowed_staff
        ).select_related("created_by").order_by("-effective_from", "-created_at")
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)
        return queryset

    def list(self, request, *args, **kwargs):
        return Response([serialize_quotite_history_entry(e) for e in self.get_queryset()])

    def create(self, request, *args, **kwargs):
        # LOT RH12: StaffManagementPermission.has_permission does not gate
        # POST at all (a pre-existing gap in that shared class, unrelated to
        # this lot -- see its own has_permission branching) -- the SAME
        # cumulative update check it already applies to PUT/PATCH is invoked
        # explicitly here instead, so creating a quotite-history entry is
        # never actually left open to any authenticated user.
        permission = StaffManagementPermission(action="update", scope="company")
        if not permission._check_update_permission(request.user):
            raise PermissionDenied(
                "You need staff-management update permission to record a contractual situation."
            )
        staff_id = request.data.get("staff")
        staff = get_object_or_404(self._staff_queryset(request), pk=staff_id)
        contractual_hours = request.data.get("contractual_hours")
        full_time_reference_hours = request.data.get("full_time_reference_hours")
        effective_from_raw = request.data.get("effective_from")
        if not effective_from_raw:
            raise ValidationError({"effective_from": "This field is required."})
        effective_from = parse_date(str(effective_from_raw))
        if effective_from is None:
            raise ValidationError({"effective_from": "Must be a valid ISO date (YYYY-MM-DD)."})
        try:
            entry = create_quotite_history_entry(
                staff,
                contractual_hours=contractual_hours,
                full_time_reference_hours=full_time_reference_hours,
                effective_from=effective_from,
                created_by=request.user,
            )
        except LeaveAccrualQuotiteError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_quotite_history_entry(entry), status=status.HTTP_201_CREATED)
