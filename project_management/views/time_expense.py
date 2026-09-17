from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from rest_framework.response import Response
from rest_framework import viewsets, filters, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import ValidationError
from django.contrib.auth.models import AnonymousUser
from django_filters.rest_framework import DjangoFilterBackend
from django.db import transaction
from django.db.models import Q, OuterRef, Subquery, Count, IntegerField, Value, Case, When, TextField, CharField
from django.db.models.functions import Concat
from django.shortcuts import get_object_or_404
from django.core.exceptions import PermissionDenied
from django.contrib.contenttypes.models import ContentType
from drf_yasg.utils import swagger_auto_schema
from drf_yasg import openapi
from django.conf import settings
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_date

from project_management.models import Project, ProjectTime, TimeCard, ExpenseCard, InvoiceLineItem, PurchaseOrder, TimesheetActivity, TimesheetEntry, MonthlyTimesheetSubmission, MonthlyTimesheetFeedback
from project_management.serializers import ProjectTimeSerializer, TimeCardSerializer, ExpenseCardSerializer, TimesheetWeekSaveSerializer, TimesheetWeekReadSerializer, TimesheetActivitySerializer, TimesheetActivityDraftCreateSerializer, TimesheetActivityPayloadSerializer, TimesheetCopyPreviousWeekSerializer, TimesheetPlannedActivitiesSerializer, MonthlyTimesheetSubmissionSerializer, MonthlyTimesheetPreviewSerializer, MonthlyTimesheetPreviewRequestSerializer, MonthlyTimesheetSubmissionActionSerializer, MonthlyTimesheetReviewSerializer, MonthlyTimesheetActivityDocumentsSerializer
from project_management.serializers.time_expense import _get_week_locked_days, _get_week_edit_locked_days, _build_daily_warning_metadata, _build_monthly_timesheet_preview, _month_period_bounds, _get_eligible_timesheet_purchase_orders_queryset, _serialize_monthly_timesheet_documents, _user_can_access_project, _user_is_project_team_member, get_related_objects_from_action_items
from project_management.utils import get_product_name_from_request, send_approval_email
from project_management.permissions import TimeBookingPermission, ExpenseBookingPermission, ProjectPermission, PurchaseOrderPermission
from ecpmpapp.utils import get_frontend_url_for_solution, get_user_active_solution, require_any_permission_multiple_modules, create_notification_with_recipients, format_notification_message, get_actor_name
from ecpmpapp.constants import UserType, StatusType
from ecpmpapp.ordering import TableOrderingFilter
from project_management.filters import TimeCardFilter, ExpenseCardFilter
from project_management.currency_converter import ECBCurrencyConverter
from project_management.pagination import CustomPageNumberPagination
from models.models import User
import logging

logger = logging.getLogger(__name__)


def _annotate_visible_purchase_order_count(queryset, user):
    permission = PurchaseOrderPermission(action="view", scope="company")
    view_scope = permission.get_view_scope(user)
    if view_scope == "none":
        return queryset.annotate(
            po_count=Value(0, output_field=IntegerField())
        )

    visibility_filter = Q()
    created_filter = Q(project__purchase_orders__created_by=user) | Q(
        project__purchase_orders__document_type=(
            PurchaseOrder.DocumentType.CHANGE_NOTICE
        ),
        project__purchase_orders__source_po__created_by=user,
    )
    project_filter = Q(project__team_members__team_member=user)
    if view_scope == "created":
        visibility_filter = created_filter
    elif view_scope == "project":
        visibility_filter = project_filter
    elif view_scope == "created_or_project":
        visibility_filter = created_filter | project_filter
    return queryset.annotate(
        po_count=Count(
            "project__purchase_orders",
            filter=visibility_filter,
            distinct=True,
        )
    )


class TimesheetWeekViewSet(viewsets.GenericViewSet):
    serializer_class = TimesheetWeekReadSerializer

    def _get_related_month_filter(self, week_start_date, week_end_date):
        month_filter = Q(
            year=week_start_date.year,
            month=week_start_date.month,
        )
        if week_end_date.year != week_start_date.year or week_end_date.month != week_start_date.month:
            month_filter |= Q(
                year=week_end_date.year,
                month=week_end_date.month,
            )
        return month_filter

    def get_permissions(self):
        if self.action == "list":
            return [TimeBookingPermission(action="view", scope="company")]
        if self.action == "planned_activity_purchase_orders":
            return [
                PurchaseOrderPermission(
                    action="view",
                    scope="company",
                )
            ]
        if self.action in ["save", "activities", "copy_previous_week", "planned_activities"]:
            return [TimeBookingPermission(action="update", scope="company")]
        return super().get_permissions()

    def _get_week_start_date(self):
        requested_week_start = self.request.query_params.get("week_start")
        parsed_week_start = parse_date(requested_week_start) if requested_week_start else None
        if parsed_week_start:
            return parsed_week_start
        today = timezone.now().date()
        return today - timezone.timedelta(days=today.weekday())

    def _get_submission_summary(self, user, week_start_date, week_end_date):
        submission = (
            MonthlyTimesheetSubmission.objects.filter(
                self._get_related_month_filter(week_start_date, week_end_date),
                user=user,
            )
            .order_by("year", "month", "id")
            .first()
        )
        if not submission:
            return None
        return {
            "id": submission.id,
            "month": submission.month,
            "year": submission.year,
            "status": submission.status,
            "submitted_at": submission.submitted_at,
            "reviewed_at": submission.reviewed_at,
            "reviewed_by": submission.reviewed_by_id,
        }

    def _get_submission_summaries(self, user, week_start_date, week_end_date):
        submissions = (
            MonthlyTimesheetSubmission.objects.filter(
                self._get_related_month_filter(week_start_date, week_end_date),
                user=user,
            )
            .order_by("year", "month", "id")
        )
        return [
            {
                "id": submission.id,
                "month": submission.month,
                "year": submission.year,
                "status": submission.status,
                "submitted_at": submission.submitted_at,
                "reviewed_at": submission.reviewed_at,
                "reviewed_by": submission.reviewed_by_id,
            }
            for submission in submissions
        ]

    def _build_week_payload(self, user, week_start_date):
        week_end_date = week_start_date + timezone.timedelta(days=6)
        locked_days = _get_week_locked_days(user, week_start_date, week_end_date)
        edit_locked_days = _get_week_edit_locked_days(
            user,
            week_start_date,
            week_end_date,
        )
        activities = (
            TimesheetActivity.objects.filter(
                user=user,
                week_start_date=week_start_date,
                week_end_date=week_end_date,
            )
            .select_related(
                "company_action_item",
                "company_calendar_event",
                "project",
                "purchase_order",
                "project_milestone",
                "project_action_item",
            )
            .prefetch_related("entries")
            .order_by("id")
        )
        activity_data = TimesheetActivitySerializer(activities, many=True).data
        for activity in activity_data:
            activity["entries"] = [
                entry
                for entry in activity.get("entries", [])
                if entry["entry_date"] not in {
                    day.isoformat() for day in locked_days.keys()
                }
            ]

        daily_totals_map = {
            (week_start_date + timezone.timedelta(days=offset)): 0.0
            for offset in range(7)
        }
        for day, metadata in locked_days.items():
            daily_totals_map[day] = float(metadata["hours"] or 0)
        for activity in activities:
            for entry in activity.entries.all():
                if entry.entry_date in locked_days:
                    continue
                daily_totals_map[entry.entry_date] = (
                    daily_totals_map.get(entry.entry_date, 0.0) + float(entry.hours or 0)
                )

        daily_totals = [
            {
                "date": day.isoformat(),
                "hours": round(total, 2),
            }
            for day, total in daily_totals_map.items()
        ]
        week_total_hours = round(sum(item["hours"] for item in daily_totals), 2)
        daily_warning_metadata = _build_daily_warning_metadata(
            user,
            daily_totals_map,
        )

        return {
            "week_start_date": week_start_date,
            "week_end_date": week_end_date,
            "activities": activity_data,
            "daily_totals": daily_totals,
            "week_total_hours": week_total_hours,
            **daily_warning_metadata,
            "locked_dates": [day.isoformat() for day in edit_locked_days.keys()],
            "locked_day_details": list(edit_locked_days.values()),
            "can_edit": len(edit_locked_days) < 7,
            "submission": self._get_submission_summary(user, week_start_date, week_end_date),
            "submissions": self._get_submission_summaries(
                user,
                week_start_date,
                week_end_date,
            ),
        }

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "week_start",
                openapi.IN_QUERY,
                description="Start date of the week in YYYY-MM-DD format. Defaults to current week.",
                type=openapi.TYPE_STRING,
                required=False,
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        week_start_date = self._get_week_start_date()
        payload = self._build_week_payload(request.user, week_start_date)
        serializer = self.get_serializer(payload)
        return Response(serializer.data)

    @action(detail=False, methods=["put"], url_path="save", serializer_class=TimesheetWeekSaveSerializer)
    def save(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        save_result = serializer.save(user=request.user)
        payload = self._build_week_payload(request.user, save_result["week_start_date"])
        return Response(TimesheetWeekReadSerializer(payload).data)

    @action(detail=False, methods=["post"], url_path="activities", serializer_class=TimesheetActivityDraftCreateSerializer)
    def activities(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        payload = serializer.build_activity_payload()
        return Response(TimesheetActivityPayloadSerializer(payload).data)

    @action(detail=False, methods=["post"], url_path="copy-previous-week", serializer_class=TimesheetCopyPreviousWeekSerializer)
    def copy_previous_week(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        payloads = serializer.build_activity_payloads()
        return Response({"activities": TimesheetActivityPayloadSerializer(payloads, many=True).data})

    @action(detail=False, methods=["post"], url_path="planned-activities", serializer_class=TimesheetPlannedActivitiesSerializer)
    def planned_activities(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        payloads = serializer.build_activity_payloads()
        return Response({"activities": TimesheetActivityPayloadSerializer(payloads, many=True).data})

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                "project_id",
                openapi.IN_QUERY,
                description="Project id used to fetch eligible purchase orders for planned activities.",
                type=openapi.TYPE_INTEGER,
                required=True,
            ),
        ]
    )
    @action(detail=False, methods=["get"], url_path="planned-activity-purchase-orders")
    def planned_activity_purchase_orders(self, request, *args, **kwargs):
        raw_project_id = request.query_params.get("project_id")
        if not raw_project_id:
            raise ValidationError({"project_id": "This query parameter is required."})

        try:
            project_id = int(raw_project_id)
        except (TypeError, ValueError):
            raise ValidationError({"project_id": "Project id must be a valid integer."})

        project = Project.objects.filter(id=project_id).first()
        if not project:
            raise ValidationError({"project_id": "Selected project does not exist."})
        if not _user_is_project_team_member(request.user, project):
            raise PermissionDenied(
                "You must be a team member of the selected project."
            )
        purchase_orders = _get_eligible_timesheet_purchase_orders_queryset(
            project,
            request.user,
        )
        return Response(
            {
                "project_id": project.id,
                "purchase_orders": [
                    {
                        "id": purchase_order.id,
                        "reference": purchase_order.reference,
                        "status": purchase_order.status,
                        "po_type": purchase_order.po_type,
                        "project_id": purchase_order.project_id,
                    }
                    for purchase_order in purchase_orders
                ],
            }
        )


def _monthly_timesheet_list_sort_key(item, field):
    if field == "id":
        return item.get("id") or 0
    if field == "created_by":
        return ((item.get("user") or {}).get("name") or "").casefold()
    if field == "month_label":
        return (item.get("year") or 0, item.get("month") or 0)
    if field == "month":
        return item.get("month") or 0
    if field == "year":
        return item.get("year") or 0
    if field == "scope_summary":
        return (item.get("scope_summary") or "").casefold()
    if field == "total_hours":
        try:
            return float(item.get("total_hours") or 0)
        except (TypeError, ValueError):
            return 0.0
    if field == "status":
        return (item.get("status") or "").casefold()
    if field == "is_invoiced":
        return 1 if item.get("is_invoiced") else 0
    if field in {"submitted_at", "reviewed_at", "created_at", "updated_at"}:
        return item.get(field) or ""
    return item.get("id") or 0


def _sort_monthly_timesheet_list_items(items, ordering):
    """
    Apply list `ordering` to the full serialized result set before pagination.

    List filters (submission_scope / is_invoiced) run on serialized rows, and several
    columns are SerializerMethodFields, so OrderingFilter alone cannot sort the
    paginated table for every column.
    """
    if not ordering:
        ordering = "-submitted_at"
    descending = str(ordering).startswith("-")
    field = str(ordering)[1:] if descending else str(ordering)
    allowed = {
        "id",
        "created_by",
        "month_label",
        "month",
        "year",
        "scope_summary",
        "total_hours",
        "status",
        "is_invoiced",
        "submitted_at",
        "reviewed_at",
        "created_at",
        "updated_at",
    }
    if field not in allowed:
        field = "submitted_at"
        descending = True

    # Stable secondary by id (ascending) regardless of primary direction.
    ordered = sorted(items, key=lambda item: item.get("id") or 0)
    return sorted(
        ordered,
        key=lambda item: _monthly_timesheet_list_sort_key(item, field),
        reverse=descending,
    )


class MonthlyTimesheetViewSet(viewsets.GenericViewSet):
    queryset = MonthlyTimesheetSubmission.objects.all()
    serializer_class = MonthlyTimesheetSubmissionSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPageNumberPagination
    filter_backends = [filters.OrderingFilter]
    ordering_fields = [
        "id",
        "submitted_at",
        "reviewed_at",
        "year",
        "month",
        "status",
        "created_at",
    ]
    ordering = ["-year", "-month", "-submitted_at", "-id"]

    def get_serializer_class(self):
        if self.action == "activity_documents":
            return MonthlyTimesheetActivityDocumentsSerializer
        return super().get_serializer_class()

    def _user_can_manage_own_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {
                "time_booking": [
                    "view",
                    "view_created",
                    "create",
                    "create_without_approval",
                    "update",
                    "update_created",
                    "full_access",
                ]
            },
            "company",
            {},
        )

    def _user_can_review_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {"time_booking": ["review", "full_access"]},
            "company",
            {},
        )

    def _user_can_view_company_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {"time_booking": ["view", "review", "full_access"]},
            "company",
            {},
        )

    def _user_can_auto_approve_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {"time_booking": ["create_without_approval", "full_access"]},
            "company",
            {},
        )

    def _user_can_delete_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {"time_booking": ["delete", "full_access"]},
            "company",
            {},
        )

    def _user_can_delete_created_timesheets(self, user):
        return require_any_permission_multiple_modules(
            user,
            {"time_booking": ["delete_created", "delete", "full_access"]},
            "company",
            {},
        )

    def _assert_user_can_manage_own_timesheets(self, user):
        if not self._user_can_manage_own_timesheets(user):
            raise PermissionDenied("You do not have permission to access monthly timesheets.")

    def _assert_user_can_review_timesheets(self, user):
        if not self._user_can_review_timesheets(user):
            raise PermissionDenied("You do not have permission to review monthly timesheets.")

    def _assert_user_can_view_company_timesheets(self, user):
        if not self._user_can_view_company_timesheets(user):
            raise PermissionDenied("You do not have permission to view monthly timesheets.")

    def _get_reviewer_queryset(self, user):
        if user.is_freelancer or not user.company_id:
            return MonthlyTimesheetSubmission.objects.none()
        return MonthlyTimesheetSubmission.objects.filter(user__company_id=user.company_id)

    def _can_delete_submission(self, user, submission):
        if submission.status == "approved":
            return False

        if submission.user_id == user.id:
            return self._user_can_delete_created_timesheets(user)

        if user.company_id and submission.user.company_id == user.company_id:
            return self._user_can_delete_timesheets(user)

        return False

    def _get_submission_for_access(self, submission_id):
        submission = get_object_or_404(MonthlyTimesheetSubmission, pk=submission_id)
        request_user = self.request.user
        if submission.user_id == request_user.id:
            self._assert_user_can_manage_own_timesheets(request_user)
            return submission
        if request_user.company_id and submission.user.company_id == request_user.company_id:
            self._assert_user_can_view_company_timesheets(request_user)
            return submission
        raise PermissionDenied("You do not have permission to access this monthly timesheet.")

    def _build_submission_preview_response(self, submission):
        preview_payload = _build_monthly_timesheet_preview(submission.user, submission.month, submission.year)
        serializer = MonthlyTimesheetPreviewSerializer(
            {
                "submission": submission,
                **preview_payload,
            },
            context={
                "preview_by_submission_id": {
                    submission.id: preview_payload,
                }
            },
        )
        return serializer.data

    def _resolve_generated_timecard_defaults(self, activity):
        defaults = {
            "project": None,
            "company_profile": None,
            "freelancer": None,
            "milestone": None,
            "action_item": None,
        }
        if activity.source_type == "project":
            defaults["project"] = activity.project
        elif activity.source_type == "project_phase":
            defaults["project"] = activity.project_milestone.project if activity.project_milestone else None
            defaults["milestone"] = activity.project_milestone
        elif activity.source_type == "project_action_item":
            action_project, _, _ = get_related_objects_from_action_items(activity.project_action_item)
            defaults["project"] = action_project
            defaults["action_item"] = activity.project_action_item
        elif activity.source_type == "company_action_item":
            _, company_profile, freelancer = get_related_objects_from_action_items(activity.company_action_item)
            defaults["company_profile"] = company_profile
            defaults["freelancer"] = freelancer
            defaults["action_item"] = activity.company_action_item
        elif activity.source_type == "company_calendar_event":
            defaults["company_profile"] = activity.company_calendar_event.company_profile if activity.company_calendar_event else None
            defaults["freelancer"] = activity.company_calendar_event.freelancer if activity.company_calendar_event else None
        return defaults

    def _resolve_generated_locked_timecard_defaults(self, submission):
        return {
            "project": None,
            "milestone": None,
            "action_item": None,
            "company_profile": None if submission.user.is_freelancer else submission.user.company,
            "freelancer": submission.user if submission.user.is_freelancer else None,
        }

    def _build_generated_locked_timecard_name(self, locked_day_detail):
        lock_type = locked_day_detail.get("lock_type")
        if lock_type == "public_holiday":
            return locked_day_detail.get("holiday_name") or "Public holiday"

        leave_type = str(locked_day_detail.get("leave_type") or "").strip()
        if leave_type:
            return f"{leave_type.capitalize()} leave"

        return "Leave"

    def _refresh_generated_timecards(self, submission, reviewer):
        TimeCard.objects.filter(timesheet_submission=submission).delete()
        preview = _build_monthly_timesheet_preview(
            submission.user,
            submission.month,
            submission.year,
        )
        activity_ids = {
            activity["id"]
            for week in preview.get("weeks", [])
            for activity in week.get("activities", [])
            if activity.get("id")
        }
        activity_entries = {
            entry["id"]: {
                "activity_id": activity["id"],
                "entry_date": entry["entry_date"],
            }
            for week in preview.get("weeks", [])
            for activity in week.get("activities", [])
            for entry in activity.get("entries", [])
            if entry.get("id")
        }
        activities_by_id = {
            activity.id: activity
            for activity in TimesheetActivity.objects.filter(id__in=activity_ids)
            .select_related(
                "project",
                "project_milestone",
                "project_action_item",
                "company_action_item",
                "company_calendar_event",
            )
        }
        entries_by_id = {
            entry.id: entry
            for entry in TimesheetEntry.objects.filter(id__in=activity_entries.keys())
            .select_related("activity")
        }

        generated_cards = []
        for week in preview.get("weeks", []):
            for activity_payload in week.get("activities", []):
                activity = activities_by_id.get(activity_payload.get("id"))
                if not activity:
                    continue

                defaults = self._resolve_generated_timecard_defaults(activity)
                for entry_payload in activity_payload.get("entries", []):
                    entry = entries_by_id.get(entry_payload.get("id"))
                    if not entry:
                        continue

                    generated_cards.append(
                        TimeCard(
                            name=activity.activity_name,
                            description=f"Generated from monthly timesheet {submission.month:02d}/{submission.year}",
                            date=entry.entry_date,
                            hours=float(entry.hours or 0),
                            status=StatusType.APPROVED.value,
                            approved_by=reviewer,
                            created_by=submission.user,
                            timesheet_entry=entry,
                            timesheet_submission=submission,
                            is_generated_from_timesheet=True,
                            is_invoiceable_from_timesheet=activity.is_invoiceable,
                            **defaults,
                        )
                    )

            locked_defaults = self._resolve_generated_locked_timecard_defaults(
                submission,
            )
            for locked_day_detail in week.get("locked_day_details", []):
                locked_hours = float(locked_day_detail.get("hours") or 0)
                if locked_hours <= 0:
                    continue

                generated_cards.append(
                    TimeCard(
                        name=self._build_generated_locked_timecard_name(
                            locked_day_detail,
                        ),
                        description=f"Generated from monthly timesheet {submission.month:02d}/{submission.year}",
                        date=parse_date(locked_day_detail["date"]),
                        hours=locked_hours,
                        status=StatusType.APPROVED.value,
                        approved_by=reviewer,
                        created_by=submission.user,
                        timesheet_entry=None,
                        timesheet_submission=submission,
                        is_generated_from_timesheet=True,
                        is_invoiceable_from_timesheet=True,
                        **locked_defaults,
                    )
                )

        if generated_cards:
            TimeCard.objects.bulk_create(generated_cards)

    def _notify_submission(self, submission, actor, action_type):
        actor_name = get_actor_name(actor)
        content_type = ContentType.objects.get_for_model(MonthlyTimesheetSubmission)
        object_name = f'Monthly Timesheet "{submission.month:02d}/{submission.year}"'

        if action_type == "submit":
            recipients = set()
            if actor.company_id:
                candidate_users = User.objects.filter(company_id=actor.company_id).exclude(id=actor.id)
                for candidate in candidate_users:
                    if self._user_can_review_timesheets(candidate):
                        recipients.add(candidate)
            message = f'{object_name} from {actor_name} requires your approval.'
            create_notification_with_recipients(
                message=message,
                content_type=content_type,
                object_id=submission.id,
                content_object=submission,
                notification_type="monthly_timesheet",
                action_type="create",
                recipients=recipients,
            )
            return

        if action_type == "approve":
            message = f"{object_name} was approved by {actor_name}."
        else:
            message = f"{object_name} was rejected by {actor_name}."

        create_notification_with_recipients(
            message=message,
            content_type=content_type,
            object_id=submission.id,
            content_object=submission,
            notification_type="monthly_timesheet",
            action_type="update",
            recipients={submission.user},
        )

    def get_queryset(self):
        user = self.request.user
        scope = self.request.query_params.get("scope")
        month = self.request.query_params.get("month")
        year = self.request.query_params.get("year")
        users = self.request.query_params.get("users")
        status_param = self.request.query_params.get("status")
        search = self.request.query_params.get("search")

        if scope == "review":
            self._assert_user_can_view_company_timesheets(user)
            queryset = self._get_reviewer_queryset(user)
        else:
            self._assert_user_can_manage_own_timesheets(user)
            queryset = MonthlyTimesheetSubmission.objects.filter(user=user)

        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)
        if status_param:
            statuses = [
                item.strip().lower()
                for item in str(status_param).split(",")
                if item.strip()
            ]
            if len(statuses) == 1:
                queryset = queryset.filter(status=statuses[0])
            elif statuses:
                queryset = queryset.filter(status__in=statuses)
        elif scope == "review":
            queryset = queryset.exclude(status="draft")
        if users:
            user_ids = [int(item) for item in str(users).split(",") if str(item).strip().isdigit()]
            if user_ids:
                queryset = queryset.filter(user_id__in=user_ids)
        if search:
            normalized_search = str(search).strip()
            if normalized_search.isdigit():
                queryset = queryset.filter(id=int(normalized_search))
            else:
                queryset = queryset.none()
        return queryset.select_related("user", "reviewed_by").prefetch_related(
            "feedback_entries"
        )

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        submission_ids = list(queryset.values_list("id", flat=True))
        preview_by_submission_id = {
            submission.id: _build_monthly_timesheet_preview(submission.user, submission.month, submission.year)
            for submission in queryset
        }
        invoiced_submission_ids = set(
            InvoiceLineItem.objects.filter(
                monthly_timesheet_submission_id__in=submission_ids,
            ).exclude(
                monthly_timesheet_submission_id__isnull=True,
            ).values_list("monthly_timesheet_submission_id", flat=True)
        )
        serializer = self.get_serializer(
            queryset,
            many=True,
            context={
                "preview_by_submission_id": preview_by_submission_id,
                "invoiced_submission_ids": invoiced_submission_ids,
            },
        )
        submission_scope = (
            request.query_params.get("submission_scope")
            or request.query_params.get("type")
            or ""
        ).strip().lower()
        is_invoiced_param = request.query_params.get("is_invoiced")
        serialized_data = list(serializer.data)

        submission_scopes = [
            scope.strip()
            for scope in submission_scope.split(",")
            if scope.strip() in {"company", "project"}
        ]
        if submission_scopes:
            serialized_data = [
                item
                for item in serialized_data
                if any(
                    (
                        scope == "company" and item.get("has_company_scope")
                    )
                    or (
                        scope == "project" and item.get("has_project_scope")
                    )
                    for scope in submission_scopes
                )
            ]

        if is_invoiced_param is not None:
            normalized_is_invoiced = str(is_invoiced_param).strip().lower()
            if normalized_is_invoiced in {"true", "false"}:
                expected_invoiced = normalized_is_invoiced == "true"
                serialized_data = [
                    item
                    for item in serialized_data
                    if bool(item.get("is_invoiced")) == expected_invoiced
                ]

        # Whole-table sort before pagination (includes computed list columns).
        serialized_data = _sort_monthly_timesheet_list_items(
            serialized_data,
            request.query_params.get("ordering"),
        )

        page = self.paginate_queryset(serialized_data)
        if page is not None:
            return self.get_paginated_response(page)
        return Response(serialized_data)

    def retrieve(self, request, *args, **kwargs):
        submission = self._get_submission_for_access(kwargs["pk"])
        serializer = self.get_serializer(submission)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="preview")
    def preview(self, request, *args, **kwargs):
        serializer = MonthlyTimesheetPreviewRequestSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        self._assert_user_can_manage_own_timesheets(request.user)
        preview_payload = _build_monthly_timesheet_preview(
            request.user,
            serializer.validated_data["month"],
            serializer.validated_data["year"],
        )
        existing_submission = MonthlyTimesheetSubmission.objects.filter(
            user=request.user,
            month=serializer.validated_data["month"],
            year=serializer.validated_data["year"],
        ).first()
        response_serializer = MonthlyTimesheetPreviewSerializer(
            {
                "submission": existing_submission,
                **preview_payload,
            },
            context={
                "preview_by_submission_id": (
                    {existing_submission.id: preview_payload}
                    if existing_submission
                    else {}
                )
            },
        )
        return Response(response_serializer.data)

    @action(detail=True, methods=["get"], url_path="preview")
    def preview_submission(self, request, *args, **kwargs):
        submission = self._get_submission_for_access(kwargs["pk"])
        return Response(self._build_submission_preview_response(submission))

    @action(
        detail=False,
        methods=["put"],
        url_path="activity-documents",
        serializer_class=MonthlyTimesheetActivityDocumentsSerializer,
    )
    @transaction.atomic
    def activity_documents(self, request, *args, **kwargs):
        self._assert_user_can_manage_own_timesheets(request.user)
        serializer = self.get_serializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        linked_documents = serializer.save(user=request.user)
        return Response(
            {
                "row_signature": serializer.validated_data.get(
                    "row_signature",
                    request.data.get("row_signature"),
                ),
                "linked_documents": _serialize_monthly_timesheet_documents(
                    linked_documents
                ),
            }
        )

    @action(detail=False, methods=["post"], url_path="submit")
    def submit(self, request, *args, **kwargs):
        serializer = MonthlyTimesheetSubmissionActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self._assert_user_can_manage_own_timesheets(request.user)

        month = serializer.validated_data["month"]
        year = serializer.validated_data["year"]
        comment = serializer.validated_data.get("comment")
        preview_payload = _build_monthly_timesheet_preview(request.user, month, year)

        if preview_payload["month_total_hours"] <= 0:
            raise ValidationError({"non_field_errors": ["Cannot submit a month without any logged hours."]})

        with transaction.atomic():
            submission, created = MonthlyTimesheetSubmission.objects.get_or_create(
                user=request.user,
                month=month,
                year=year,
                defaults={"status": "draft"},
            )

            if submission.status == "pending":
                raise ValidationError({"non_field_errors": ["This monthly timesheet is already pending review."]})
            if submission.status == "approved":
                raise ValidationError({"non_field_errors": ["This monthly timesheet has already been approved."]})

            auto_approve = self._user_can_auto_approve_timesheets(request.user)
            submitted_at = timezone.now()
            submission.status = "approved" if auto_approve else "pending"
            submission.submitted_at = submitted_at
            submission.reviewed_at = submitted_at if auto_approve else None
            submission.reviewed_by = request.user if auto_approve else None
            submission.save(
                update_fields=[
                    "status",
                    "submitted_at",
                    "reviewed_at",
                    "reviewed_by",
                    "updated_at",
                ]
            )

            if not created and comment:
                MonthlyTimesheetFeedback.objects.create(
                    submission=submission,
                    author=request.user,
                    feedback_type="resubmission",
                    comment=comment,
                )

            if auto_approve:
                self._refresh_generated_timecards(submission, request.user)

        if not auto_approve:
            self._notify_submission(submission, request.user, "submit")
        return Response(self._build_submission_preview_response(submission), status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, *args, **kwargs):
        submission = self._get_submission_for_access(kwargs["pk"])
        self._assert_user_can_review_timesheets(request.user)
        if submission.status != "pending":
            raise ValidationError({"non_field_errors": ["Only pending monthly timesheets can be approved."]})

        with transaction.atomic():
            submission.status = "approved"
            submission.reviewed_at = timezone.now()
            submission.reviewed_by = request.user
            submission.save(update_fields=["status", "reviewed_at", "reviewed_by", "updated_at"])
            self._refresh_generated_timecards(submission, request.user)

        self._notify_submission(submission, request.user, "approve")
        return Response(self._build_submission_preview_response(submission), status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, *args, **kwargs):
        submission = self._get_submission_for_access(kwargs["pk"])
        self._assert_user_can_review_timesheets(request.user)
        serializer = MonthlyTimesheetReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if submission.status != "pending":
            raise ValidationError({"non_field_errors": ["Only pending monthly timesheets can be rejected."]})

        comment = serializer.validated_data.get("comment")
        if not comment:
            raise ValidationError({"comment": ["Feedback comment is required when rejecting a monthly timesheet."]})

        with transaction.atomic():
            submission.status = "rejected"
            submission.reviewed_at = timezone.now()
            submission.reviewed_by = request.user
            submission.save(update_fields=["status", "reviewed_at", "reviewed_by", "updated_at"])
            MonthlyTimesheetFeedback.objects.create(
                submission=submission,
                author=request.user,
                feedback_type="rejection",
                comment=comment,
            )

        self._notify_submission(submission, request.user, "reject")
        return Response(self._build_submission_preview_response(submission), status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        submission = self._get_submission_for_access(kwargs["pk"])

        if submission.status == "approved":
            raise ValidationError(
                {"non_field_errors": ["Approved monthly timesheets cannot be deleted."]}
            )

        if not self._can_delete_submission(request.user, submission):
            raise PermissionDenied("You do not have permission to delete this monthly timesheet.")

        submission.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class TimeCardViewSet(viewsets.ModelViewSet):
    queryset = TimeCard.objects.all()
    serializer_class = TimeCardSerializer
    filterset_class = TimeCardFilter
    filter_backends = [DjangoFilterBackend, TableOrderingFilter, filters.SearchFilter]
    search_fields = ['name']
    ordering_fields = [
        'id',
        'name',
        'created_by',
        'date',
        'hours',
        'project__project_name',
        'description',
        'status',
        'po_numbers'
    ]
    ordering = ['-id']
    ordering_text_fields = {'name', 'description', 'status', 'project__project_name'}
    ordering_natural_fields = {'name', 'description', 'project__project_name'}

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            print(f'[DEBUG] TimeCardViewSet: get_permissions: list | self.request.query_params: {self.request.query_params}')
            return [TimeBookingPermission(action='view', scope='company')]
        elif self.action == 'create':
            return [TimeBookingPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [TimeBookingPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [TimeBookingPermission(action='delete', scope='company')]
        return super().get_permissions()

    def perform_create(self, serializer):
        request_user = self.request.user
        
        # Let the serializer handle all status logic
        instance = serializer.save(created_by=request_user)
        
        # Only send approval email if status is still pending after serializer processing
        if instance.status == StatusType.PENDING.value:
            product = get_product_name_from_request(self.request)
            frontend_url = get_frontend_url_for_solution(product)
            if instance.project:
                project_owner = instance.project.user
                if not project_owner.is_freelancer:
                    company_admin = project_owner.company.admin_user
                    send_approval_email(company_admin, "Time Card", instance, frontend_url)
                elif project_owner.is_freelancer:
                    send_approval_email(project_owner, "Time Card", instance, frontend_url)
            elif request_user.staff.supervisor:
                send_approval_email(request_user.staff.supervisor.user, "Time Card", instance, frontend_url)

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return TimeCard.objects.none()

        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]

        has_time_booking_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'time_booking': ['view', 'full_access', 'review']}, 
            'company', 
            self.request.query_params
        )

        has_time_booking_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'time_booking': ['view_created', 'update_created', 'delete_created']}, 
            'company', 
            self.request.query_params
        )
        query_filters = Q()
        if not user.is_freelancer and has_time_booking_company_permissions:
            query_filters = (
                (Q(project__isnull=False, project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))) |
                Q(company_profile__isnull=False, company_profile=user.company)
            )
        elif user.is_freelancer and has_time_booking_company_permissions:
             # Freelancers see their own time cards and project-related ones
            query_filters = (
                (Q(project__isnull=False, project__solution__in=products) & 
                Q(project__team_members__team_member=user)) |  # Only projects where they are team members
                Q(freelancer__isnull=False, freelancer=user)  # Only their own freelancer expense cards
            )
        elif has_time_booking_created_permissions:
            # Users with assigned/created permissions only see their own time cards
            query_filters = Q(created_by=user)

        queryset = TimeCard.objects.filter(query_filters).distinct()

        # Optional filter by latest invoice status
        invoice_status = self.request.query_params.get("invoice_status")
        if invoice_status:
            latest_invoice_subquery = InvoiceLineItem.objects.filter(
                time_card=OuterRef('pk')
            ).filter(
                invoice__document_type='invoice'
            ).order_by('-created_at').values('invoice__status')[:1]

            queryset = queryset.annotate(
                latest_invoice_status=Subquery(latest_invoice_subquery)
            ).filter(latest_invoice_status=invoice_status)

        # Optional boolean filter: whether the time card has any invoice line items
        is_invoiced_param = self.request.query_params.get("is_invoiced")
        if is_invoiced_param is not None:
            value = str(is_invoiced_param).lower()
            truthy = {"true", "1", "yes"}
            falsy = {"false", "0", "no"}

            if value in truthy:
                queryset = queryset.filter(invoice_line_items__isnull=False)
            elif value in falsy:
                queryset = queryset.filter(invoice_line_items__isnull=True)

        ordering = self.request.query_params.get('ordering')
        if ordering in ('project__project_name', '-project__project_name'):
            asc = not ordering.startswith('-')
            expr = F('project__project_name')
            if asc:
                return queryset.order_by(expr.asc(nulls_last=True))
            else:
                return queryset.order_by(expr.desc(nulls_last=True))
        elif ordering in ('po_numbers', '-po_numbers'):
            queryset = _annotate_visible_purchase_order_count(
                queryset,
                self.request.user,
            )
            asc = not ordering.startswith('-')
            if asc:
                return queryset.order_by('po_count', 'id')
            else:
                return queryset.order_by('-po_count', '-id')
        return queryset

    def get_object(self):
        obj = get_object_or_404(TimeCard, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        request_user = self.request.user

        # Check project access
        if obj.project:
            has_company_projects_permissions = require_any_permission_multiple_modules(
                request_user,
                {"projects": ["full_access", "view_all_access"]},
                "company",
                {"project": obj.project.id}
            )
            is_team_member = obj.project.team_members.filter(team_member=request_user).exists()

            if not has_company_projects_permissions and not is_team_member:
                raise PermissionDenied("You do not have permission to access this TimeCard's project.")

        # Check company profile access
        elif obj.company_profile:
            if not request_user.company or request_user.company != obj.company_profile:
                raise PermissionDenied("You do not have permission to access this TimeCard's company profile.")

        # Check freelancer access
        elif obj.freelancer:
            if obj.freelancer != request_user:
                raise PermissionDenied("You do not have permission to access this TimeCard.")

        return obj

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'invoice_status',
                openapi.IN_QUERY,
                description="Invoice status",
                type=openapi.TYPE_STRING,
                required=False
            ),
            openapi.Parameter(
                'is_invoiced',
                openapi.IN_QUERY,
                description="Filter time cards that have (true) or do not have (false) any invoice.",
                type=openapi.TYPE_BOOLEAN,
                required=False
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        ordering = request.query_params.get("ordering")
        computed_fields = {
            "invoice",
            "project_company"
        }
        sort_by = ordering.lstrip("-") if ordering else None

        if sort_by not in computed_fields:
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(queryset, many=True, context={'request': request})
            return Response(serializer.data)

        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data
        
        if ordering:
            reverse = ordering.startswith("-")

            # Computed fields that need Python-side sorting
            if sort_by in computed_fields:
                # Sort by the computed field
                def get_computed_value(item):
                    if sort_by == "invoice":
                        invoice = item.get('invoice')
                        if invoice:
                            return (0, invoice.get('status', ''))
                        # Use tuple to handle None values properly
                        # (1, '') ensures None values come after non-None values
                        return (1, '')
                        
                    elif sort_by == "project_company":
                        project_company = item.get('project_company')
                        if project_company:
                            return (0, project_company.get('name', ''))
                        return (1, '')
                    return (0, '')

                data = sorted(data, key=get_computed_value, reverse=reverse)
                if sort_by == "project_company":
                  data.reverse()
        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)


class ExpenseCardViewSet(viewsets.ModelViewSet):
    queryset = ExpenseCard.objects.all()
    serializer_class = ExpenseCardSerializer
    filterset_class = ExpenseCardFilter
    filter_backends = [DjangoFilterBackend, TableOrderingFilter, filters.SearchFilter]
    search_fields = ['name']
    ordering_fields = [
        'id',
        'name',
        'category',
        'created_by',
        'expense_date',
        'hours',
        'quantity',
        'project',
        'project__project_name',
        'total',
        'description',
        'status',
        'rate',
        'po_numbers',
        'invoice_sort_id',
        'created_by_full_name',
        'booking_scope_order',
    ]
    ordering = ['-id']
    ordering_text_fields = {
        'name',
        'category',
        'description',
        'status',
        'project__project_name',
        'created_by_full_name',
        'booking_scope_order',
    }
    ordering_natural_fields = {
        'name',
        'description',
        'project__project_name',
        'created_by_full_name',
        'booking_scope_order',
    }

    def get_permissions(self):
        if self.action in ['list', 'retrieve']:
            return [ExpenseBookingPermission(action='view', scope='company')]
        elif self.action == 'create':
            return [ExpenseBookingPermission(action='create', scope='company')]
        elif self.action in ['update', 'partial_update']:
            return [ExpenseBookingPermission(action='update', scope='company')]
        elif self.action == 'destroy':
            return [ExpenseBookingPermission(action='delete', scope='company')]
        return super().get_permissions()

    def perform_create(self, serializer):
        request_user = self.request.user
        
        # Let the serializer handle all status logic
        instance = serializer.save(created_by=request_user)
        
        # Only send approval email if status is still pending after serializer processing
        if instance.status == StatusType.PENDING.value:
            product = get_product_name_from_request(self.request)
            frontend_url = get_frontend_url_for_solution(product)
            if instance.project:
                project_owner = instance.project.user
                if not project_owner.is_freelancer:
                    company_admin = project_owner.company.admin_user
                    send_approval_email(company_admin, "Expense Card", instance, frontend_url)
                elif project_owner.is_freelancer:
                    send_approval_email(project_owner, "Expense Card", instance, frontend_url)
            elif request_user.staff.supervisor:
                send_approval_email(request_user.staff.supervisor.user, "Expense Card", instance, frontend_url)

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ExpenseCard.objects.none()

        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION:
            products = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        else:
            products = [product_name]

        has_expense_booking_company_permissions = require_any_permission_multiple_modules(
            user, 
            {'expense_booking': ['view', 'full_access', 'review']}, 
            'company', 
            self.request.query_params
        )

        has_expense_booking_created_permissions = require_any_permission_multiple_modules(
            user, 
            {'expense_booking': ['view_created', 'update_created', 'delete_created']}, 
            'company', 
            self.request.query_params
        )
        query_filters = Q()
        if not user.is_freelancer and has_expense_booking_company_permissions:
            query_filters = (
                (Q(project__isnull=False, project__solution__in=products) & 
                (Q(project__user__company=user.company) | Q(project__team_members__team_member=user))) |
                Q(company_profile__isnull=False, company_profile=user.company)
            )
        elif user.is_freelancer and has_expense_booking_company_permissions:
            # Freelancers see their own expense cards and project-related ones
            query_filters = (
                (Q(project__isnull=False, project__solution__in=products) & 
                Q(project__team_members__team_member=user)) |  # Only projects where they are team members
                Q(freelancer__isnull=False, freelancer=user)  # Only their own freelancer expense cards
            )
        elif has_expense_booking_created_permissions:
            # Users with assigned/created permissions only see their own expense cards
            query_filters = Q(created_by=user)

        queryset = ExpenseCard.objects.filter(query_filters).distinct().annotate(
            invoice_sort_id=Subquery(
                InvoiceLineItem.objects.filter(
                    expense_card_id=OuterRef('pk'),
                    invoice__document_type='invoice',
                ).order_by('-created_at').values('invoice_id')[:1],
                output_field=IntegerField(),
            ),
            created_by_full_name=Concat(
                'created_by__first_name',
                Value(' '),
                'created_by__last_name',
                output_field=TextField(),
            ),
            booking_scope_order=Case(
                When(project__isnull=False, then=Value('project')),
                default=Value('company'),
                output_field=CharField(),
            ),
        )

        # Optional filter by latest invoice status
        invoice_status = self.request.query_params.get("invoice_status")
        if invoice_status:
            latest_invoice_subquery = InvoiceLineItem.objects.filter(
                expense_card=OuterRef('pk')
            ).filter(
                invoice__document_type='invoice'
            ).order_by('-created_at').values('invoice__status')[:1]

            queryset = queryset.annotate(
                latest_invoice_status=Subquery(latest_invoice_subquery)
            ).filter(latest_invoice_status=invoice_status)

        # Optional boolean filter: whether the expense card has any invoice line items
        is_invoiced_param = self.request.query_params.get("is_invoiced")
        if is_invoiced_param is not None:
            value = str(is_invoiced_param).lower()
            truthy = {"true", "1", "yes"}
            falsy = {"false", "0", "no"}

            if value in truthy:
                queryset = queryset.filter(invoice_line_items__isnull=False)
            elif value in falsy:
                queryset = queryset.filter(invoice_line_items__isnull=True)
        
        ordering = self.request.query_params.get('ordering')
        if ordering in ('project__project_name', '-project__project_name'):
            asc = not ordering.startswith('-')
            expr = F('project__project_name')
            if asc:
                return queryset.order_by(expr.asc(nulls_last=True))
            else:
                return queryset.order_by(expr.desc(nulls_last=True))
        elif ordering in ('po_numbers', '-po_numbers'):
            queryset = _annotate_visible_purchase_order_count(
                queryset,
                self.request.user,
            )
            asc = not ordering.startswith('-')
            if asc:
                return queryset.order_by('po_count', 'id')
            else:
                return queryset.order_by('-po_count', '-id')
            
        return queryset

    def get_object(self):
        obj = get_object_or_404(ExpenseCard, pk=self.kwargs['pk'])
        self.check_object_permissions(self.request, obj)
        request_user = self.request.user

        # Check project access
        if obj.project:
            has_company_projects_permissions = require_any_permission_multiple_modules(
                request_user,
                {"projects": ["full_access", "view_all_access"]},
                "company",
                {"project": obj.project.id}
            )
            is_team_member = obj.project.team_members.filter(team_member=request_user).exists()
            if not has_company_projects_permissions and not is_team_member:
                raise PermissionDenied("You do not have permission to access this ExpenseCard's project.")

        # Check company profile access
        elif obj.company_profile:
            if not request_user.company or request_user.company != obj.company_profile:
                raise PermissionDenied("You do not have permission to access this ExpenseCard's company profile.")

        # Check freelancer access
        elif obj.freelancer:
            if obj.freelancer != request_user:
                raise PermissionDenied("You do not have permission to access this ExpenseCard.")

        return obj

    @swagger_auto_schema(
        manual_parameters=[
            openapi.Parameter(
                'invoice_status',
                openapi.IN_QUERY,
                description="Invoice status",
                type=openapi.TYPE_STRING,
                required=False
            ),
            openapi.Parameter(
                'is_invoiced',
                openapi.IN_QUERY,
                description="Filter expense cards that have (true) or do not have (false) any invoice.",
                type=openapi.TYPE_BOOLEAN,
                required=False
            ),
            openapi.Parameter(
                'ordering',
                openapi.IN_QUERY,
                description="Fields to order by, comma separated. Prefix with '-' for descending.",
                type=openapi.TYPE_STRING,
                example="date,-total,invoice,project_company"
            ),
        ]
    )
    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        ordering = request.query_params.get("ordering")
        sort_by = ordering.lstrip("-") if ordering else None

        if sort_by not in {"invoice", "project_company"}:
            page = self.paginate_queryset(queryset)
            if page is not None:
                serializer = self.get_serializer(page, many=True, context={'request': request})
                return self.get_paginated_response(serializer.data)

            serializer = self.get_serializer(queryset, many=True, context={'request': request})
            return Response(serializer.data)

        serializer = self.get_serializer(queryset, many=True, context={'request': request})
        data = serializer.data
        reverse = ordering.startswith("-")

        def get_computed_value(item):
            if sort_by == "invoice":
                invoice = item.get('invoice')
                if invoice:
                    return (0, invoice.get('status', ''))
                return (1, '')

            project_company = item.get('project_company', {})
            if project_company:
                return (0, project_company.get('name', ''))
            return (1, '')

        data = sorted(data, key=get_computed_value, reverse=reverse)
        if sort_by == "project_company":
            data.reverse()

        page = self.paginate_queryset(data)
        if page is not None:
            return self.get_paginated_response(page)

        return Response(data)


class ProjectTimeViewSet(viewsets.ModelViewSet):
    queryset = ProjectTime.objects.all().order_by('id')
    serializer_class = ProjectTimeSerializer
    filter_backends = [filters.SearchFilter, DjangoFilterBackend]
    filterset_fields = ['project']
    search_fields = ['user__first_name', 'user__last_name']

    def get_serializer(self, *args, **kwargs):
        serializer = super(ProjectTimeViewSet, self).get_serializer(*args, **kwargs)
        serializer.context['user'] = self.request.user
        return serializer

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ProjectTime.objects.none()
        product_name = get_product_name_from_request(self.request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
        return ProjectTime.objects.filter(Q(project__team_members__team_member=user, project__solution__in=products)).distinct().order_by('-id')

    def get_object(self):
        obj = get_object_or_404(ProjectTime, pk=self.kwargs['pk'])
        # TODO: Adjust as per new permission system
        # if not has_project_permission(self.request.method, self.request.user, obj.project, get_product_name_from_request(self.request)):
        #     raise PermissionDenied("You do not have permission to perform this action.")
        return obj

    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)
