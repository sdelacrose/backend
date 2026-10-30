import logging
from datetime import date
from decimal import Decimal
from rest_framework import serializers
from rest_framework.reverse import reverse
from django.db import transaction
from django.utils import timezone
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave import HALF_DAY_AFTERNOON, HALF_DAY_MORNING, HALF_DAY_PERIODS
from human_resource_management.permissions import (
    can_view_staff_compensation,
    has_cumulative_leave_review_permission,
    is_hr_reviewer_for,
)
from .staff import StaffSerializer

logger = logging.getLogger(__name__)
from human_resource_management.utils import is_weekend
from human_resource_management.services.leave_balance import (
    RESOLVED,
    UNRESOLVED_NO_MATCH,
    compute_leave_request_days,
    current_year_bounds,
    get_employee_leave_summary,
    get_organization_working_days,
    resolve_leave_type,
)
from human_resource_management.services.leave_entitlement import (
    LeaveEntitlementError,
    check_entitlement_or_raise_for_items,
    entitlement_available,
    is_entitlement_mode_active_for_employee,
    sum_existing_requests,
)
from human_resource_management.models.leave_entitlement import LeaveEntitlement
from project_management.serializers.time_expense import get_valid_choices
from ecpmpapp.constants import StatusType, LeaveType
from models.models import Staff, UserRole
from models.models import LeaveSubType as LeaveSubTypeModel
from models.models import LeaveType as LeaveTypeModel


def _is_legacy_non_consuming(item):
    return item.type in [LeaveType.SICK.value, LeaveType.UNPAID.value]


def _requires_supporting_document(leave_type):
    """Only ever consulted for a request linked by the stable `leave_type` FK
    (LOT RH 3) -- a legacy/unresolved request has no reliable type to ask, and
    this lot was explicitly told never to guess for those."""
    return bool(leave_type is not None and leave_type.effective_value('requires_supporting_document'))


def serialize_leave_attachment(attachment, request):
    """Never includes the raw storage URL (`attachment.file.url`) -- only a
    link back to LeaveRequestAttachmentViewSet.download, which re-checks
    permission on every call. This is the one thing the closest existing
    precedents in this codebase (DocumentAttachment, CorrespondenceAttachment)
    get wrong: both also return a raw, unsigned, non-expiring file_url
    alongside their gated download_url."""
    return {
        "id": attachment.id,
        "file_name": attachment.file_name,
        "file_size": attachment.file_size,
        "uploaded_by": (
            {"id": attachment.uploaded_by_id, "name": getattr(attachment.uploaded_by, "full_name", None)}
            if attachment.uploaded_by_id else None
        ),
        "uploaded_at": attachment.uploaded_at,
        "download_url": reverse(
            "leave-request-attachment-download",
            kwargs={"leave_request_id": attachment.leave_request_id, "attachment_id": attachment.id},
            request=request,
        ),
    }


def serialize_leave_for_calendar(leave, *, include_type):
    """LOT RH 6: the ONE shape every calendar surface uses for a leave
    request -- `LeaveRequestViewSet.calendar` and the `api/user/events`
    aggregate (`CalendarEventViewSet.list`) both call this instead of each
    hand-rolling their own dict. Deliberately an allowlist that never
    includes `reason`/`attachments`/`leave_type`/`leave_sub_type`/balance
    data for ANY viewer, HR included -- those stay in the secured
    leave-request detail, never the calendar payload. `include_type` is the
    only axis of difference: False for a pure team manager (confidentiality,
    see LeaveRequestManagerViewSerializer), True for everyone else who can
    see the row at all."""
    data = {
        "id": leave.id,
        "status": leave.status,
        "start_date": leave.start_date,
        "end_date": leave.end_date,
        "is_half_day": leave.is_half_day,
        "half_day_period": leave.half_day_period,
        "employee": {
            "id": leave.employee_id,
            "name": getattr(leave.employee.user, "full_name", None),
        },
    }
    if include_type:
        data["type"] = leave.type
    return data


def serialize_manager_opinion(opinion):
    """LOT RH 5: purely informational -- never implies a decision. Shown to
    the owner and to HR (see LeaveRequestSerializer.to_representation) and,
    in a stripped-down form, to the team manager themselves via
    LeaveRequestManagerViewSerializer."""
    if opinion is None:
        return None
    return {
        "opinion": opinion.opinion,
        "comment": opinion.comment,
        "given_by": (
            {"id": opinion.given_by_id, "name": getattr(opinion.given_by, "full_name", None)}
            if opinion.given_by_id else None
        ),
        "given_at": opinion.given_at,
        "updated_at": opinion.updated_at,
    }


def _check_balance_or_raise(*, employee, items, exclude_request_id=None):
    """Shared by the single-row path (_enforce_balance_or_raise, a batch of
    one) and the multi-row 'Add New Request' table submission (LOT RH 3):
    `items` are unsaved LeaveRequest-like probes.

    LOT RH9: work-from-home items ALWAYS stay on the legacy global
    calendar-year mechanism below (`Staff.wfh_leaves_allowed`), regardless of
    activation -- "Préserver séparément le télétravail" is that lot's own
    explicit instruction, untouched here. A regular-leave item routes to the
    NEW mechanism (`check_entitlement_or_raise_for_items`) instead of the
    legacy path ONLY for a company that has explicitly activated the
    mechanism (`CompanyConfig.leave_entitlement_mode_active`) -- an existing,
    non-activated company's regular-leave items keep running through the
    exact legacy comparison unchanged, never a silent switch.

    LOT RH10 follow-up (activation explicite): configuring a leave type's
    monthly accrual rate alone does NOT route anything here -- the earlier
    behaviour that treated a type with an effective accrual rate as
    implicitly "activated" (bypassing the company's own explicit activation
    step) has been reverted. The activation preview/service (LOT RH9,
    extended by LOT RH10 for monthly-mode blocking conditions) is the one
    and only switch, reused unchanged for both modes.

    LOT RH10 follow-up (no exemption once activated): the historical "company
    admins and freelancers are exempt" rule is preserved ONLY for a company
    that has NOT activated the mechanism (exactly the legacy behaviour it
    always had). Once a company activates entitlement mode, administrators
    and freelancers are checked like any other collaborator for every
    non-WFH item -- no role may reserve or consume rights it has not
    acquired. WFH keeps its own, separate legacy exemption regardless of
    activation (orthogonal to this lot).
    """
    entitlement_mode_active = is_entitlement_mode_active_for_employee(employee)
    is_exempt_role = employee.user.is_company_admin or employee.user.is_freelancer
    if is_exempt_role and not entitlement_mode_active:
        return

    company_profile_id = employee.user.company_id

    legacy_items = []
    entitlement_items = []
    for item in items:
        if item.is_working_from_home:
            if is_exempt_role:
                continue  # WFH's own legacy exemption, unrelated to entitlement activation
            legacy_items.append(item)
            continue
        if not entitlement_mode_active:
            legacy_items.append(item)
            continue
        leave_type = getattr(item, "leave_type", None)
        if leave_type is None:
            # LOT RH9: once activated, an unresolved type is never a silent
            # fallback to the legacy global balance -- it is the exact
            # "compteur absent" case this lot requires to surface explicitly.
            raise serializers.ValidationError(
                "No leave entitlement counter configured for this request. "
                "Please contact HR."
            )
        if not leave_type.effective_value("consumes_balance"):
            continue  # no reservation/deduction concept for a non-consuming type
        entitlement_items.append(item)

    years = set()
    for item in legacy_items:
        years.update(range(item.start_date.year, item.end_date.year + 1))

    for year in sorted(years):
        year_start, year_end = current_year_bounds(date(year, 1, 1))
        summary = get_employee_leave_summary(
            employee, year_start, year_end, exclude_request_id=exclude_request_id
        )
        added_leave = Decimal(0)
        added_wfh = Decimal(0)
        for item in legacy_items:
            if not getattr(item, "leave_type_id", None):
                _, resolution = (
                    resolve_leave_type(item.type, company_profile_id)
                    if item.type
                    else (None, UNRESOLVED_NO_MATCH)
                )
                # Once a type resolves to a configured LeaveType, its own
                # consumes_balance governs (see compute_leave_request_days) --
                # the legacy Sick/Unpaid exemption only still applies to
                # historical/unresolved type strings.
                if resolution != RESOLVED and _is_legacy_non_consuming(item):
                    continue
            breakdown = compute_leave_request_days(
                item, year_start, year_end, company_profile_id=company_profile_id
            )
            if breakdown.deductible <= 0:
                continue
            if item.is_working_from_home:
                added_wfh += breakdown.deductible
            else:
                added_leave += breakdown.deductible
        if added_leave > 0 and (summary.leave_balance is None or added_leave > summary.leave_balance):
            raise serializers.ValidationError(
                f"Employee does not have enough leave balance in {year}."
            )
        if added_wfh > 0 and (summary.wfh_balance is None or added_wfh > summary.wfh_balance):
            raise serializers.ValidationError(
                f"Employee does not have enough work-from-home balance in {year}."
            )

    if entitlement_items:
        try:
            check_entitlement_or_raise_for_items(
                employee, entitlement_items, exclude_request_id=exclude_request_id
            )
        except LeaveEntitlementError as exc:
            raise serializers.ValidationError(str(exc))


def _enforce_balance_or_raise(
    *,
    employee,
    start_date,
    end_date,
    leave_type_name,
    is_working_from_home,
    exclude_request_id=None,
    is_half_day=False,
    half_day_period=None,
    leave_type=None,
):
    """Shared by validate() (fast, unlocked pre-check at submission time) and the
    locked recheck in create()/update() (authoritative, runs again after the
    per-employee lock is held -- see LOT RH 2's concurrency requirement)."""
    probe = LeaveRequest(
        start_date=start_date,
        end_date=end_date,
        type=leave_type_name,
        is_working_from_home=is_working_from_home,
        is_half_day=is_half_day,
        half_day_period=half_day_period,
    )
    if leave_type is not None:
        probe.leave_type = leave_type
    _check_balance_or_raise(employee=employee, items=[probe], exclude_request_id=exclude_request_id)


def _half_day_compatible(a_is_half, a_period, b_is_half, b_period):
    """True when two absences can coexist on their overlapping date(s) --
    only when BOTH are half-day requests on opposite periods. A full day on
    either side always conflicts with anything on that date (LOT RH 3)."""
    return bool(a_is_half and b_is_half and a_period != b_period)


def _requests_conflict(a_start, a_end, a_is_half, a_period, b_start, b_end, b_is_half, b_period):
    if a_end < b_start or b_end < a_start:
        return False
    return not _half_day_compatible(a_is_half, a_period, b_is_half, b_period)


def _resolve_and_validate_identity(
    *, company_profile_id, type_name, leave_type, leave_sub_type, is_half_day, half_day_period, is_working_from_home
):
    """LOT RH 3: validate the stable LeaveType/LeaveSubType identity link and
    the half-day fields for one row. Returns (type_name, leave_type,
    leave_sub_type) to persist -- `type_name` is re-derived from
    `leave_type.name` when an id was given, so the historical display label
    never drifts out of sync with the live type. Never guesses: a type
    outside this company, inactive, a sub-type that doesn't belong to the
    given type, or a half-day on a type that doesn't allow it, is rejected
    outright rather than silently accepted or silently dropped."""
    if is_working_from_home:
        if leave_type is not None or leave_sub_type is not None:
            raise serializers.ValidationError(
                {"leave_type": "A leave type cannot be set on a work-from-home request."}
            )
        if is_half_day:
            raise serializers.ValidationError(
                {"is_half_day": "Half-day is not supported for work-from-home requests in this release."}
            )
        return type_name, None, None

    if leave_type is not None:
        if (
            leave_type.company_config_id is None
            or leave_type.company_config.company_profile_id != company_profile_id
        ):
            raise serializers.ValidationError(
                {"leave_type": "This leave type does not belong to your company."}
            )
        if leave_type.effective_value("is_active") is False:
            raise serializers.ValidationError({"leave_type": "This leave type is not active."})

        active_sub_types = list(leave_type.sub_types.filter(is_active=True))
        if active_sub_types:
            if leave_sub_type is None:
                raise serializers.ValidationError(
                    {"leave_sub_type": "A sub-type is required for this leave type."}
                )
            if leave_sub_type.leave_type_id != leave_type.id or not leave_sub_type.is_active:
                raise serializers.ValidationError(
                    {"leave_sub_type": "Invalid sub-type for this leave type."}
                )
        elif leave_sub_type is not None:
            raise serializers.ValidationError(
                {"leave_sub_type": "This leave type does not have sub-types."}
            )

        if is_half_day:
            if not leave_type.effective_value("allow_half_day"):
                raise serializers.ValidationError(
                    {"is_half_day": "This leave type does not allow half-day requests."}
                )
            if half_day_period not in (HALF_DAY_MORNING, HALF_DAY_AFTERNOON):
                raise serializers.ValidationError(
                    {"half_day_period": "Morning or afternoon is required for a half-day request."}
                )
        elif half_day_period:
            raise serializers.ValidationError(
                {"half_day_period": "half_day_period can only be set for a half-day request."}
            )

        return leave_type.name, leave_type, leave_sub_type

    # No stable id given (a historical row being re-saved unchanged, or a
    # legacy/not-yet-updated client sending only the free-form type string).
    if is_half_day:
        raise serializers.ValidationError(
            {"is_half_day": "A specific, active leave type must be selected to request a half-day."}
        )
    if leave_sub_type is not None:
        raise serializers.ValidationError(
            {"leave_sub_type": "A leave type must be selected to use a sub-type."}
        )
    if half_day_period:
        raise serializers.ValidationError(
            {"half_day_period": "half_day_period can only be set for a half-day request."}
        )
    return type_name, None, None


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee = StaffSerializer(read_only=True)
    leave_type = serializers.PrimaryKeyRelatedField(
        queryset=LeaveTypeModel.objects.all(), required=False, allow_null=True
    )
    leave_sub_type = serializers.PrimaryKeyRelatedField(
        queryset=LeaveSubTypeModel.objects.all(), required=False, allow_null=True
    )
    is_half_day = serializers.BooleanField(required=False, default=False)
    half_day_period = serializers.ChoiceField(choices=HALF_DAY_PERIODS, required=False, allow_null=True)

    class Meta:
        model = LeaveRequest
        fields = ["id", "type", "leave_type", "leave_sub_type", "start_date", "end_date", "status", "employee",
                  "reason", "is_working_from_home", "is_half_day", "half_day_period"]
        extra_kwargs = {
            'type': {'required': False, 'allow_null': True}
        }

    def validate(self, attrs):
        request = self.context.get('request')
        user = request.user
        instance = self.instance

        # Determine employee from the request or instance.
        employee = user.staff if not instance else instance.employee

        # Retrieve the is_working_from_home flag.
        is_working_from_home = attrs.get('is_working_from_home', instance.is_working_from_home if instance else False)

        # Retrieve leave type if provided (it is now optional).
        type_name = attrs.get('type', instance.type if instance else None)
        leave_type = attrs.get('leave_type', instance.leave_type if instance else None)
        leave_sub_type = attrs.get('leave_sub_type', instance.leave_sub_type if instance else None)
        is_half_day = attrs.get('is_half_day', instance.is_half_day if instance else False)
        half_day_period = attrs.get('half_day_period', instance.half_day_period if instance else None)

        # Validate leave type only if provided.
        if not is_working_from_home and type_name is not None:
            request = self.context.get('request')
            request_user = request.user
            if request_user.is_freelancer:
                valid_choices = get_valid_choices("leave_types", request_user.id, request, is_freelancer=True)
            else:
                valid_choices = get_valid_choices("leave_types", request_user.company.id, request, is_company=True)
            if valid_choices and type_name not in valid_choices:
                raise serializers.ValidationError(
                    {"type": f"Invalid leave type. Allowed choices are: {', '.join(valid_choices)}"}
                )

        start_date = attrs.get('start_date', instance.start_date if instance else None)
        end_date = attrs.get('end_date', instance.end_date if instance else None)

        if is_weekend(start_date):
            raise serializers.ValidationError("Start date cannot be on a weekend")
        if is_weekend(end_date):
            raise serializers.ValidationError("End date cannot be on a weekend")

        if not start_date or not end_date:
            raise serializers.ValidationError("Start date and End date are required.")

        if start_date > end_date:
            raise serializers.ValidationError("Start date cannot be after end date.")

        # LOT RH 5: a past start/end date is now allowed (regularization) --
        # removed entirely rather than narrowed, per spec. Every other check
        # here (weekend, start<=end, half-day, overlap, balance, below) is
        # unchanged and still applies exactly as before to a past-dated
        # request; only the "cannot be in the past" rejection itself is gone.

        # LOT RH 3: resolve/validate the stable type/sub-type identity link
        # and the half-day fields. `type_name` comes back re-derived from the
        # resolved LeaveType's own name when an id was given.
        type_name, leave_type, leave_sub_type = _resolve_and_validate_identity(
            company_profile_id=employee.user.company_id,
            type_name=type_name,
            leave_type=leave_type,
            leave_sub_type=leave_sub_type,
            is_half_day=is_half_day,
            half_day_period=half_day_period,
            is_working_from_home=is_working_from_home,
        )
        if is_half_day and start_date != end_date:
            raise serializers.ValidationError(
                {"is_half_day": "A half-day request must be for a single date."}
            )
        # LOT L04 (SST): refused outright even when submitted directly
        # (leave_type id, never only the free-text `type` name) -- an SST
        # type is only ever created by the dedicated SST service, never
        # through this classic endpoint.
        if leave_type is not None and leave_type.is_sst_managed:
            raise serializers.ValidationError(
                {"leave_type": "This leave type is managed by the SST module, not the classic leave request form."}
            )

        attrs['type'] = type_name
        attrs['leave_type'] = leave_type
        attrs['leave_sub_type'] = leave_sub_type
        attrs['is_half_day'] = is_half_day
        attrs['half_day_period'] = half_day_period if is_half_day else None

        # Fast, unlocked pre-check for immediate UI feedback -- the authoritative
        # recheck happens again under lock in create()/update() below. When
        # editing an existing (still-pending) request's own dates/type, exclude
        # it from its own balance check -- it isn't APPROVED yet so the
        # aggregation wouldn't double-count it anyway, but this makes that
        # guarantee explicit rather than incidental (LOT RH 2A).
        _enforce_balance_or_raise(
            employee=employee,
            start_date=start_date,
            end_date=end_date,
            leave_type_name=type_name,
            is_working_from_home=is_working_from_home,
            exclude_request_id=instance.id if instance else None,
            is_half_day=is_half_day,
            half_day_period=attrs['half_day_period'],
            leave_type=leave_type,
        )

        overlap_requests = LeaveRequest.objects.filter(
            employee=employee,
            start_date__lte=end_date,
            end_date__gte=start_date,
            status__in=[StatusType.PENDING.value, StatusType.APPROVED.value]
        )

        if instance:
            overlap_requests = overlap_requests.exclude(id=instance.id)

        # Half-day aware (LOT RH 3): a full day conflicts with anything on
        # that date; two half-days on the SAME date only conflict if they
        # share the same period (morning+afternoon may coexist).
        has_conflict = any(
            _requests_conflict(
                start_date, end_date, is_half_day, attrs['half_day_period'],
                candidate.start_date, candidate.end_date, candidate.is_half_day, candidate.half_day_period,
            )
            for candidate in overlap_requests
        )
        if has_conflict:
            raise serializers.ValidationError("Employee has already requested leave for these dates.")

        return attrs

    def create(self, validated_data):
        """Create a leave request. Always starts pending -- LOT RH4 closure:
        every creation path (this one, the batch path below, and a directly
        client-supplied `status` in the payload) is closed off from ever
        setting anything else. The only way a request becomes approved or
        rejected is an explicit HR review action through update(), which is
        the one place that still re-checks `leave_request.review` and the
        "not your own request" rule. `create_without_approval`/freelancer
        auto-approval previously bypassed this entirely; removed."""
        with transaction.atomic():
            request = self.context.get('request')
            request_user = request.user
            employee = request_user.staff
            validated_data.pop("status", None)
            # LOT RH 5: decided once, here, from the submission date (this
            # codebase has no per-organization timezone -- TIME_ZONE=UTC is
            # already what every other date check here uses) vs this
            # request's own start_date. Never recomputed afterwards.
            validated_data["is_regularization"] = (
                validated_data.get("start_date") < timezone.now().date()
            )

            # LOT RH 9: a NEW, locked reservation check -- only for a company
            # that activated the per-type/per-cycle entitlement mechanism.
            # Before this lot, `pending` never reserved anything (the only
            # authoritative, locked balance check happened at approval time,
            # see update() below); activation makes a pending request hold
            # back quota other concurrent submissions cannot also claim, so
            # two simultaneous creations must now be fully serialized here
            # too. A non-activated company's create() is entirely unchanged.
            # (LOT RH10 follow-up: configuring a type's monthly rate alone no
            # longer bypasses this gate -- see _check_balance_or_raise.)
            if is_entitlement_mode_active_for_employee(employee):
                Staff.objects.select_for_update().get(pk=employee.pk)
                probe = LeaveRequest(employee=employee, **validated_data)
                _check_balance_or_raise(employee=employee, items=[probe])

            leave_request = LeaveRequest.objects.create(
                employee=employee,
                **validated_data
            )

        return leave_request

    def update(self, instance, validated_data):
        """Update leave request with proper email notifications."""
        with transaction.atomic():
            request = self.context.get('request')
            request_user = request.user

            if "status" in validated_data:
                # Stable, common lock per collaborator, acquired BEFORE re-checking
                # status and (for an approval) balance -- so a duplicate/concurrent
                # approval attempt for the same employee is fully serialized rather
                # than merely checked-then-raced. Re-fetch under the lock: the
                # `instance` DRF handed us was read before we held it.
                Staff.objects.select_for_update().get(pk=instance.employee_id)
                instance.refresh_from_db()

                if instance.status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
                    # Already resolved by the time we got the lock (e.g. a duplicate
                    # approval click, or a concurrent reviewer) -- no further effect.
                    raise serializers.ValidationError("Request has already been reviewed.")

                employee = instance.employee
                new_status = validated_data["status"]

                if new_status not in {
                    StatusType.APPROVED.value,
                    StatusType.REJECTED.value,
                }:
                    raise serializers.ValidationError(
                        "Leave status can only be approved or rejected during review."
                    )

                if employee.user == request_user:
                    raise serializers.ValidationError(
                        "You cannot approve or reject your own leave request."
                    )

                if (
                    request_user.is_freelancer
                    or not request_user.company_id
                    or employee.user.company_id != request_user.company_id
                ):
                    raise serializers.ValidationError(
                        "You cannot review leave requests outside your company."
                    )

                if not has_cumulative_leave_review_permission(request_user):
                    raise serializers.ValidationError(
                        "You need leave review permission to approve or reject "
                        "leave requests."
                    )

                # LOT RH13: no reliable decision audit existed before this lot
                # (see LeaveRequest.decided_at's own comment) -- populated
                # HERE, the one place status is ever set, never backfilled
                # for a request decided before this lot shipped.
                validated_data["decided_at"] = timezone.now()
                validated_data["decided_by"] = request_user

                if new_status == StatusType.APPROVED.value:
                    # LOT RH 4: re-checked here, under the same per-employee
                    # lock just acquired above (and refreshed from DB) -- a
                    # concurrent attachment deletion acquires this identical
                    # lock before removing the last required document (see
                    # LeaveRequestAttachmentViewSet.destroy), so the two can
                    # never race: whichever gets the lock first is the one
                    # that's consistent by the time the other proceeds.
                    if _requires_supporting_document(instance.leave_type) and not instance.attachments.exists():
                        raise serializers.ValidationError(
                            "This leave type requires a supporting document before it can be approved."
                        )
                    # LOT RH11: a PENDING request may already carry a FROZEN
                    # `LeaveCarryOverAllocation` (its carry-over expired while
                    # it was still pending). That frozen portion is already
                    # historically secured and must never be re-derived
                    # against a now-gone envelope -- only the REMAINING
                    # (acquisition-financed) portion is re-checked live.
                    # Never blocks approval of an unchanged pending request
                    # merely because its allocation is frozen.
                    frozen_allocations = list(instance.carry_over_allocations.all())
                    if frozen_allocations:
                        from human_resource_management.services.leave_carryover import (
                            finalize_due_carryovers, raise_if_cycle_closed,
                        )
                        frozen_total = sum(
                            (a.quantity_allocated for a in frozen_allocations), Decimal(0)
                        )
                        entitlement = LeaveEntitlement.objects.filter(
                            employee=employee, leave_type=instance.leave_type,
                            cycle_period__period_start__lte=instance.start_date,
                            cycle_period__period_end__gte=instance.start_date,
                        ).first()
                        if entitlement is not None:
                            finalize_due_carryovers(entitlement)
                            raise_if_cycle_closed(entitlement)
                        company_profile_id = employee.user.company_id
                        working_days = get_organization_working_days(company_profile_id)
                        total_requested = compute_leave_request_days(
                            instance, instance.start_date, instance.end_date,
                            company_profile_id=company_profile_id, working_days=working_days,
                        ).deductible
                        remaining_to_check = total_requested - frozen_total
                        if remaining_to_check > 0 and entitlement is not None:
                            own_consumed = sum_existing_requests(
                                employee, instance.leave_type, entitlement.cycle_period,
                                status=StatusType.APPROVED.value, exclude_request_id=instance.id,
                                company_profile_id=company_profile_id, working_days=working_days,
                            )
                            own_available = entitlement_available(entitlement, own_consumed, Decimal(0))
                            if remaining_to_check > own_available:
                                raise serializers.ValidationError(
                                    f"Insufficient leave entitlement for '{instance.leave_type.name}': "
                                    f"available {own_available} (own, beyond the already-secured "
                                    f"{frozen_total} from an expired carry-over), requested {remaining_to_check}."
                                )
                    else:
                        _enforce_balance_or_raise(
                            employee=employee,
                            start_date=instance.start_date,
                            end_date=instance.end_date,
                            leave_type_name=instance.type,
                            is_working_from_home=instance.is_working_from_home,
                            exclude_request_id=instance.id,
                            is_half_day=instance.is_half_day,
                            half_day_period=instance.half_day_period,
                            leave_type=instance.leave_type,
                        )
            else:
                # Non-status edit (e.g. a still-pending request's own dates/type).
                if instance.status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
                    raise serializers.ValidationError("Request has already been reviewed.")

                # LOT RH11: a request already carrying a FROZEN
                # `LeaveCarryOverAllocation` (its carry-over expired while
                # still pending) never distinguishes its original version
                # from new dates -- blocked for this lot, first-class:
                # date/duration/type changes are refused outright.
                # Reason/attachment-only edits remain allowed, and approval/
                # rejection of the UNCHANGED request is handled separately
                # above, never blocked by this.
                #
                # validate() unconditionally re-derives type/leave_type/
                # is_half_day/half_day_period into attrs for every PATCH
                # (even a reason-only one, see _resolve_and_validate_identity
                # above) -- so checking mere PRESENCE in validated_data would
                # false-block every edit. Compare each against the instance's
                # current value instead: only a genuine change counts.
                _BALANCE_AFFECTING_FIELDS = {
                    "start_date", "end_date", "type", "leave_type",
                    "is_half_day", "half_day_period", "is_working_from_home",
                }
                balance_fields_actually_changing = {
                    field for field in _BALANCE_AFFECTING_FIELDS
                    if field in validated_data and validated_data[field] != getattr(instance, field)
                }
                if (
                    balance_fields_actually_changing
                    and instance.carry_over_allocations.exists()
                ):
                    raise serializers.ValidationError(
                        "This request's balance was already allocated against a "
                        "since-expired carry-over -- its dates, duration or type "
                        "can no longer be modified. Cancel and resubmit instead."
                    )

                employee = instance.employee
                # LOT RH 9: "modification des dates/types met à jour la
                # projection sous verrou" -- re-check under the same lock,
                # excluding THIS request's own current reservation, against
                # the NEW requested dates/type. Only for an activated
                # company; unchanged otherwise. (LOT RH10 follow-up: a
                # monthly-accrual-configured type alone no longer bypasses
                # this gate -- see _check_balance_or_raise.)
                if is_entitlement_mode_active_for_employee(employee):
                    Staff.objects.select_for_update().get(pk=instance.employee_id)
                    instance.refresh_from_db()
                    if instance.status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
                        raise serializers.ValidationError("Request has already been reviewed.")
                    probe = LeaveRequest(
                        employee=employee,
                        start_date=validated_data.get("start_date", instance.start_date),
                        end_date=validated_data.get("end_date", instance.end_date),
                        type=validated_data.get("type", instance.type),
                        leave_type=validated_data.get("leave_type", instance.leave_type),
                        is_working_from_home=validated_data.get(
                            "is_working_from_home", instance.is_working_from_home
                        ),
                        is_half_day=validated_data.get("is_half_day", instance.is_half_day),
                        half_day_period=validated_data.get("half_day_period", instance.half_day_period),
                    )
                    _check_balance_or_raise(employee=employee, items=[probe], exclude_request_id=instance.id)

            updated = super().update(instance, validated_data)
            if "status" in validated_data:
                # LOT RH13: a side effect only -- never allowed to break the
                # approve/reject action itself if signal detection fails.
                from human_resource_management.services.leave_absence_signal import (
                    check_late_decision_signal,
                )
                try:
                    check_late_decision_signal(updated, event_type=new_status, actor=request_user)
                except Exception:
                    logger.exception("RH13 late-decision signal check failed for request %s", updated.pk)
            return updated

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        # LOT RH13 follow-up / L00-A: `employee` is the full StaffSerializer
        # (salary, hourly_rate included) -- correct for Staff Management
        # itself, but a leave-review right alone must never carry it along
        # for the ride. Stripped here (never a separate, narrower employee
        # serializer) so every surface that renders a LeaveRequestSerializer
        # -- list, detail, review queue, batch rows -- is covered by this ONE
        # place. L00-A reconnects this to the now-dedicated compensation
        # control (can_view_staff_compensation: self, view_compensation, or
        # the canonical Company Admin) -- a bare staff_management.view/
        # full_access no longer implies this on its own (DEC-02/06).
        if representation.get("employee") is not None:
            request = self.context.get('request')
            viewer = getattr(request, 'user', None)
            if not (viewer and can_view_staff_compensation(viewer, instance.employee)):
                representation["employee"].pop("salary", None)
                representation["employee"].pop("hourly_rate", None)
        representation["created_by_solution_role_details"] = None
        user_solution_role = UserRole.objects.filter(user=instance.employee.user, role__type="solution", role__scope="company").first()
        if user_solution_role:
            representation["created_by_solution_role_details"] = {
                "id": user_solution_role.id,
                "name": user_solution_role.role.name,
                "description": user_solution_role.role.description,
                "is_default": user_solution_role.role.is_default,
                "is_active": user_solution_role.role.is_active,
            }

        # LOT RH 4: `requires_supporting_document` is the type's own config
        # (already visible to everyone via Leave Type config), fine to expose
        # unconditionally -- drives the "justificatif attendu" indicator
        # without an extra round-trip. The attachment list itself is the
        # actual document metadata/consultation this lot restricts to the
        # owner or HR (same `leave_request.review` gate as approval) --
        # omitted entirely for anyone else, not just left empty.
        representation["requires_supporting_document"] = _requires_supporting_document(instance.leave_type)
        # LOT RH 5: a fixed, immutable fact set at creation -- never
        # recomputed, see LeaveRequestSerializer.create/LeaveRequestBatch
        # Serializer.create. Informational only, visible to anyone who can
        # see the request at all (owner or HR).
        representation["is_regularization"] = instance.is_regularization
        representation["manager_opinion"] = serialize_manager_opinion(
            getattr(instance, "manager_opinion", None)
        )
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is not None and (
            user == instance.employee.user or is_hr_reviewer_for(user, instance.employee)
        ):
            representation["attachments"] = [
                serialize_leave_attachment(a, request) for a in instance.attachments.all()
            ]
        return representation


class _ManagerViewEmployeeSerializer(serializers.Serializer):
    """LOT RH 5: identity only -- never StaffSerializer (which also carries
    leave_taken/leave_left/salary/productivity -- exactly what item 3 says
    must never reach a team manager)."""
    id = serializers.IntegerField(read_only=True)
    name = serializers.CharField(source="user.full_name", read_only=True)


class LeaveRequestManagerViewSerializer(serializers.ModelSerializer):
    """LOT RH 5: the team manager's confidentiality-restricted view of a
    direct report's leave request. A deliberate ALLOWLIST (not the shared
    LeaveRequestSerializer with fields hidden after the fact) -- `type`,
    `leave_sub_type`, `reason`, and `attachments` are simply never fields
    on this serializer at all, so a future field added to the real one can
    never leak here by omission. Only ever selected by
    LeaveRequestViewSet.get_serializer_class for a user who holds
    `leave_request.manager_opinion` and none of full_access/view/
    view_created/review -- a pure team manager, never HR."""
    employee = _ManagerViewEmployeeSerializer(read_only=True)
    manager_opinion = serializers.SerializerMethodField()

    class Meta:
        model = LeaveRequest
        fields = [
            "id", "employee", "start_date", "end_date",
            "is_half_day", "half_day_period", "status", "manager_opinion",
        ]

    def get_manager_opinion(self, instance):
        return serialize_manager_opinion(getattr(instance, "manager_opinion", None))


class LeaveRequestBatchSerializer(serializers.Serializer):
    """Multi-row 'Add New Request' table submission (LOT RH 3, My Bookings).
    Each row is revalidated through the SAME LeaveRequestSerializer a
    single-row submission uses -- identity/sub-type, half-day, weekend/past-
    date, and each row's own DB-overlap/balance checks are not duplicated
    here. This serializer only adds what no single row can see on its own:
    conflicts between the rows of this SAME submission (siblings aren't in
    the DB yet), and their COMBINED effect on each touched year's balance.
    A row stays an independent LeaveRequest for its own future HR review --
    this is a convenience for submitting several at once, not a new entity."""
    rows = serializers.ListField(child=serializers.DictField(), allow_empty=False)

    def validate(self, attrs):
        request = self.context['request']
        row_serializers = []
        row_errors = {}

        for index, row_data in enumerate(attrs['rows']):
            row_serializer = LeaveRequestSerializer(data=row_data, context={'request': request})
            if row_serializer.is_valid():
                row_serializers.append(row_serializer)
            else:
                row_errors[index] = row_serializer.errors
        if row_errors:
            raise serializers.ValidationError({'rows': row_errors})

        # LOT RH4 closure: strip any client-supplied `status` from every row
        # right away, same as the single-row path -- a row dict never carries
        # one from here on, so create() below has nothing to accidentally use.
        validated_rows = [
            {k: v for k, v in s.validated_data.items() if k != "status"}
            for s in row_serializers
        ]
        for i in range(len(validated_rows)):
            a = validated_rows[i]
            for j in range(i + 1, len(validated_rows)):
                b = validated_rows[j]
                if _requests_conflict(
                    a['start_date'], a['end_date'], a.get('is_half_day', False), a.get('half_day_period'),
                    b['start_date'], b['end_date'], b.get('is_half_day', False), b.get('half_day_period'),
                ):
                    raise serializers.ValidationError(
                        {'rows': {j: {'start_date': f'Conflicts with row {i} of this same submission.'}}}
                    )

        employee = request.user.staff
        probes = [LeaveRequest(employee=employee, **row) for row in validated_rows]
        # Fast, unlocked cumulative pre-check (mirrors the single-row path) --
        # the authoritative recheck happens again under lock in create() below.
        _check_balance_or_raise(employee=employee, items=probes)

        attrs['rows'] = validated_rows
        return attrs

    def create(self, validated_data):
        """LOT RH4 closure: every row always starts pending, same as the
        single-row path -- create_without_approval/freelancer auto-approval
        removed entirely (it used to bypass HR review per-row here)."""
        request = self.context['request']
        employee = request.user.staff
        rows = validated_data['rows']
        # LOT RH 5: same submission-time instant for every row in this one
        # batch call -- consistent with "decided once, from the submission
        # date", not re-evaluated row by row.
        today = timezone.now().date()

        with transaction.atomic():
            # Same stable per-employee lock as the single-row path -- covers
            # ALL rows of this submission together, so a concurrent approval
            # of an unrelated pending request for the same employee can never
            # race past it.
            Staff.objects.select_for_update().get(pk=employee.pk)

            # LOT RH 9: the authoritative, locked recheck validate() already
            # claimed happens here -- added now that activation makes it
            # meaningful (a non-activated company's batch create is
            # unchanged; validate()'s own fast pre-check already covered it
            # exactly as before). (LOT RH10 follow-up: a monthly-accrual-
            # configured type alone no longer bypasses this gate.)
            if is_entitlement_mode_active_for_employee(employee):
                probes = [LeaveRequest(employee=employee, **row) for row in rows]
                _check_balance_or_raise(employee=employee, items=probes)

            created = [
                LeaveRequest.objects.create(
                    employee=employee,
                    is_regularization=row.get("start_date") < today,
                    **row,
                )
                for row in rows
            ]

        return created
