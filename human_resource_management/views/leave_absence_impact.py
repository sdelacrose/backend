from django.contrib.auth.models import AnonymousUser
from django.utils.dateparse import parse_date
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from companyprofileapis.permissions import CompanyConfigurationsPermission
from human_resource_management.models.leave_entitlement import (
    LeaveAccrualAbsenceSignal, LeaveTypeAbsenceImpactHistory,
)
from human_resource_management.permissions import (
    LeaveEntitlementPermission,
    resolve_authorized_solution_for_request,
    resolve_authorized_solution_for_write,
)
from human_resource_management.serializers.leave_absence_impact import (
    serialize_absence_impact_entry, serialize_absence_signal,
)
from human_resource_management.services.leave_absence_impact import (
    LeaveAbsenceImpactError, record_absence_impact_change,
)
from human_resource_management.services.leave_accrual import first_of_next_month
from human_resource_management.views.leave_type import _company_configs_for_request
from models.models import LeaveType


class LeaveTypeAbsenceImpactViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """LOT RH13: the explicit source-absence-type -> target-accrual-type
    policy, list (?source=<id>&target=<id>) and create only -- never
    update/delete (a deactivation is a new row with effect=NONE, same
    "never rewrite history" precedent as every other *History model here).
    Reuses `company_configurations`, the SAME permission already gating the
    Leave Types configuration screen this lives alongside."""

    def get_permissions(self):
        if self.action == "list":
            return [CompanyConfigurationsPermission("view", "company")]
        return [CompanyConfigurationsPermission("update", "company")]

    def _company_leave_types(self, request, *, resolver=resolve_authorized_solution_for_request):
        # LOT RH13 follow-up (solution isolation): scoped to the ACTIVE
        # solution's own CompanyConfig(s), reusing the exact same mechanism
        # leave_type.py's own configuration screen already uses -- a company
        # configured for more than one solution must never let a policy pair
        # a source/target from two DIFFERENT solutions, nor list them.
        #
        # `resolver`: LOT RH13 follow-up (write-path hardening) -- `list`
        # (read) uses the lenient read-path resolver (default); `create`
        # (write) passes the stricter write-path resolver instead, which
        # refuses outright on an explicit unauthorized solution rather than
        # silently falling back.
        if isinstance(request.user, AnonymousUser):
            return LeaveType.objects.none()
        return LeaveType.objects.filter(
            company_config__in=_company_configs_for_request(
                request, product_name=resolver(request)
            )
        )

    def get_queryset(self):
        allowed = self._company_leave_types(self.request)
        qs = LeaveTypeAbsenceImpactHistory.objects.filter(
            source_leave_type__in=allowed, target_leave_type__in=allowed,
        ).select_related("source_leave_type", "target_leave_type", "created_by").order_by(
            "source_leave_type_id", "target_leave_type_id", "-effective_from", "-created_at",
        )
        source_id = self.request.query_params.get("source")
        target_id = self.request.query_params.get("target")
        if source_id:
            qs = qs.filter(source_leave_type_id=source_id)
        if target_id:
            qs = qs.filter(target_leave_type_id=target_id)
        return qs

    def list(self, request, *args, **kwargs):
        return Response([serialize_absence_impact_entry(e) for e in self.get_queryset()])

    def create(self, request, *args, **kwargs):
        allowed = self._company_leave_types(request, resolver=resolve_authorized_solution_for_write)
        source_id = request.data.get("source_leave_type")
        target_id = request.data.get("target_leave_type")
        source = allowed.filter(pk=source_id).first()
        target = allowed.filter(pk=target_id).first()
        if source is None or target is None:
            raise ValidationError(
                "Both source and target leave types must belong to your own company."
            )
        if target.effective_accrual_rate() is None:
            raise ValidationError(
                "The target leave type must be configured for monthly accrual "
                "(an effective rate must be set) -- this policy only ever affects "
                "monthly accrual, never quota or request-day-counting."
            )

        effect = request.data.get("effect")
        if effect not in dict(LeaveTypeAbsenceImpactHistory.EFFECT_CHOICES):
            raise ValidationError({"effect": "Must be one of none/suspend/reduce."})
        reduction_factor = request.data.get("reduction_factor")
        if effect == LeaveTypeAbsenceImpactHistory.EFFECT_REDUCE:
            if reduction_factor in (None, ""):
                raise ValidationError({"reduction_factor": "Required for REDUCE."})
        else:
            reduction_factor = None

        effective_from_raw = request.data.get("effective_from")
        if effective_from_raw:
            effective_from = parse_date(str(effective_from_raw))
            if effective_from is None:
                raise ValidationError({"effective_from": "Must be a valid ISO date (YYYY-MM-DD)."})
            if effective_from.day != 1:
                raise ValidationError(
                    {"effective_from": "A policy change can only take effect on the first day of a month."}
                )
        else:
            effective_from = first_of_next_month()

        try:
            entry = record_absence_impact_change(
                source, target, effect, reduction_factor,
                effective_from=effective_from, created_by=request.user,
            )
        except LeaveAbsenceImpactError as exc:
            raise ValidationError(str(exc))
        return Response(serialize_absence_impact_entry(entry), status=status.HTTP_201_CREATED)


class LeaveAccrualAbsenceSignalViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    """LOT RH13: late-decision signals -- consultation + an explicit
    `resolve` action only. Reuses `LeaveEntitlementPermission` (the SAME
    real-HR-reviewer gate already protecting Leave Entitlements/Accrual
    History) -- never `staff_management`, never widened."""
    permission_classes = [LeaveEntitlementPermission]

    def get_queryset(self):
        qs = LeaveAccrualAbsenceSignal.objects.select_related(
            "entitlement__employee__user", "entitlement__leave_type", "credit", "resolved_by",
        ).order_by("-detected_at")
        if isinstance(self.request.user, AnonymousUser):
            return LeaveAccrualAbsenceSignal.objects.none()
        if self.request.user.is_freelancer:
            qs = qs.filter(entitlement__employee__user=self.request.user)
        else:
            qs = qs.filter(entitlement__employee__user__company=self.request.user.company)
        # LOT RH13 follow-up (solution isolation): a signal's own credit is
        # always for a leave type scoped to one CompanyConfig -- restrict to
        # the ACTIVE solution's own configs, same mechanism as the policy
        # screen above.
        qs = qs.filter(
            entitlement__leave_type__company_config__in=_company_configs_for_request(
                self.request, product_name=resolve_authorized_solution_for_request(self.request)
            )
        )
        resolved_param = self.request.query_params.get("resolved")
        if resolved_param is not None:
            qs = qs.filter(resolved=resolved_param.lower() == "true")
        return qs

    def list(self, request, *args, **kwargs):
        return Response([serialize_absence_signal(s) for s in self.get_queryset()])

    def retrieve(self, request, *args, **kwargs):
        signal = self.get_queryset().filter(pk=kwargs["pk"]).first()
        if signal is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_absence_signal(signal))

    @action(detail=True, methods=["post"])
    def resolve(self, request, *args, **kwargs):
        # LOT RH13: an EXPLICIT, distinct RH action -- never triggered just
        # because an adjustment happened to be made via the pre-existing
        # mechanism. A note is required: this is the "traitement explicite"
        # the signal's own existence is meant to force, not a silent close.
        signal = self.get_queryset().filter(pk=kwargs["pk"]).first()
        if signal is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        note = (request.data.get("resolution_note") or "").strip()
        if not note:
            raise ValidationError({"resolution_note": "A justification is required to resolve a signal."})
        from django.utils import timezone
        signal.resolved = True
        signal.resolved_by = request.user
        signal.resolved_at = timezone.now()
        signal.resolution_note = note
        signal.save(update_fields=["resolved", "resolved_by", "resolved_at", "resolution_note"])
        return Response(serialize_absence_signal(signal))
