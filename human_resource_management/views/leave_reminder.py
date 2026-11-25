from django.contrib.auth.models import AnonymousUser
from django.utils.dateparse import parse_date
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from human_resource_management.models.reminder import LeaveBalanceReminderRule, LeaveReminderDelivery
from human_resource_management.permissions import (
    LeaveReminderManagementPermission,
    resolve_authorized_solution_for_request,
    resolve_authorized_solution_for_write,
)
from human_resource_management.serializers.reminder import (
    LeaveBalanceReminderRuleSerializer,
    serialize_delivery,
)
from human_resource_management.services.leave_accrual import LeaveAccrualTimezoneError
from human_resource_management.services.leave_reminders import (
    LeaveReminderBlockedError,
    build_rule_preview,
    process_rule,
)
from human_resource_management.views.leave_type import _company_configs_for_request


class LeaveBalanceReminderRuleViewSet(viewsets.ModelViewSet):
    """AI-1685: configuration, collective preview, and manual trigger for
    leave-balance reminder rules -- scoped to the SAME company/solution
    isolation as LeaveType (reuses _company_configs_for_request exactly),
    gated by the dedicated staff_management.manage_leave_reminders action
    (never staff_management.update/full_access, never the bare role name)."""
    serializer_class = LeaveBalanceReminderRuleSerializer
    permission_classes = [LeaveReminderManagementPermission]

    def _company_config_for_write(self):
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_write(self.request)
        )
        company_config = configs.first()
        if company_config is None:
            raise ValidationError(
                "No company configuration found for this company/solution."
            )
        return company_config

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return LeaveBalanceReminderRule.objects.none()
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_request(self.request)
        )
        return LeaveBalanceReminderRule.objects.filter(company_config__in=configs).order_by("-id")

    def get_serializer_context(self):
        # Only CREATE needs a resolved company_config in context -- an
        # UPDATE's serializer already has `self.instance.company_config`
        # (the object this rule was created under), which the serializer's
        # own validate_* methods fall back to directly.
        context = super().get_serializer_context()
        if self.request.method == "POST" and self.action == "create":
            context["company_config"] = self._company_config_for_write()
        return context

    def perform_create(self, serializer):
        serializer.save(company_config=self._company_config_for_write(), created_by=self.request.user)

    @action(detail=True, methods=["get"])
    def preview(self, request, pk=None):
        rule = self.get_object()
        try:
            entries = build_rule_preview(rule)
        except LeaveAccrualTimezoneError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"rule_id": rule.id, "entries": entries})

    @action(detail=True, methods=["post"])
    def trigger(self, request, pk=None):
        # AI-1685: manual trigger shares build_rule_preview/process_rule's
        # exact same recheck and anti-duplicate claim as the scheduled
        # command -- never a separate code path, never a forced resend of
        # an occurrence already delivered.
        rule = self.get_object()
        try:
            entries = process_rule(rule, manual=True)
        except LeaveReminderBlockedError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"rule_id": rule.id, "entries": entries})


class LeaveReminderDeliveryViewSet(viewsets.ReadOnlyModelViewSet):
    """AI-1685: administrative delivery history -- read-only, same
    permission and company/solution scoping as the rule viewset above."""
    permission_classes = [LeaveReminderManagementPermission]

    def get_queryset(self):
        if isinstance(self.request.user, AnonymousUser):
            return LeaveReminderDelivery.objects.none()
        configs = _company_configs_for_request(
            self.request, product_name=resolve_authorized_solution_for_request(self.request)
        )
        qs = LeaveReminderDelivery.objects.filter(
            rule__company_config__in=configs
        ).select_related("rule", "staff__user", "recipient").order_by("-created_at")
        rule_id = self.request.query_params.get("rule")
        if rule_id:
            qs = qs.filter(rule_id=rule_id)
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def list(self, request, *args, **kwargs):
        return Response([serialize_delivery(d) for d in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        return Response(serialize_delivery(self.get_object()))
