import logging
import posixpath

from rest_framework import viewsets, filters, status
from django_filters.rest_framework import DjangoFilterBackend
import django_filters
from django_filters import rest_framework as drf_filters
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.auth.models import AnonymousUser
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.utils.dateparse import parse_date
from django.utils import timezone
from project_management.filters import CharInFilter, NumberInFilter
from human_resource_management.serializers.leave import (
    LeaveRequestBatchSerializer,
    LeaveRequestManagerViewSerializer,
    LeaveRequestSerializer,
    serialize_leave_attachment,
    serialize_leave_for_calendar,
    serialize_manager_opinion,
)
from human_resource_management.models import LeaveRequest
from human_resource_management.models.leave import LeaveRequestAttachment, LeaveManagerOpinion
from models.models import Staff
from ecpmpapp.constants import StatusType
from human_resource_management.permissions import (
    LeaveReportingPermission,
    LeaveRequestPermission,
    has_cumulative_leave_review_permission,
    has_leave_manager_opinion_permission,
    is_hr_reviewer_for,
    is_team_manager_for,
    resolve_authorized_solution_for_request,
)

from ecpmpapp.utils import require_any_permission_multiple_modules
from ecpmpapp.ordering import TableOrderingFilter
from human_resource_management.views.leave_type import _company_configs_for_request
from human_resource_management.services.absence_reporting import (
    get_approved_absence_recap,
    get_detailed_leave_rows,
    resolve_scoped_employees,
)
from human_resource_management.services.leave_balance import (
    current_year_bounds,
    get_employee_leave_summary,
)
from human_resource_management.services.leave_export import (
    build_absence_recap_pdf,
    build_absence_recap_xlsx,
    build_detailed_leave_xlsx,
)
from human_resource_management.services.leave_entitlement import (
    get_entitlement_breakdown,
    is_entitlement_mode_active_for_employee,
)
from human_resource_management.models.leave_entitlement import LeaveEntitlement

from django.db.models.functions import Concat
from django.db.models import Value, CharField, F

#TODO: Automatically adds sick leave entries to the office calendar.
#TODO: Approved leave is displayed on the office calendar with color-coded status.
#TODO: Declined leave is removed from the calendar

class LeaveRequestFilter(django_filters.FilterSet):
    status = CharInFilter(field_name='status', lookup_expr='in')
    type = CharInFilter(field_name='type', lookup_expr='in')
    employee = NumberInFilter(field_name='employee', lookup_expr='in')
    employee_user = NumberInFilter(field_name='employee__user', lookup_expr='in')
    start_date = drf_filters.DateFromToRangeFilter(field_name='start_date', label='Start Date Range')
    end_date = drf_filters.DateFromToRangeFilter(field_name='end_date', label='End Date Range')
    date_from = drf_filters.DateFilter(field_name='start_date', lookup_expr='gte')
    date_to = drf_filters.DateFilter(field_name='end_date', lookup_expr='lte')

    class Meta:
        model = LeaveRequest
        fields = ["status", "type", "employee", "employee_user", "start_date", "end_date", "date_from", "date_to"]

logger = logging.getLogger(__name__)


class LeaveRequestViewSet(viewsets.ModelViewSet):
    serializer_class = LeaveRequestSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend, TableOrderingFilter]
    filterset_class = LeaveRequestFilter
    ordering_fields = [
        "id",
        "employee",
        "employee__user__email",
        "employee__user__first_name",
        "employee__user__last_name",
        "employee_full_name",
        "type",
        "start_date",
        "end_date",
        "is_working_from_home",
        "status",
        "reason",
        "created_at",
    ]
    ordering = ["-id"]
    ordering_text_fields = {"employee_full_name", "type", "status", "reason"}
    ordering_natural_fields = {"employee_full_name", "reason"}

    def get_permissions(self):
        if self.action in ['list', 'retrieve', 'calendar']:
            return [LeaveRequestPermission(action='view', scope='company')]
        elif self.action in ['create', 'batch']:
            return [LeaveRequestPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [LeaveRequestPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [LeaveRequestPermission(action='delete', scope='company')]
        elif self.action in [
            'export',
            'absence_recap',
            'absence_recap_export_xlsx',
            'absence_recap_export_pdf',
        ]:
            # LOT RH 8: detailed export + approved-absence recap are reserved
            # to real HR reviewers (has_cumulative_leave_review_permission),
            # never the generic 'view'/'view_created' crowd that the rest of
            # this viewset's `view` action already allows through.
            return [LeaveReportingPermission()]
        return super().get_permissions()

    def get_serializer_class(self):
        # LOT RH 5: a pure team-manager viewer (holds leave_request.
        # manager_opinion but none of full_access/view/view_created/review)
        # ALWAYS gets the confidentiality-restricted serializer, regardless
        # of query params -- so a retrieve call without ?team_view=true
        # can't accidentally return the full representation to them either.
        user = getattr(self.request, 'user', None)
        if (
            user is not None
            and not isinstance(user, AnonymousUser)
            and has_leave_manager_opinion_permission(user)
            and not has_cumulative_leave_review_permission(user)
            and not require_any_permission_multiple_modules(
                user,
                {'leave_request': ['full_access', 'view', 'view_created']},
                'company',
                self.request.query_params,
            )
        ):
            return LeaveRequestManagerViewSerializer
        return super().get_serializer_class()

    def get_queryset(self):
        """
        Filter leave requests based on user permissions.
        The permission class handles the permission checks, this method handles the data filtering.
        """

        user = self.request.user
        if isinstance(user, AnonymousUser):
            return LeaveRequest.objects.none()

        is_review_queue = str(
            self.request.query_params.get('review_queue', '')
        ).lower() in {'true', '1'}
        is_team_view = str(
            self.request.query_params.get('team_view', '')
        ).lower() in {'true', '1'}

        if is_team_view:
            # LOT RH 5: NEVER company-wide -- strictly `Staff.supervisor`'s
            # direct reports, the one genuine, already-relied-upon reporting
            # line in this codebase (Time Card/Expense Card approval routing
            # already uses it the same way).
            if not has_leave_manager_opinion_permission(user) or user.is_freelancer:
                return LeaveRequest.objects.none()
            staff = getattr(user, 'staff', None)
            if staff is None:
                return LeaveRequest.objects.none()
            queryset = LeaveRequest.objects.filter(employee__supervisor=staff).exclude(
                leave_type__is_sst_managed=True
            )
            return self._annotate_employee_name(queryset.distinct())

        if is_review_queue:
            has_leave_request_review = has_cumulative_leave_review_permission(user)
            if (
                not has_leave_request_review
                or user.is_freelancer
                or not user.company_id
            ):
                return LeaveRequest.objects.none()

            queryset = LeaveRequest.objects.filter(
                employee__user__company_id=user.company_id,
            ).exclude(leave_type__is_sst_managed=True)
            return self._annotate_employee_name(queryset.distinct())

        # Check permissions using the same logic as the permission class
        has_leave_request_full_access = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['full_access']}, 
            'company', 
            self.request.query_params
        )

        has_leave_request_view = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['view']}, 
            'company', 
            self.request.query_params
        )

        has_leave_request_view_created = require_any_permission_multiple_modules(
            user, 
            {'leave_request': ['view_created']}, 
            'company', 
            self.request.query_params
        )

        has_leave_request_review = has_cumulative_leave_review_permission(user)

        query_filters = Q()
        # Build queryset based on permissions
        if (
            has_leave_request_full_access
            or has_leave_request_view
            or has_leave_request_review
        ):
            # Users with full_access or view permissions see all leave requests
            if user.is_freelancer:
                # For freelancers, we need to get their staff profile
                try:
                    staff_profile = user.staff
                    query_filters = Q(employee=staff_profile)
                except Staff.DoesNotExist:
                    return LeaveRequest.objects.none()
            else:
                query_filters = Q(employee__user__company=user.company)
        elif has_leave_request_view_created:
            # Users with view_created permissions see only their own leave requests
            staff_profile = user.staff
            query_filters = Q(employee=staff_profile)
        else:
            # No permissions, return empty queryset
            return LeaveRequest.objects.none()

        # LOT L04 (SST): a dedicated, standalone module -- excluded here, not
        # selectively hidden per-viewer, so the classic endpoint never carries
        # an SST row down any path (list, retrieve by id, calendar, export,
        # review queue, team view above). SST has its own dedicated views.
        queryset = LeaveRequest.objects.filter(query_filters).exclude(
            leave_type__is_sst_managed=True
        ).distinct()

        return self._annotate_employee_name(queryset)

    @staticmethod
    def _annotate_employee_name(queryset):
        return queryset.annotate(
            employee_full_name=Concat(
                F('employee__user__first_name'),
                Value(' '),
                F('employee__user__last_name'),
                output_field=CharField()
            )
        )

    @action(detail=False, methods=["post"], url_path="batch")
    def batch(self, request):
        serializer = LeaveRequestBatchSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        created = serializer.save()
        output = LeaveRequestSerializer(created, many=True, context={"request": request})
        return Response(output.data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["get"], url_path="calendar")
    def calendar(self, request):
        """LOT RH 6: this calendar payload is intentionally narrower than the
        main leave-request representation for EVERY viewer, HR included --
        reason/attachments never appear here regardless of role (they stay
        in the secured leave-request detail). Only `status=approved` rows
        are ever returned: a pending request is never shown as a confirmed
        unavailability. On top of that, a pure team manager (holds
        `leave_request.manager_opinion` but none of full_access/view/
        view_created/review) gets `type` stripped too -- the same
        confidentiality boundary `LeaveRequestManagerViewSerializer` already
        enforces for list/retrieve, applied here by hand since this action
        builds its own response dict rather than going through DRF's
        serializer_class pipeline."""
        range_start_value = request.query_params.get("range_start")
        range_end_value = request.query_params.get("range_end")
        range_start = parse_date(range_start_value or "")
        range_end = parse_date(range_end_value or "")
        if not range_start or not range_end:
            return Response(
                {"range": "Valid range_start and range_end dates are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if range_end <= range_start or (range_end - range_start).days > 62:
            return Response(
                {"range": "The date range must be positive and at most 62 days."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = (
            self.get_queryset()
            .filter(status=StatusType.APPROVED.value)
            .filter(start_date__lt=range_end, end_date__gte=range_start)
            .select_related("employee__user")
            .order_by("start_date", "id")
        )
        user = request.user
        is_full_viewer = has_cumulative_leave_review_permission(user) or require_any_permission_multiple_modules(
            user, {'leave_request': ['full_access', 'view', 'view_created']}, 'company', request.query_params,
        )
        return Response([
            serialize_leave_for_calendar(leave, include_type=is_full_viewer)
            for leave in queryset
        ])

    @staticmethod
    def _parse_period(request):
        """Required start_date/end_date query params for every reporting
        action (LOT RH 8) -- unlike `calendar`'s range, there is no implicit
        default here: a reporting export always needs an explicit, bounded
        period. Returns (start, end, error_response_or_None)."""
        start_raw = request.query_params.get("start_date")
        end_raw = request.query_params.get("end_date")
        start = parse_date(start_raw) if start_raw else None
        end = parse_date(end_raw) if end_raw else None
        if not start or not end:
            return None, None, Response(
                {"period": "Valid start_date and end_date are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if start > end:
            return None, None, Response(
                {"period": "start_date must not be after end_date."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return start, end, None

    @staticmethod
    def _parse_employee_ids(request):
        """`employee` query param: comma-separated ids, or omitted/"all" for
        every authorized collaborator of the company. Returns
        (ids_or_None, error_response_or_None) -- None means "all"."""
        raw = request.query_params.get("employee")
        if not raw or raw.strip().lower() == "all":
            return None, None
        ids = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.lstrip("-").isdigit():
                return None, Response(
                    {"employee": f"Invalid employee id: {part}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ids.append(int(part))
        if not ids:
            return None, None
        return ids, None

    def _scoped_staff_or_error(self, request, employee_ids):
        staff_queryset, unknown_ids = resolve_scoped_employees(
            request.user.company_id, employee_ids
        )
        if unknown_ids:
            return None, Response(
                {"employee": f"Unknown or out-of-scope employee id(s): {unknown_ids}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return staff_queryset, None

    @action(detail=False, methods=["get"], url_path="export")
    def export(self, request):
        """LOT RH 8: detailed XLSX export for Time Booking > Leaves. Exports
        every matching row for the filtered period/collaborators/type/status
        -- never only the displayed page. A request straddling the period
        bounds only contributes the portion inside them (duration/deductible
        both clipped via compute_leave_request_days, through
        get_detailed_leave_rows). Never includes `reason` or attachments."""
        period_start, period_end, error = self._parse_period(request)
        if error:
            return error
        employee_ids, error = self._parse_employee_ids(request)
        if error:
            return error
        staff_queryset, error = self._scoped_staff_or_error(request, employee_ids)
        if error:
            return error

        queryset = LeaveRequest.objects.filter(
            employee__in=staff_queryset,
            start_date__lte=period_end,
            end_date__gte=period_start,
        ).exclude(leave_type__is_sst_managed=True)  # LOT L04 (SST): never in the generic Leaves export
        # LOT RH13 follow-up (solution isolation): a legacy row with no
        # stable `leave_type` FK can never be attributed to a solution and
        # is always kept (same precedent as leave_entitlement_activation.py)
        # -- only a row whose leave_type belongs to ANOTHER solution's
        # CompanyConfig is excluded.
        queryset = queryset.filter(
            Q(leave_type__isnull=True)
            | Q(leave_type__company_config__in=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ))
        )
        type_param = request.query_params.get("type")
        if type_param:
            types = [t.strip() for t in type_param.split(",") if t.strip()]
            if types:
                queryset = queryset.filter(type__in=types)
        status_param = request.query_params.get("status")
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            if statuses:
                queryset = queryset.filter(status__in=statuses)

        rows = get_detailed_leave_rows(
            queryset, period_start, period_end,
            company_profile_id=request.user.company_id,
        )
        company_name = getattr(request.user.company, "company_name", "") or ""
        return build_detailed_leave_xlsx(
            rows, company_name=company_name,
            period_start=period_start, period_end=period_end,
        )

    def _build_absence_recap_data(self, request):
        """Shared by the JSON view and both exports, so the three always
        show the exact same numbers (LOT RH 8's own requirement)."""
        period_start, period_end, error = self._parse_period(request)
        if error:
            return None, error
        employee_ids, error = self._parse_employee_ids(request)
        if error:
            return None, error
        staff_queryset, error = self._scoped_staff_or_error(request, employee_ids)
        if error:
            return None, error

        company_id = request.user.company_id
        rows = get_approved_absence_recap(
            staff_queryset, period_start, period_end,
            company_profile_id=company_id,
            allowed_leave_type_configs=_company_configs_for_request(
                request, product_name=resolve_authorized_solution_for_request(request)
            ),
        )

        # Optional, clearly-separated global-balance reference block: the
        # EXISTING current-calendar-year balance (never a per-type balance,
        # never implied to be "as of the end of the filtered period" -- the
        # lot explicitly forbids both). Cheap to attach since it reuses
        # get_employee_leave_summary as-is.
        year_start, year_end = current_year_bounds()
        as_of = timezone.now().date().isoformat()
        today = timezone.now().date()
        staff_by_id = {s.id: s for s in staff_queryset.filter(
            id__in=[r.employee_id for r in rows]
        )}
        for row in rows:
            staff = staff_by_id.get(row.employee_id)
            if staff is None:
                continue
            annual_summary = get_employee_leave_summary(staff, year_start, year_end)
            row.global_balance = {
                "leave_balance": annual_summary.leave_balance,
                "wfh_balance": annual_summary.wfh_balance,
                "year": year_start.year,
                "as_of": as_of,
            }

            # LOT RH 9, section 6: per-type balances (current cycle, as-of
            # TODAY -- never tied to the filtered period) once the company
            # has activated the new entitlement mechanism. Kept entirely
            # separate from `days_by_type` (duration on the filtered period)
            # above -- never conflated with it.
            row.entitlement_balances = None
            if is_entitlement_mode_active_for_employee(staff):
                # LOT RH13 follow-up (solution isolation): a company
                # configured for more than one solution must never have the
                # recap's per-type balances include another solution's
                # entitlements -- scoped to the ACTIVE solution's own
                # CompanyConfig(s), same mechanism as every other RH screen.
                current_entitlements = LeaveEntitlement.objects.filter(
                    employee=staff,
                    cycle_period__period_start__lte=today,
                    cycle_period__period_end__gte=today,
                    leave_type__company_config__in=_company_configs_for_request(
                        request, product_name=resolve_authorized_solution_for_request(request)
                    ),
                ).select_related("leave_type", "cycle_period")
                balances = []
                for entitlement in current_entitlements:
                    cycle = entitlement.cycle_period
                    breakdown = get_entitlement_breakdown(
                        staff, entitlement.leave_type, cycle.period_start, cycle.period_end,
                    )
                    portion = breakdown[0] if breakdown else None
                    # LOT RH10: `quota` keeps its historical meaning here for quota-mode
                    # rows; for a monthly-accrual row it would otherwise show the
                    # repurposed "adjustment" value (usually 0.00) where a reader expects
                    # a balance figure, so `acquired` is surfaced alongside it instead.
                    balances.append({
                        "leave_type_id": entitlement.leave_type_id,
                        "leave_type_name": entitlement.leave_type.name,
                        "cycle_start": cycle.period_start.isoformat(),
                        "cycle_end": cycle.period_end.isoformat(),
                        "mode": entitlement.mode,
                        "quota": float(entitlement.quota),
                        "acquired": float(portion.acquired) if portion and portion.acquired is not None else None,
                        "consumed": float(portion.consumed) if portion else None,
                        "reserved": float(portion.reserved) if portion else None,
                        "available": (
                            float(portion.available)
                            if portion and portion.available is not None else None
                        ),
                        "as_of": today.isoformat(),
                    })
                row.entitlement_balances = balances

        return {
            "period_start": period_start,
            "period_end": period_end,
            "company_name": getattr(request.user.company, "company_name", "") or "",
            "generated_at": timezone.now(),
            "rows": rows,
        }, None

    @action(detail=False, methods=["get"], url_path="absence-recap")
    def absence_recap(self, request):
        """LOT RH 8: one row per collaborator with >=1 APPROVED absence on
        the filtered period -- types taken (no duplicates), total absence
        days and a per-type day breakdown, all DURATION-based (never the
        balance-deduction quantity -- see absence_reporting.py). No per-type
        balance (deferred, entitlements per type/cycle are not defined)."""
        data, error = self._build_absence_recap_data(request)
        if error:
            return error
        return Response({
            "period_start": data["period_start"].isoformat(),
            "period_end": data["period_end"].isoformat(),
            "company_name": data["company_name"],
            "generated_at": data["generated_at"].isoformat(),
            "rows": [
                {
                    "employee_id": r.employee_id,
                    "employee_name": r.employee_name,
                    "types": r.types,
                    "total_days": float(r.total_days),
                    "days_by_type": {k: float(v) for k, v in r.days_by_type.items()},
                    "global_balance": (
                        {
                            "leave_balance": (
                                float(r.global_balance["leave_balance"])
                                if r.global_balance["leave_balance"] is not None else None
                            ),
                            "wfh_balance": (
                                float(r.global_balance["wfh_balance"])
                                if r.global_balance["wfh_balance"] is not None else None
                            ),
                            "year": r.global_balance["year"],
                            "as_of": r.global_balance["as_of"],
                        }
                        if r.global_balance else None
                    ),
                    "entitlement_balances": r.entitlement_balances,
                }
                for r in data["rows"]
            ],
        })

    @action(detail=False, methods=["get"], url_path="absence-recap-export-xlsx")
    def absence_recap_export_xlsx(self, request):
        data, error = self._build_absence_recap_data(request)
        if error:
            return error
        return build_absence_recap_xlsx(data)

    @action(detail=False, methods=["get"], url_path="absence-recap-export-pdf")
    def absence_recap_export_pdf(self, request):
        data, error = self._build_absence_recap_data(request)
        if error:
            return error
        return build_absence_recap_pdf(data)

    def get_object(self):
        # LOT L04 (SST): this bypasses get_queryset() entirely (a direct
        # lookup, pre-existing before this lot) -- its own SST exclusion
        # must be repeated here, or a direct retrieve-by-id would still
        # reach an SST row that check_object_permissions alone (a company/
        # action-level check, not row-specific) would never catch.
        obj = get_object_or_404(LeaveRequest.objects.exclude(leave_type__is_sst_managed=True), pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        return obj
    
    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != StatusType.PENDING.value:
            raise PermissionDenied("Only pending leave requests can be deleted.")
        if instance.employee != request.user.staff:
            raise PermissionDenied("You are not allowed to delete this leave request.")
        # LOT RH13: checked BEFORE the physical delete below (the row, and
        # its dates/leave_type, must still exist to resolve what it could
        # affect) -- a side effect only, never allowed to break deletion
        # itself. In practice a PENDING request never yet affected any
        # posted credit (only APPROVED absences are ever considered), so
        # this essentially never produces a real signal -- the hook exists
        # for completeness/robustness, not because it is expected to fire.
        from human_resource_management.services.leave_absence_signal import (
            check_late_decision_signal,
        )
        try:
            check_late_decision_signal(instance, event_type="deleted", actor=request.user)
        except Exception:
            logger.exception("RH13 late-decision signal check failed for deleted request %s", instance.pk)
        # LOT RH11: same per-employee lock every other balance-affecting
        # write already takes -- finalize any overdue carry-over (freezing
        # the snapshot as of THIS instant, still counting this request,
        # before it's removed) and refuse outright if its cycle is
        # already closed, BEFORE the deletion itself.
        if is_entitlement_mode_active_for_employee(instance.employee) and instance.leave_type_id:
            from human_resource_management.services.leave_carryover import (
                finalize_due_carryovers, raise_if_cycle_closed,
            )
            with transaction.atomic():
                Staff.objects.select_for_update().get(pk=instance.employee_id)
                entitlement = LeaveEntitlement.objects.filter(
                    employee=instance.employee, leave_type=instance.leave_type,
                    cycle_period__period_start__lte=instance.start_date,
                    cycle_period__period_end__gte=instance.start_date,
                ).first()
                if entitlement is not None:
                    finalize_due_carryovers(entitlement)
                    raise_if_cycle_closed(entitlement)
                instance.delete()
        else:
            instance.delete()
        return Response({"detail": "Leave request deleted successfully."}, status=status.HTTP_204_NO_CONTENT)


def _can_view_leave_attachments(user, leave_request):
    return user.id == leave_request.employee.user_id or is_hr_reviewer_for(user, leave_request.employee)


class LeaveRequestAttachmentViewSet(viewsets.GenericViewSet):
    """Supporting-document attachments for a LeaveRequest (LOT RH 4).

    Deliberately its own small model/viewset, never a Document/
    DocumentAttachment: those are reachable through the generic Company
    Documents permission module (broadly held by company roles) and their
    serializers return a raw, unsigned, non-expiring storage URL directly in
    list/retrieve JSON -- there is no way to make that private without
    changing shared, heavily-depended-on code far outside this lot's scope.
    Authorization is done explicitly per request (same convention as
    CorrespondenceAttachmentViewSet/DocumentAttachmentViewSet) rather than
    through DRF's object-permission plumbing, since the parent LeaveRequest
    must be resolved and scope-checked before anything else -- and unlike
    those two precedents, the serialized output here never includes the raw
    file URL, only a link back to this view's own gated `download` action.
    """

    filter_backends = []
    parser_classes = [MultiPartParser, FormParser]

    def _leave_request(self, leave_request_id):
        leave_request = (
            LeaveRequest.objects.select_related("employee__user")
            .filter(pk=leave_request_id)
            .first()
        )
        if leave_request is None:
            raise Http404
        return leave_request

    @staticmethod
    def _attachment(leave_request, attachment_id):
        attachment = LeaveRequestAttachment.objects.filter(
            leave_request=leave_request, pk=attachment_id
        ).first()
        if attachment is None:
            raise Http404
        return attachment

    def list(self, request, leave_request_id=None, *args, **kwargs):
        leave_request = self._leave_request(leave_request_id)
        if not _can_view_leave_attachments(request.user, leave_request):
            raise DRFPermissionDenied()
        attachments = leave_request.attachments.select_related("uploaded_by")
        results = [serialize_leave_attachment(a, request) for a in attachments]
        return Response({"count": len(results), "results": results})

    def create(self, request, leave_request_id=None, *args, **kwargs):
        leave_request = self._leave_request(leave_request_id)
        is_owner = request.user.id == leave_request.employee.user_id
        # The collaborator may always add (at submission or later); HR may
        # only add AFTER approval (consulting/complementing the record, never
        # a path to approve first and furnish evidence afterwards).
        allowed = is_owner or (
            leave_request.status == StatusType.APPROVED.value
            and is_hr_reviewer_for(request.user, leave_request.employee)
        )
        if not allowed:
            raise DRFPermissionDenied()
        uploaded = request.FILES.get("file")
        if uploaded is None:
            return Response({"file": "A file is required."}, status=status.HTTP_400_BAD_REQUEST)
        attachment = LeaveRequestAttachment(
            leave_request=leave_request,
            file_name=posixpath.basename(uploaded.name or "") or "attachment",
            file_size=uploaded.size,
            uploaded_by=request.user,
        )
        attachment.file = uploaded
        attachment.save()
        return Response(
            serialize_leave_attachment(attachment, request), status=status.HTTP_201_CREATED
        )

    def download(self, request, leave_request_id=None, attachment_id=None, *args, **kwargs):
        leave_request = self._leave_request(leave_request_id)
        if not _can_view_leave_attachments(request.user, leave_request):
            raise DRFPermissionDenied()
        attachment = self._attachment(leave_request, attachment_id)
        if not attachment.file:
            raise Http404
        return FileResponse(
            attachment.file.open("rb"), as_attachment=True, filename=attachment.file_name,
        )

    def destroy(self, request, leave_request_id=None, attachment_id=None, *args, **kwargs):
        leave_request = self._leave_request(leave_request_id)
        is_owner = request.user.id == leave_request.employee.user_id
        is_reviewer = is_hr_reviewer_for(request.user, leave_request.employee)
        if not (is_owner or is_reviewer):
            raise DRFPermissionDenied()
        with transaction.atomic():
            # Same stable per-employee lock the approval path takes before
            # recomputing whether a required document is present (serializers.
            # leave.LeaveRequestSerializer.update) -- the two are therefore
            # mutually exclusive for the same employee: a concurrent delete of
            # the last required attachment can never slip in between an
            # approval's own check and its commit.
            Staff.objects.select_for_update().get(pk=leave_request.employee_id)
            leave_request.refresh_from_db()
            if leave_request.status == StatusType.APPROVED.value:
                return Response(
                    {"file": "Attachments cannot be removed once this leave request has been approved."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            attachment = self._attachment(leave_request, attachment_id)
            stored = attachment.file
            attachment.delete()
            if stored:
                transaction.on_commit(lambda: stored.storage.delete(stored.name))
        return Response(status=status.HTTP_204_NO_CONTENT)


class LeaveManagerOpinionViewSet(viewsets.GenericViewSet):
    """A team manager's non-binding operational opinion on a direct report's
    leave request (LOT RH 5). Deliberately its own small viewset, same
    convention as LeaveRequestAttachmentViewSet: authorization is done
    explicitly per request rather than through DRF's object-permission
    plumbing, since the parent LeaveRequest must be resolved first and the
    team-membership check (`Staff.supervisor`) is specific to this feature."""

    filter_backends = []

    def _leave_request(self, leave_request_id):
        leave_request = (
            LeaveRequest.objects.select_related("employee__user", "employee__supervisor__user")
            .filter(pk=leave_request_id)
            .first()
        )
        if leave_request is None:
            raise Http404
        return leave_request

    def create(self, request, leave_request_id=None, *args, **kwargs):
        leave_request = self._leave_request(leave_request_id)
        if not is_team_manager_for(request.user, leave_request.employee):
            raise DRFPermissionDenied()

        opinion_value = request.data.get("opinion")
        if opinion_value not in {LeaveManagerOpinion.OPINION_FAVORABLE, LeaveManagerOpinion.OPINION_UNFAVORABLE}:
            return Response(
                {"opinion": "Must be 'favorable' or 'unfavorable'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        comment = request.data.get("comment") or ""

        with transaction.atomic():
            # Same stable per-employee lock the approval and attachment-
            # deletion paths already take -- re-checked here under the lock
            # so a concurrent HR decision can never let a late opinion land
            # on a request that was just approved/rejected.
            Staff.objects.select_for_update().get(pk=leave_request.employee_id)
            leave_request.refresh_from_db()
            # Re-verify team membership and permission under the lock too --
            # a concurrent change to the reporting line or the permission
            # itself must not let a stale check through.
            if not is_team_manager_for(request.user, leave_request.employee):
                raise DRFPermissionDenied()
            if leave_request.status != StatusType.PENDING.value:
                return Response(
                    {"detail": "This request has already been decided; no new opinion can be recorded."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            opinion, _ = LeaveManagerOpinion.objects.update_or_create(
                leave_request=leave_request,
                defaults={
                    "opinion": opinion_value,
                    "comment": comment,
                    "given_by": request.user,
                },
            )
        return Response(serialize_manager_opinion(opinion), status=status.HTTP_201_CREATED)
