from datetime import date
from decimal import Decimal, InvalidOperation

from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.utils.dateparse import parse_date
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ecpmpapp.utils import get_user_active_solution
from human_resource_management.models.leave_entitlement import LeaveEntitlement
from human_resource_management.permissions import (
    LeaveEntitlementPermission,
    has_cumulative_leave_review_permission,
    resolve_authorized_solution_for_request,
    resolve_authorized_solution_for_write,
)
from human_resource_management.serializers.leave_entitlement import (
    serialize_accrual_credit,
    serialize_cycle_breakdown,
    serialize_entitlement,
    serialize_entitlement_history_entry,
)
from human_resource_management.services.leave_accrual import first_of_next_month
from human_resource_management.services.leave_entitlement import (
    LeaveEntitlementError,
    entitlement_available,
    sum_existing_requests,
    get_entitlement_breakdown,
    resolve_cycle_period,
)
from human_resource_management.services.leave_entitlement_activation import (
    ActivationBlockedError,
    activate as run_activation,
    build_activation_preview,
)
from human_resource_management.services.leave_balance import get_organization_working_days
from human_resource_management.views.leave_type import _company_configs_for_request
from ecpmpapp.constants import StatusType
from models.models import CompanyConfig, LeaveType, Staff


def _consumed_reserved(entitlement):
    company_profile_id = entitlement.employee.user.company_id
    working_days = get_organization_working_days(company_profile_id)
    consumed = sum_existing_requests(
        entitlement.employee, entitlement.leave_type, entitlement.cycle_period,
        status=StatusType.APPROVED.value, exclude_request_id=None,
        company_profile_id=company_profile_id, working_days=working_days,
    )
    reserved = sum_existing_requests(
        entitlement.employee, entitlement.leave_type, entitlement.cycle_period,
        status=StatusType.PENDING.value, exclude_request_id=None,
        company_profile_id=company_profile_id, working_days=working_days,
    )
    return consumed, reserved


class LeaveEntitlementViewSet(viewsets.GenericViewSet):
    """LOT RH 9: consultation/attribution/adjustment/history of per-employee/
    per-type/per-cycle entitlements. RH (has_cumulative_leave_review_
    permission) sees and manages any employee in their company; any other
    authenticated employee may only ever consult their OWN entitlements --
    never another's, not even a direct report's (a manager/Company Manager/
    Admin Server-only user gets nothing here beyond their own row, same as
    the rest of this module's confidentiality rules)."""

    permission_classes = [IsAuthenticated]

    def _staff_or_404(self, employee_id, company_id):
        staff = Staff.objects.filter(pk=employee_id, user__company_id=company_id).select_related("user").first()
        if staff is None:
            return None
        return staff

    def list(self, request):
        user = request.user
        if isinstance(user, AnonymousUser) or not user.company_id:
            return Response([])
        is_reviewer = has_cumulative_leave_review_permission(user)

        employee_param = request.query_params.get("employee")
        if is_reviewer:
            if employee_param:
                staff = self._staff_or_404(employee_param, user.company_id)
                if staff is None:
                    return Response({"employee": "Unknown or out-of-scope employee id."}, status=400)
                queryset = LeaveEntitlement.objects.filter(employee=staff)
            else:
                queryset = LeaveEntitlement.objects.filter(employee__user__company_id=user.company_id)
        else:
            # LOT RH 9: "ne pas exposer les droits d'autrui aux managers" --
            # a non-reviewer can only ever see their own, regardless of what
            # `employee` they ask for.
            own_staff = getattr(user, "staff", None)
            if own_staff is None:
                return Response([])
            if employee_param and str(employee_param) != str(own_staff.id):
                raise DRFPermissionDenied()
            queryset = LeaveEntitlement.objects.filter(employee=own_staff)

        # LOT RH13 follow-up (solution isolation): scoped to the ACTIVE
        # solution's own CompanyConfig(s), never the whole company -- applied
        # ONCE here regardless of which branch above built the queryset, so
        # a company configured for more than one solution never surfaces
        # another solution's counters, whether listing by employee or not.
        queryset = queryset.filter(
            leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            )
        )

        leave_type_param = request.query_params.get("leave_type")
        if leave_type_param:
            queryset = queryset.filter(leave_type_id=leave_type_param)

        queryset = queryset.select_related("employee__user", "leave_type", "cycle_period").order_by(
            "employee_id", "leave_type_id", "-cycle_period__period_start"
        )
        results = []
        for entitlement in queryset:
            consumed, reserved = _consumed_reserved(entitlement)
            results.append(serialize_entitlement(entitlement, consumed=consumed, reserved=reserved))
        return Response(results)

    def create(self, request):
        if not has_cumulative_leave_review_permission(request.user):
            raise DRFPermissionDenied()
        user = request.user
        employee_id = request.data.get("employee")
        leave_type_id = request.data.get("leave_type")
        reference_date = parse_date(request.data.get("reference_date") or "")
        reason = (request.data.get("reason") or "").strip()
        quota_raw = request.data.get("quota")

        if not employee_id or not leave_type_id or not reference_date:
            return Response(
                {"detail": "employee, leave_type and reference_date are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not reason:
            return Response({"reason": "A reason is required for every attribution."}, status=400)
        try:
            quota = Decimal(str(quota_raw))
        except (InvalidOperation, TypeError):
            return Response({"quota": "A valid decimal quota is required."}, status=400)

        staff = self._staff_or_404(employee_id, user.company_id)
        if staff is None:
            return Response({"employee": "Unknown or out-of-scope employee id."}, status=400)
        leave_type = LeaveType.objects.filter(
            pk=leave_type_id,
            company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_write(request)
            ),
        ).first()
        if leave_type is None:
            return Response({"leave_type": "Unknown or out-of-scope leave type id."}, status=400)

        try:
            cycle_period = resolve_cycle_period(leave_type, reference_date)
        except LeaveEntitlementError as exc:
            return Response({"reference_date": str(exc)}, status=400)

        if LeaveEntitlement.objects.filter(
            employee=staff, leave_type=leave_type, cycle_period=cycle_period
        ).exists():
            return Response(
                {"detail": "An entitlement already exists for this employee/type/cycle -- use the adjust action instead."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # LOT RH 10: the mode is decided here, once, from the leave type's
        # effective monthly rate -- never retroactively: an existing
        # quota-mode entitlement is NEVER switched to monthly mode just
        # because the type later gets a rate configured (see LeaveType's
        # effective_accrual_rate docstring).
        accrual_rate_override = None
        accrual_override_effective_from = None
        if leave_type.effective_accrual_rate() is not None:
            mode = LeaveEntitlement.MODE_MONTHLY_ACCRUAL
            override_raw = request.data.get("accrual_rate_override")
            if override_raw not in (None, ""):
                try:
                    accrual_rate_override = Decimal(str(override_raw))
                except (InvalidOperation, TypeError):
                    return Response({"accrual_rate_override": "A valid decimal rate is required."}, status=400)
                # LOT RH10F closure: an override set AT CREATION still needs
                # an effective date -- None (unconditional/always-in-force)
                # would otherwise let a brand-new entitlement's override
                # reach back and silently govern cycle months that elapsed
                # BEFORE this attribution decision was even made.
                #
                # Two genuinely different situations, same as a rate CHANGE
                # vs. a type's very first rate:
                #   - "deja en acquisition": the employee could already have
                #     had presence in an EARLIER month under this type
                #     (contract_start_date/accrual_effective_date fall
                #     before the current month) -- a backlog may exist, even
                #     if not yet actually credited. The override then
                #     follows the EXACT same rule as a rate change: defaults
                #     to the 1st of NEXT month, rejects an explicit mid-month
                #     date -- the current, in-progress month keeps using
                #     whatever rate already governed it (the type's rate),
                #     never split mid-month.
                #   - true first-ever entry (no possible earlier presence at
                #     all): no restriction, exactly like the type's own
                #     first-ever rate anchoring to accrual_effective_date --
                #     the override applies to that first, possibly partial,
                #     ALREADY-VALIDATED prorated month, unchanged.
                earliest_possible_start = staff.contract_start_date
                type_effective_date = leave_type.effective_accrual_effective_date()
                if type_effective_date is not None and (
                    earliest_possible_start is None or type_effective_date > earliest_possible_start
                ):
                    earliest_possible_start = type_effective_date
                today = date.today()
                has_backlog = (
                    earliest_possible_start is not None
                    and (earliest_possible_start.year, earliest_possible_start.month)
                    < (today.year, today.month)
                )

                raw_effective_from = request.data.get("override_effective_from")
                if raw_effective_from in (None, ""):
                    accrual_override_effective_from = first_of_next_month() if has_backlog else None
                else:
                    accrual_override_effective_from = parse_date(str(raw_effective_from))
                    if accrual_override_effective_from is None:
                        return Response(
                            {"override_effective_from": "A valid date (YYYY-MM-DD) is required."},
                            status=400,
                        )
                    if has_backlog and accrual_override_effective_from.day != 1:
                        return Response(
                            {
                                "override_effective_from": (
                                    "This employee could already have prior presence under this "
                                    "type -- an override can only take effect on the first day of "
                                    "a month, never mid-month."
                                )
                            },
                            status=400,
                        )
        else:
            mode = LeaveEntitlement.MODE_QUOTA

        entitlement = LeaveEntitlement.objects.create(
            employee=staff, leave_type=leave_type, cycle_period=cycle_period,
            mode=mode, quota=quota, last_adjustment_reason=reason, last_adjusted_by=user,
            accrual_rate_override=accrual_rate_override,
            accrual_override_effective_from=accrual_override_effective_from,
        )
        consumed, reserved = _consumed_reserved(entitlement)
        return Response(
            serialize_entitlement(entitlement, consumed=consumed, reserved=reserved),
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def adjust(self, request, pk=None):
        """LOT RH 9 follow-up: an adjustment is a commitment-affecting write
        exactly like a reservation, so it takes the SAME per-employee lock
        (`Staff.objects.select_for_update()`) before re-reading live
        consumed/reserved and deciding -- a concurrent reservation and a
        concurrent adjustment can therefore never race past each other.
        Refused outright if the new value would make the available balance
        negative: whether a reduction below already-approved/reserved
        commitments should ever be allowed is an open business decision this
        lot does NOT resolve -- it is never silently enabled here.

        LOT RH 10: for a monthly-accrual entitlement, `quota` here is the net
        manual adjustment (not an absolute cap) -- `available` is computed via
        `entitlement_available`, which adds the posted accrual-credit total
        for monthly mode and leaves quota-mode behavior byte-for-byte
        unchanged."""
        if not has_cumulative_leave_review_permission(request.user):
            raise DRFPermissionDenied()
        entitlement = LeaveEntitlement.objects.filter(
            pk=pk, employee__user__company_id=request.user.company_id,
            leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_write(request)
            ),
        ).select_related("employee__user", "leave_type", "cycle_period").first()
        if entitlement is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"reason": "A reason is required for every adjustment."}, status=400)
        try:
            quota = Decimal(str(request.data.get("quota")))
        except (InvalidOperation, TypeError):
            return Response({"quota": "A valid decimal quota is required."}, status=400)

        # LOT RH 10: individual RH rate override, monthly-accrual entitlements only --
        # silently ignored (not rejected) for a quota-mode entitlement, same as the
        # UI never showing the field for that mode.
        accrual_rate_override = entitlement.accrual_rate_override
        override_changed = False
        if entitlement.mode == LeaveEntitlement.MODE_MONTHLY_ACCRUAL and "accrual_rate_override" in request.data:
            override_raw = request.data.get("accrual_rate_override")
            if override_raw in (None, ""):
                accrual_rate_override = None
            else:
                try:
                    accrual_rate_override = Decimal(str(override_raw))
                except (InvalidOperation, TypeError):
                    return Response({"accrual_rate_override": "A valid decimal rate is required."}, status=400)
            override_changed = accrual_rate_override != entitlement.accrual_rate_override

        # LOT RH10F closure: a genuine override CHANGE requires an explicit
        # `override_effective_from` (validated to the 1st of a month),
        # defaulting to the 1st of NEXT month when omitted -- the exact
        # "retroactive mid-month" fix already applied at the type-rate
        # level. Recorded into the entitlement's OWN
        # `accrual_override_effective_from` field (a genuine business date,
        # distinct from `simple_history`'s `history_date` audit timestamp --
        # see that field's docstring on the model) rather than disguised as
        # when the row was saved, so the real modification date/author/audit
        # trail is never disturbed. Never touches any already-posted
        # LeaveAccrualCredit.
        override_effective_from = None
        if override_changed:
            raw_effective_from = request.data.get("override_effective_from")
            if raw_effective_from in (None, ""):
                override_effective_from = first_of_next_month()
            else:
                override_effective_from = parse_date(str(raw_effective_from))
                if override_effective_from is None:
                    return Response(
                        {"override_effective_from": "A valid date (YYYY-MM-DD) is required."},
                        status=400,
                    )
                if override_effective_from.day != 1:
                    return Response(
                        {"override_effective_from": "An override change can only take effect on the first day of a month."},
                        status=400,
                    )

        with transaction.atomic():
            Staff.objects.select_for_update().get(pk=entitlement.employee_id)
            entitlement.refresh_from_db()
            # LOT RH11: finalize any overdue carry-over first, then refuse
            # outright if this entitlement's cycle is already closed --
            # same guard every balance-affecting write goes through.
            from human_resource_management.services.leave_carryover import (
                LeaveCarryOverError, finalize_due_carryovers, raise_if_cycle_closed,
            )
            finalize_due_carryovers(entitlement)
            try:
                raise_if_cycle_closed(entitlement)
            except LeaveCarryOverError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
            consumed, reserved = _consumed_reserved(entitlement)
            # entitlement_available(entitlement, ...) reads the PERSISTED entitlement.quota;
            # shifting its result by (new - old) probes the candidate value without an
            # extra query or a fragile unsaved-instance stand-in, and stays correct for
            # both modes since quota enters the formula identically either way (either
            # as the absolute quota, or as the net adjustment term).
            available = (
                entitlement_available(entitlement, consumed, reserved) - entitlement.quota + quota
            )
            if available < 0:
                return Response(
                    {
                        "quota": (
                            f"This value ({quota}) would make the available balance negative "
                            f"({available}): {consumed} already consumed and {reserved} already "
                            f"reserved. Reducing below existing commitments is not supported."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # LOT RH 9: never a silent overwrite -- `simple_history` captures
            # the PRIOR quota/reason as its own historical row before this
            # save commits the new one.
            entitlement.quota = quota
            entitlement.accrual_rate_override = accrual_rate_override
            entitlement.last_adjustment_reason = reason
            entitlement.last_adjusted_by = request.user
            if override_changed:
                entitlement.accrual_override_effective_from = override_effective_from
            entitlement.save(update_fields=[
                "quota", "accrual_rate_override", "accrual_override_effective_from",
                "last_adjustment_reason", "last_adjusted_by", "updated_at",
            ])

        consumed, reserved = _consumed_reserved(entitlement)
        return Response(serialize_entitlement(entitlement, consumed=consumed, reserved=reserved))

    @action(detail=True, methods=["get"])
    def history(self, request, pk=None):
        user = request.user
        entitlement = LeaveEntitlement.objects.filter(
            pk=pk, employee__user__company_id=getattr(user, "company_id", None),
            leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ),
        ).select_related("employee__user").first()
        if entitlement is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        own_staff = getattr(user, "staff", None)
        is_owner = own_staff is not None and own_staff.id == entitlement.employee_id
        if not (is_owner or has_cumulative_leave_review_permission(user)):
            raise DRFPermissionDenied()
        records = entitlement.history.all().select_related("history_user").order_by("-history_date")
        return Response([serialize_entitlement_history_entry(r) for r in records])

    @action(detail=True, methods=["get"], url_path="accrual-history")
    def accrual_history(self, request, pk=None):
        """LOT RH 10: the posted monthly-credit ledger for one entitlement --
        same ownership/RH visibility rule as `history` above."""
        user = request.user
        entitlement = LeaveEntitlement.objects.filter(
            pk=pk, employee__user__company_id=getattr(user, "company_id", None),
            leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ),
        ).select_related("employee__user").first()
        if entitlement is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        own_staff = getattr(user, "staff", None)
        is_owner = own_staff is not None and own_staff.id == entitlement.employee_id
        if not (is_owner or has_cumulative_leave_review_permission(user)):
            raise DRFPermissionDenied()
        credits = entitlement.accrual_credits.select_related("created_by").order_by("-period_start")
        return Response([serialize_accrual_credit(c) for c in credits])

    @action(detail=True, methods=["get"], url_path="carryover-history")
    def carryover_history(self, request, pk=None):
        """LOT RH11: this entitlement's own closure (if closed), any
        carry-over it RECEIVED (and its expiration, once resolved), and any
        carry-over it SENT to the immediately following cycle -- same
        ownership/RH visibility rule as `history`/`accrual_history` above.
        Consultation only, never gated by raise_if_cycle_closed (history is
        never blocked)."""
        user = request.user
        entitlement = LeaveEntitlement.objects.filter(
            pk=pk, employee__user__company_id=getattr(user, "company_id", None),
            leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ),
        ).select_related("employee__user").first()
        if entitlement is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        own_staff = getattr(user, "staff", None)
        is_owner = own_staff is not None and own_staff.id == entitlement.employee_id
        if not (is_owner or has_cumulative_leave_review_permission(user)):
            raise DRFPermissionDenied()
        from human_resource_management.serializers.leave_entitlement import (
            serialize_carryover_history,
        )
        return Response(serialize_carryover_history(entitlement))

    @action(detail=False, methods=["get"])
    def breakdown(self, request):
        user = request.user
        if isinstance(user, AnonymousUser) or not user.company_id:
            return Response([])
        is_reviewer = has_cumulative_leave_review_permission(user)

        employee_param = request.query_params.get("employee")
        own_staff = getattr(user, "staff", None)
        if is_reviewer and employee_param:
            staff = self._staff_or_404(employee_param, user.company_id)
            if staff is None:
                return Response({"employee": "Unknown or out-of-scope employee id."}, status=400)
        else:
            if employee_param and own_staff and str(employee_param) != str(own_staff.id):
                raise DRFPermissionDenied()
            staff = own_staff
        if staff is None:
            return Response([])

        leave_type_id = request.query_params.get("leave_type")
        start_date = parse_date(request.query_params.get("start_date") or "")
        end_date = parse_date(request.query_params.get("end_date") or "")
        if not leave_type_id or not start_date or not end_date:
            return Response(
                {"detail": "leave_type, start_date and end_date are all required."}, status=400
            )
        leave_type = LeaveType.objects.filter(
            pk=leave_type_id,
            company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ),
        ).first()
        if leave_type is None:
            return Response({"leave_type": "Unknown or out-of-scope leave type id."}, status=400)

        try:
            breakdown = get_entitlement_breakdown(staff, leave_type, start_date, end_date)
        except LeaveEntitlementError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response([serialize_cycle_breakdown(b) for b in breakdown])


class LeaveEntitlementActivationViewSet(viewsets.GenericViewSet):
    """LOT RH 9, section 5: `preview`/`activate` are RH-only. `status` is
    deliberately open to ANY authenticated company employee (not just RH) --
    it is a plain boolean + date, nothing confidential, and every
    collaborator's own submission form needs it to know whether to show
    droits/disponible or the legacy global balance."""

    def get_permissions(self):
        if self.action in ("preview", "activate"):
            return [LeaveEntitlementPermission()]
        return [IsAuthenticated()]

    def _company_config(self, request):
        # LOT RH13 follow-up (write-path hardening): activation CREATES a
        # CompanyConfig row (get_or_create) -- never resolved under
        # get_user_active_solution's own silent fallback for a user with no
        # license at all, which would otherwise fabricate a row for a solution
        # the user was never actually granted.
        solution = resolve_authorized_solution_for_write(request)
        config, _ = CompanyConfig.objects.get_or_create(
            company_profile_id=request.user.company_id, solution=solution, user=None,
        )
        return config

    @action(detail=False, methods=["get"], url_path="status", url_name="status")
    def activation_status(self, request):
        user = request.user
        if isinstance(user, AnonymousUser) or not user.company_id:
            return Response({"is_active": False, "activated_at": None, "transition_leave_type_id": None})
        # Read-only: never creates a CompanyConfig row, and never needs the
        # heavier solution/license resolution `activate()` uses -- same
        # "any CompanyConfig row for this company" check as
        # is_entitlement_mode_active_for_employee, just without a Staff
        # instance in hand yet.
        config = _company_configs_for_request(
            request, product_name=resolve_authorized_solution_for_request(request)
        ).filter(leave_entitlement_mode_active=True).first()
        if config is None:
            return Response({"is_active": False, "activated_at": None, "transition_leave_type_id": None})
        return Response({
            "is_active": True,
            "activated_at": (
                config.leave_entitlement_activated_at.isoformat()
                if config.leave_entitlement_activated_at else None
            ),
            "transition_leave_type_id": config.leave_entitlement_transition_leave_type_id,
        })

    @action(detail=False, methods=["get"])
    def preview(self, request):
        transition_leave_type_id = request.query_params.get("transition_leave_type")
        try:
            result = build_activation_preview(
                request.user.company_id, transition_leave_type_id=transition_leave_type_id,
                solution=get_user_active_solution(request.user.id),
            )
        except ActivationBlockedError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response(_serialize_preview(result))

    @action(detail=False, methods=["post"])
    def activate(self, request):
        transition_leave_type_id = request.data.get("transition_leave_type_id")
        if not transition_leave_type_id:
            return Response({"transition_leave_type_id": "Required."}, status=400)
        raw_quotas = request.data.get("transition_quotas") or {}
        transition_quotas = {}
        for employee_id, value in raw_quotas.items():
            try:
                transition_quotas[int(employee_id)] = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError):
                return Response({"transition_quotas": f"Invalid quota for employee {employee_id}."}, status=400)

        config = self._company_config(request)
        try:
            preview = run_activation(
                config,
                transition_leave_type_id=transition_leave_type_id,
                transition_quotas=transition_quotas,
                activated_by=request.user,
            )
        except ActivationBlockedError as exc:
            blocked_preview = build_activation_preview(
                request.user.company_id, transition_leave_type_id=transition_leave_type_id
            )
            return Response(
                {"detail": str(exc), "preview": _serialize_preview(blocked_preview)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"activated": True, "preview": _serialize_preview(preview)})


def _serialize_preview(preview):
    return {
        "transition_leave_type_id": preview.transition_leave_type_id,
        "types_and_cycles": preview.types_and_cycles,
        "pending_count": preview.pending_count,
        "approved_count": preview.approved_count,
        "ambiguous_request_ids": preview.ambiguous_request_ids,
        "missing_counter_items": preview.missing_counter_items,
        "negative_available_items": preview.negative_available_items,
        "proposed_transition_quotas": {
            str(k): float(v) for k, v in preview.proposed_transition_quotas.items()
        },
        # LOT RH10 follow-up: the same activation switch, previewed for
        # monthly-accrual types too -- never summed with the quota figures
        # above.
        "monthly_accrual_types": preview.monthly_accrual_types,
        "monthly_missing_config_items": preview.monthly_missing_config_items,
        "monthly_missing_contract_items": preview.monthly_missing_contract_items,
        "monthly_timezone_blocked": preview.monthly_timezone_blocked,
        "monthly_projected": preview.monthly_projected,
        "can_activate": preview.can_activate,
    }
