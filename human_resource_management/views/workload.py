from datetime import date

from django.contrib.auth.models import AnonymousUser
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from human_resource_management.permissions import (
    WorkloadAlertManagementPermission,
    WorkloadViewPermission,
    resolve_authorized_solution_for_request,
    resolve_authorized_solution_for_write,
)
from human_resource_management.serializers.workload import (
    StaffWorkloadAlertConfigSerializer,
    serialize_episode,
    serialize_workload_result,
)
from human_resource_management.services.workload import compute_staff_workload, month_bounds
from human_resource_management.services.workload_alerts import (
    WorkloadAlertBlockedError,
    evaluate_config,
)
from human_resource_management.models.workload import StaffWorkloadAlertConfig, StaffWorkloadAlertEpisode
from human_resource_management.views.leave_type import _company_configs_for_request
from models.models import Staff


def _parse_period(request):
    """`?period=YYYY-MM`, defaulting to the current calendar month -- the
    HRM screen's own consultation filter, with NO effect on alert
    evaluation (services.workload_alerts resolves its own period
    independently, see AI-2054 section 6)."""
    raw = request.query_params.get("period")
    today = date.today()
    if not raw:
        return month_bounds(today.year, today.month)
    try:
        year_str, month_str = raw.split("-")
        return month_bounds(int(year_str), int(month_str))
    except (ValueError, AttributeError):
        raise ValidationError({"period": "Expected format YYYY-MM."})


def _staff_queryset_for_request(request):
    configs = _company_configs_for_request(
        request, product_name=resolve_authorized_solution_for_request(request)
    )
    config = configs.first()
    if config is None:
        return Staff.objects.none(), None
    if config.user_id:
        return Staff.objects.filter(user_id=config.user_id), config
    return Staff.objects.filter(
        user__company_id=config.company_profile_id, user__is_deleted=False,
    ), config


class StaffWorkloadViewSet(viewsets.ViewSet):
    """AI-2054: read-only cross-project occupancy -- NEVER creates/modifies
    any Project Management allocation, never triggers an alert evaluation
    (that only ever happens via the scheduled task / signals / an explicit
    manual recompute on StaffWorkloadAlertConfigViewSet, see AI-2054 section
    6 -- "aucune notification déclenchée par une simple consultation")."""
    permission_classes = [WorkloadViewPermission]

    def list(self, request):
        """The Staff Management grid's new 'Taux d'occupation' column --
        light entries only (no per-action detail), one per staff in scope,
        for the selected consultation period."""
        if isinstance(request.user, AnonymousUser):
            return Response([])
        period_start, period_end = _parse_period(request)
        staff_qs, config = _staff_queryset_for_request(request)
        solution = resolve_authorized_solution_for_request(request)
        entries = []
        for staff in staff_qs.select_related("user"):
            result = compute_staff_workload(
                staff, period_start, period_end, solution=solution,
            )
            serialized = serialize_workload_result(result)
            # Grid rows never need the per-action detail payload.
            serialized.pop("contributions", None)
            serialized["staff_name"] = staff.user.full_name
            entries.append(serialized)
        return Response(entries)

    def retrieve(self, request, pk=None):
        """The collaborator-detail panel: full capacity/charge/rate +
        per-action detail + anomalies, for the selected consultation
        period."""
        if isinstance(request.user, AnonymousUser):
            return Response({"detail": "Not found."}, status=404)
        period_start, period_end = _parse_period(request)
        staff_qs, config = _staff_queryset_for_request(request)
        staff = staff_qs.select_related("user").filter(pk=pk).first()
        if staff is None:
            return Response({"detail": "Not found."}, status=404)
        solution = resolve_authorized_solution_for_request(request)
        result = compute_staff_workload(staff, period_start, period_end, solution=solution)
        payload = serialize_workload_result(result)
        payload["staff_name"] = staff.user.full_name
        return Response(payload)


class StaffWorkloadAlertConfigViewSet(viewsets.ModelViewSet):
    """AI-2054: configuration of overload-alert recipients, one per
    company/solution (OneToOne on CompanyConfig) -- same isolation and
    gating recipe as AI-1685's LeaveBalanceReminderRuleViewSet, reserved to
    the dedicated `manage_workload_alerts` action (never view_workload
    alone)."""
    serializer_class = StaffWorkloadAlertConfigSerializer
    permission_classes = [WorkloadAlertManagementPermission]

    def _company_config_for_write(self):
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_write(self.request)
        )
        company_config = configs.first()
        if company_config is None:
            raise ValidationError("No company configuration found for this company/solution.")
        return company_config

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return StaffWorkloadAlertConfig.objects.none()
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_request(self.request)
        )
        return StaffWorkloadAlertConfig.objects.filter(company_config__in=configs).order_by("-id")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if self.request.method == "POST" and self.action == "create":
            context["company_config"] = self._company_config_for_write()
        return context

    def perform_create(self, serializer):
        serializer.save(company_config=self._company_config_for_write(), created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def recompute(self, request, pk=None):
        # AI-2054: manual recompute shares evaluate_staff_workload's exact
        # same episode/claim logic as the scheduled task -- never a
        # separate code path, never a forced resend to an already-served
        # recipient.
        config = self.get_object()
        try:
            entries = evaluate_config(config, manual=True)
        except WorkloadAlertBlockedError as exc:  # pragma: no cover - defensive, evaluate_config already isolates this
            return Response({"detail": str(exc)}, status=400)
        return Response({"config_id": config.id, "entries": entries})


class StaffWorkloadAlertEpisodeViewSet(viewsets.ReadOnlyModelViewSet):
    """AI-2054: administrative episode history -- read-only, same
    permission and company/solution scoping as the config viewset above."""
    permission_classes = [WorkloadAlertManagementPermission]

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return StaffWorkloadAlertEpisode.objects.none()
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_request(self.request)
        )
        qs = StaffWorkloadAlertEpisode.objects.filter(
            company_config__in=configs
        ).select_related("staff__user").order_by("-started_on", "-id")
        staff_id = self.request.query_params.get("staff")
        if staff_id:
            qs = qs.filter(staff_id=staff_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def list(self, request, *args, **kwargs):
        return Response([serialize_episode(e) for e in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        return Response(serialize_episode(self.get_object()))
