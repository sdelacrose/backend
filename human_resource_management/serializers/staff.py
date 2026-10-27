from datetime import date
from django.db.models import Q, Sum
from datetime import timedelta, date
from rest_framework.exceptions import PermissionDenied
from rest_framework import serializers

from human_resource_management.models import StaffObservation
from human_resource_management.models.staff import StaffContractAuditLog
from human_resource_management.permissions import (
    can_update_staff_compensation,
    can_view_staff_compensation,
)
from human_resource_management.services.leave_accrual import resolve_current_quotite_entry
from human_resource_management.services.leave_balance import (
    current_year_bounds,
    get_employee_leave_summary,
)
from models.models import User, Staff, StaffConfiguration
from project_management.models.project import Project
from ecpmpapp.utils import require_any_permission_multiple_modules

from datetime import date
from django.db.models import Sum, Q
from ecpmpapp.constants import StatusType, UserType

class StaffSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="user.full_name", read_only=True)
    user_id = serializers.CharField(source="user.id", read_only=True)
    mobile = serializers.CharField(source="user.mobile_phone", read_only=True)
    dob = serializers.DateField(source="user.dob", read_only=True)
    employment_status = serializers.SerializerMethodField()
    leave_taken = serializers.SerializerMethodField()
    leave_left = serializers.SerializerMethodField()
    work_from_home_taken = serializers.SerializerMethodField()
    work_from_home_left = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    project_allocation = serializers.SerializerMethodField()
    productivity_percentage = serializers.SerializerMethodField()
    total_allocated_hours = serializers.SerializerMethodField()
    total_spent_hours = serializers.SerializerMethodField()
    current_regime = serializers.SerializerMethodField()
    # L00-A: capability flags, always present (never themselves sensitive --
    # a plain boolean reveals nothing a stripped salary/hourly_rate already
    # doesn't), letting the frontend render an editable vs read-only vs
    # entirely-absent compensation field WITHOUT re-deriving "is this the
    # Company Admin" itself (see permissions.can_view_staff_compensation/
    # can_update_staff_compensation -- the one place that logic lives).
    can_view_compensation = serializers.SerializerMethodField()
    can_update_compensation = serializers.SerializerMethodField()
    # L00-B3.1: current + scheduled compensation, resolved LIVE from
    # StaffCompensationHistory (never from the raw salary/hourly_rate
    # mirror) -- same "actuel vs programme" shape as current_regime above.
    # Stripped in to_representation alongside salary/hourly_rate, same gate.
    compensation_situation = serializers.SerializerMethodField()
    # L00-B3.2: the consolidated employer-cost ESTIMATE (net estimate,
    # employer cost, ancillary costs included/excluded/non-monthly) --
    # resolved live, never stored, same masking gate as compensation_
    # situation above. Explicit `status` field distinguishes a complete
    # result from an incomplete/not-calculable one -- never a fabricated
    # total.
    employer_cost_estimate = serializers.SerializerMethodField()

    class Meta:
        model = Staff
        fields = [
            "name",
            "user_id",
            "mobile",
            "id",
            "dob",
            "employment_status",
            "annual_leaves_allowed",
            "wfh_leaves_allowed",
            "leave_taken",
            "leave_left",
            "work_from_home_taken",
            "work_from_home_left",
            "salary",
            "productivity_percentage",
            "hourly_rate",
            "location",
            "weekly_hours",
            "contract_start_date",
            "contract_end_date",
            "contract_type",
            "current_regime",
            "project_allocation",
            "user",
            "total_allocated_hours",
            "total_spent_hours",
            "can_view_compensation",
            "can_update_compensation",
            "compensation_situation",
            "employer_cost_estimate",
        ]

    def _viewer(self):
        request = self.context.get("request")
        return getattr(request, "user", None) if request else None

    def get_can_view_compensation(self, obj):
        viewer = self._viewer()
        return bool(viewer) and can_view_staff_compensation(viewer, obj)

    def get_can_update_compensation(self, obj):
        # L00-A/DEC-04: a self-edit of one's own compensation is refused
        # unconditionally regardless of any permission -- this capability
        # flag reflects that directly, rather than leaving the frontend to
        # separately special-case "but it's my own record".
        viewer = self._viewer()
        if not viewer or viewer == obj.user:
            return False
        return can_update_staff_compensation(viewer, obj)

    def get_employment_status(self, obj):
        return "Freelancer" if obj.user.is_freelancer else "Company User"

    def get_current_regime(self, obj):
        # AI-1685: full-time/part-time, read live from RH12's own quotite
        # history (resolve_current_quotite_entry) -- the ONE source of
        # truth, never a second computation. "unknown" (never "full_time")
        # when this employee has no quotite history at all yet -- a missing
        # history is NEVER read as 100%. A future-dated row (effective_from
        # > today) is surfaced separately as `scheduled_change`, distinct
        # from the regime actually in force today.
        today = date.today()
        current_entry = resolve_current_quotite_entry(obj, as_of=today)
        from human_resource_management.models.leave_entitlement import StaffQuotiteHistory
        next_entry = (
            StaffQuotiteHistory.objects.filter(staff=obj, effective_from__gt=today)
            .order_by("effective_from", "created_at")
            .first()
        )
        if current_entry is None:
            status_value = "unknown"
            quotite = None
        else:
            quotite = current_entry.quotite
            status_value = "full_time" if quotite == 1 else "part_time"
        return {
            "status": status_value,
            "quotite": float(quotite) if quotite is not None else None,
            "effective_from": current_entry.effective_from.isoformat() if current_entry else None,
            "scheduled_change": (
                {
                    "quotite": float(next_entry.quotite),
                    "effective_from": next_entry.effective_from.isoformat(),
                }
                if next_entry is not None else None
            ),
        }

    def get_compensation_situation(self, obj):
        # L00-B3.1: same "actuel vs programme" shape as get_current_regime
        # above, resolved live from StaffCompensationHistory -- never from
        # the raw salary/hourly_rate mirror (see services.staff_
        # compensation). None entirely (not an empty dict) when this
        # employee has no compensation history at all yet -- a legitimate,
        # not-yet-migrated state, never guessed. Masked alongside salary/
        # hourly_rate in to_representation -- this method itself does not
        # re-check permissions (same convention as the other capability/
        # detail fields here, gated centrally).
        from human_resource_management.services.staff_compensation import (
            resolve_current_compensation, resolve_next_scheduled_compensation,
        )
        from human_resource_management.serializers.staff_compensation_history import (
            serialize_compensation_event,
        )
        today = date.today()
        current_entry = resolve_current_compensation(obj, as_of=today)
        next_entry = resolve_next_scheduled_compensation(obj, as_of=today)
        return {
            "current": serialize_compensation_event(current_entry) if current_entry else None,
            "scheduled_change": serialize_compensation_event(next_entry) if next_entry else None,
        }

    def get_employer_cost_estimate(self, obj):
        # L00-B3.2: resolved live, as of today -- see services.staff_
        # employer_cost for the full rules (never a payroll engine, never
        # a legally-authoritative figure, explicit incomplete states).
        from human_resource_management.services.staff_employer_cost import (
            resolve_employer_cost_estimate,
        )
        return resolve_employer_cost_estimate(obj, as_of=date.today())

    def validate_salary(self, value):
        return self._guard_compensation_field_against_history("salary", value)

    def validate_hourly_rate(self, value):
        return self._guard_compensation_field_against_history("hourly_rate", value)

    def _guard_compensation_field_against_history(self, field_name, value):
        # L00-B3.1: same "a correction is a new row" guard as
        # validate_weekly_hours below, transposed to salary/hourly_rate --
        # once ANY StaffCompensationHistory exists for this employee, a
        # direct PATCH here that would actually CHANGE the value is refused
        # outright (resubmitting the SAME, already-current value is still
        # accepted, it writes nothing new). Record a new situation via the
        # dedicated endpoint instead.
        if (
            self.instance is not None
            and self.instance.pk
            and value != getattr(self.instance, field_name)
        ):
            from human_resource_management.models.staff import StaffCompensationHistory
            if StaffCompensationHistory.objects.filter(staff=self.instance).exists():
                raise serializers.ValidationError(
                    "This employee's compensation is tracked via the dedicated "
                    "compensation history -- record a new situation there "
                    "instead of editing this field directly, to avoid a "
                    "contradictory current value."
                )
        return value

    def validate_weekly_hours(self, value):
        # LOT RH12 follow-up: once this employee has ANY quotite history,
        # Staff.weekly_hours is a value DERIVED from that history (see
        # create_quotite_history_entry/to_representation above), never a
        # second, independently-editable source -- a direct PATCH here that
        # would actually CHANGE it is refused outright (re-submitting the
        # SAME, already-current value -- e.g. an untouched form field -- is
        # still accepted, it writes nothing new). Record a new entry via the
        # dedicated endpoint instead.
        if (
            self.instance is not None
            and self.instance.pk
            and value != self.instance.weekly_hours
        ):
            from human_resource_management.models.leave_entitlement import (
                StaffQuotiteHistory,
            )
            if StaffQuotiteHistory.objects.filter(staff=self.instance).exists():
                raise serializers.ValidationError(
                    "This employee's weekly hours are tracked via the dedicated "
                    "contractual-situation history -- record a new entry there "
                    "instead of editing this field directly, to avoid a "
                    "contradictory current value."
                )
        return value

    _CONTRACT_RELEVANT_FIELDS = ("contract_type", "contract_start_date", "contract_end_date")

    def validate(self, attrs):
        # AI-1685: contract_type is required once a contract-relevant field
        # is GENUINELY being set or changed -- never a blanket block on
        # every edit of the dossier (same targeted-validation precedent as
        # validate_weekly_hours above), and a resubmission of an UNCHANGED
        # value is never treated as a change. Staff creation itself is
        # disabled in this API (StaffViewSet.create returns 405 -- Staff
        # rows are provisioned upstream), so only the "effective
        # modification" path is actually reachable here.
        instance = self.instance
        resolved_contract_type = attrs.get(
            "contract_type", getattr(instance, "contract_type", None) if instance else None
        )
        is_relevant_change = False
        for field in self._CONTRACT_RELEVANT_FIELDS:
            if field not in attrs:
                continue
            if instance is None or attrs[field] != getattr(instance, field):
                is_relevant_change = True
                break
        if is_relevant_change and not resolved_contract_type:
            raise serializers.ValidationError({
                "contract_type": (
                    "Required once contractual information (contract type, start "
                    "or end date) is being set or changed for this employee."
                )
            })

        # L00-A: compensation authorization -- a REAL change only (never a
        # refusal for a form that resubmits the SAME, already-current value
        # unchanged -- same "targeted validation" precedent as
        # validate_weekly_hours above). A missing key in `attrs` is never
        # reachable here at all (PATCH leaves it untouched, see
        # StaffViewSet/partial update), so an unauthorized viewer whose form
        # never saw the real value and never sends the key back is never
        # blocked for editing anything ELSE in the same request.
        instance = self.instance
        if instance is not None:
            viewer = self._viewer()
            changed_comp_fields = [
                field for field in ("salary", "hourly_rate")
                if field in attrs and attrs[field] != getattr(instance, field)
            ]
            if changed_comp_fields:
                if viewer is not None and viewer == instance.user:
                    # DEC-04: no exception for an RH/Company Admin editing
                    # their OWN record -- this lot builds no validation
                    # circuit, so self-mutation is refused outright, never
                    # silently bypassed by any role this editor also holds.
                    raise serializers.ValidationError({
                        field: (
                            "You cannot change your own compensation -- this "
                            "requires validation by another authorized person "
                            "(not yet available)."
                        ) for field in changed_comp_fields
                    })
                if viewer is None or not can_update_staff_compensation(viewer, instance):
                    raise serializers.ValidationError({
                        field: "You do not have permission to change this employee's compensation."
                        for field in changed_comp_fields
                    })
        return attrs

    def update(self, instance, validated_data):
        # AI-1685: append-only audit trail for contract_type/contract_end_date
        # -- captured BEFORE save, written AFTER, covering this normal API
        # path only (never a fabricated past entry for changes made before
        # this lot existed).
        # L00-A: salary/hourly_rate reuse the exact same trail (never the
        # real values -- see StaffContractAuditLog.REDACTED_VALUE).
        audited_fields = ("contract_type", "contract_end_date", "salary", "hourly_rate")
        tracked_old_values = {field: getattr(instance, field) for field in audited_fields}
        request = self.context.get("request")
        actor = getattr(request, "user", None) if request else None
        updated = super().update(instance, validated_data)
        for field in audited_fields:
            old_value = tracked_old_values[field]
            new_value = getattr(updated, field)
            if old_value == new_value:
                continue
            is_compensation_field = field in ("salary", "hourly_rate")
            StaffContractAuditLog.objects.create(
                staff=updated, field=field,
                old_value=(
                    StaffContractAuditLog.REDACTED_VALUE if is_compensation_field
                    else (str(old_value) if old_value is not None else None)
                ),
                new_value=(
                    StaffContractAuditLog.REDACTED_VALUE if is_compensation_field
                    else (str(new_value) if new_value is not None else None)
                ),
                changed_by=actor,
            )
        return updated

    def _leave_summary(self, obj):
        # Computed once per Staff object per serializer pass (list views call each
        # SerializerMethodField separately per row otherwise) via LOT RH 2's
        # centralized service -- same current-calendar-year period, same
        # cross-year-intersection and config-aware logic as the leave request
        # submission check and the Staff export below.
        cache = self.__dict__.setdefault("_leave_summary_cache", {})
        if obj.pk not in cache:
            year_start, year_end = current_year_bounds()
            cache[obj.pk] = get_employee_leave_summary(obj, year_start, year_end)
        return cache[obj.pk]

    @staticmethod
    def _as_api_number(value):
        # Internal arithmetic is Decimal (LOT RH 2); day counts are always whole
        # numbers while half-days remain out of scope, so the API keeps its
        # existing plain-integer shape for current consumers.
        return None if value is None else int(value)

    def get_leave_taken(self, obj):
        return self._as_api_number(self._leave_summary(obj).leave_taken)

    def get_leave_left(self, obj):
        return self._as_api_number(self._leave_summary(obj).leave_balance)

    def get_work_from_home_taken(self, obj):
        return self._as_api_number(self._leave_summary(obj).wfh_taken)

    def get_work_from_home_left(self, obj):
        return self._as_api_number(self._leave_summary(obj).wfh_balance)

    def get_location(self, obj):
        today = date.today()
        leave_today = obj.leave_requests.filter(start_date__lte=today, end_date__gte=today, status=StatusType.APPROVED.value).first()
        if leave_today:
            if leave_today.is_working_from_home:
                return "Remote"
            else:
                return "On Leave"
        else:
            return "Onsite"
        
    def get_project_allocation(self, obj):
        projects = Project.objects.filter(
            Q(user=obj.user) | Q(team_members__team_member=obj.user)
        ).values_list(flat=True)
        return projects                          

    def get_productivity_percentage(self, obj):
        # Get user creation date
        user_creation_date = obj.user.created_at.date()
        today = date.today()
        # Calculate total working days
        total_days = (today - user_creation_date).days
        total_weeks = total_days / 7
        # Take staff weekly hours default to 40
        total_working_hours = total_weeks * float(obj.weekly_hours or 40)
        # Get billed hours
        if obj.user.is_freelancer:
            total_billed_hours = (
                obj.user.time_cards.filter(status=StatusType.APPROVED.value)
                .aggregate(total_hours=Sum('hours'))
                .get('total_hours') or 0
            )
        else:
            total_billed_hours = (
                obj.user.created_time_cards.filter(status=StatusType.APPROVED.value)
                .aggregate(total_hours=Sum('hours'))
                .get('total_hours') or 0
            )
        if total_working_hours > 0:
            productivity_percentage = round((total_billed_hours / total_working_hours) * 100, 2)
            return productivity_percentage
        return 0

    def get_total_allocated_hours(self, obj):
        action_items = obj.user.multi_assigned_action_items.all()
        total_allocated_hours = action_items.aggregate(total_hours=Sum('allocated_time'))['total_hours'] or 0
        return total_allocated_hours

    def get_total_spent_hours(self, obj):
        user_time_cards = obj.user.created_time_cards.filter(status=StatusType.APPROVED.value)
        total_spent_hours = user_time_cards.aggregate(total_hours=Sum('hours'))['total_hours'] or 0
        return total_spent_hours

    def to_representation(self, instance):
        """Limit field visibility based on role."""
        data = super().to_representation(instance)
        user = self.context.get("request").user
        data["observation_count"] = instance.staff_observations.count()

        # LOT RH12 follow-up: "weekly_hours" always reflects the historically
        # resolved as-of-today StaffQuotiteHistory entry when one exists --
        # never the raw stored value, which otherwise only updates when
        # something re-saves it (see resolve_current_quotite_entry). This is
        # what makes "situation A now, B scheduled next month" become B on
        # its own, on the next read, once that date arrives -- no scheduled
        # job, no new planning infrastructure.
        if "weekly_hours" in data:
            from human_resource_management.services.leave_accrual import (
                resolve_current_quotite_entry,
            )
            current_entry = resolve_current_quotite_entry(instance)
            if current_entry is not None:
                data["weekly_hours"] = float(current_entry.contractual_hours)
        
        # Check if user has staff management permissions (treated as admin)
        has_staff_management_permissions = require_any_permission_multiple_modules(
            user,
            {'staff_management': ['view', 'full_access', 'update']},
            'company',
            {}
        )

        # L00-A (DEC-02/06): compensation is stripped INDEPENDENTLY of the
        # has_staff_management_permissions/self-view branch below -- the
        # bare general Staff Management right above (or even reaching this
        # point via "it's my own profile") must NEVER by itself leak
        # salary/hourly_rate. can_view_staff_compensation already covers
        # self-view, an explicit view_compensation grant, and the canonical
        # Company Admin -- nothing else.
        if not can_view_staff_compensation(user, instance):
            data.pop("salary", None)
            data.pop("hourly_rate", None)
            data.pop("compensation_situation", None)
            data.pop("employer_cost_estimate", None)

        # Admin, Supervisor, or the user viewing their own profile gets full visibility
        if has_staff_management_permissions or user == instance.user:
            return data
        # Regular Users get limited fields
        limited_fields = [
            "name",
            "user_id",
            "id",
            "dob",
            "employment_status",
            "leave_taken",
            "leave_left",
            "work_from_home_taken",
            "work_from_home_left",
            "location",
            "project_allocation",
            "observation_count",
            # L00-A: plain capability booleans, never a value themselves --
            # always exposed so the frontend can consistently rely on them
            # regardless of which visibility tier a given viewer/record
            # falls into.
            "can_view_compensation",
            "can_update_compensation",
        ]
        return {key: data[key] for key in limited_fields if key in data}

class StaffObservationSerializer(serializers.ModelSerializer):
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = StaffObservation
        fields = "__all__"
        read_only_fields = ["company_profile", "created_by", "created_at", "updated_at"]

    def create(self, validated_data):
        request_user = self.context['request'].user
        
        # Check if user has staff observation permissions
        has_staff_observation_permissions = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['create', 'full_access']}, 
            'company', 
            {}
        )
        
        if not has_staff_observation_permissions:
            raise PermissionDenied("You do not have permission to create a staff observation.")
        
        validated_data["created_by"] = request_user
        validated_data["company_profile"] = request_user.company  # Assuming user has a company attribute
        
        return super().create(validated_data)

    def update(self, instance, validated_data):
        request_user = self.context['request'].user
        
        # Check if user has staff observation permissions
        has_staff_observation_full_access = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['full_access']}, 
            'company', 
            {}
        )
        
        has_staff_observation_update = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['update']}, 
            'company', 
            {}
        )
        
        has_staff_observation_update_created = require_any_permission_multiple_modules(
            request_user, 
            {'staff_observation': ['update_created']}, 
            'company', 
            {}
        )
        
        # Users with full access can update any observation in their company
        if has_staff_observation_full_access or  has_staff_observation_update and instance.company_profile == request_user.company:
            return super().update(instance, validated_data)

        
        # Users with update_created permissions can only update their own observations
        elif has_staff_observation_update_created and instance.created_by == request_user:
            return super().update(instance, validated_data)
        
        raise PermissionDenied("You do not have permission to update this staff observation.")


class StaffConfigurationSerializer(serializers.ModelSerializer):
    company_profile = serializers.PrimaryKeyRelatedField(read_only=True)
    freelancer = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = StaffConfiguration
        fields = "__all__"
