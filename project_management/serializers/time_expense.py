from calendar import monthrange
from collections import defaultdict
from datetime import timedelta
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from rest_framework import serializers
from django.conf import settings

from project_management.models import ProjectWork, GeneralWork, GeneralExpense, \
    ProjectTime, ECPMPConfig, Project, ProjectExpense, TimeCard, ExpenseCard, \
    TimesheetActivity, TimesheetEntry, MonthlyTimesheetSubmission, MonthlyTimesheetFeedback, \
    MonthlyTimesheetActivityDocumentLink, CompanyPublicHoliday, PurchaseOrder, \
    ActionItem, Milestone
from project_management.models import Document
from project_management.permissions import PurchaseOrderPermission
from models.models import CompanyConfig
from companyprofileapis.serializers import UserSerializer, UserNameSerializer
from ecpmpapp.constants import StatusType, UserType
from project_management.utils import get_product_name_from_request, has_project_permission, get_currency_from_record, symbol_to_currency_code
from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.currency_converter import ECBCurrencyConverter
import logging
from models.models import UserRole
from userapis.models import CalendarEvent
from human_resource_management.models import LeaveRequest

logger = logging.getLogger(__name__)

TIMESHEET_TARGET_FIELD_MODEL_MAP = {
    "company_action_item": ActionItem,
    "company_calendar_event": CalendarEvent,
    "project": Project,
    "purchase_order": PurchaseOrder,
    "project_milestone": Milestone,
    "project_action_item": ActionItem,
}

TIMESHEET_SOURCE_TO_FIELD = {
    "company_action_item": "company_action_item",
    "company_calendar_event": "company_calendar_event",
    "project": "project",
    "project_purchase_order": "purchase_order",
    "project_phase": "project_milestone",
    "project_action_item": "project_action_item",
}

TIMESHEET_LOCKED_SUBMISSION_STATUSES = ["pending", "approved"]
TIMESHEET_LOCKING_LEAVE_TYPES = {"paid", "sick", "casual", "annual"}
TIMESHEET_REVIEWABLE_SUBMISSION_STATUSES = {"pending"}
TIMESHEET_EXCLUDED_PURCHASE_ORDER_STATUSES = {
    "Fully Invoiced",
    "Fully Paid",
    "Closed",
}


def _month_period_bounds(year, month):
    return (
        timezone.datetime(year, month, 1).date(),
        timezone.datetime(year, month, monthrange(year, month)[1]).date(),
    )


def _get_week_start_for_date(entry_date):
    return entry_date - timedelta(days=entry_date.weekday())


def _is_date_in_month_period(entry_date, year, month):
    return entry_date.year == int(year) and entry_date.month == int(month)


def _get_week_starts_for_month(year, month):
    period_start, period_end = _month_period_bounds(year, month)
    week_starts = []
    current_week_start = _get_week_start_for_date(period_start)
    last_week_start = _get_week_start_for_date(period_end)

    while current_week_start <= last_week_start:
        week_starts.append(current_week_start)
        current_week_start += timedelta(days=7)

    return week_starts


def _get_week_dates_in_month(week_start_date, week_end_date, year, month):
    dates = []
    current_day = week_start_date
    while current_day <= week_end_date:
        if _is_date_in_month_period(current_day, year, month):
            dates.append(current_day)
        current_day += timedelta(days=1)
    return dates


def _get_submission_locked_dates_for_week(submission, week_start_date, week_end_date):
    return _get_week_dates_in_month(
        week_start_date,
        week_end_date,
        submission.year,
        submission.month,
    )


def _round_hours(value):
    return round(float(value or 0), 2)


def _is_future_month_period(month, year):
    today = timezone.localdate()
    return (int(year), int(month)) > (today.year, today.month)


def _get_target_display_name(target_object):
    return (
        getattr(target_object, "name", None)
        or getattr(target_object, "project_name", None)
        or str(target_object)
    )


def _get_root_action_item(action_item):
    return action_item.parent_action_item or action_item


def _get_action_item_project_context(action_item):
    root_action_item = _get_root_action_item(action_item)
    milestone = getattr(root_action_item, "milestone", None)
    project = getattr(milestone, "project", None)
    return project, milestone


def _get_timesheet_activity_display_name_for_target(
    source_type,
    target_object,
    project_object=None,
    milestone_object=None,
    action_item_object=None,
):
    return _build_timesheet_activity_display_data(
        source_type,
        target_object,
        project_object=project_object,
        milestone_object=milestone_object,
        action_item_object=action_item_object,
    )["activity_name"]


def _get_timesheet_activity_display_subtitle_for_target(
    source_type,
    target_object,
    project_object=None,
    milestone_object=None,
    action_item_object=None,
):
    return _build_timesheet_activity_display_data(
        source_type,
        target_object,
        project_object=project_object,
        milestone_object=milestone_object,
        action_item_object=action_item_object,
    )["activity_subtitle"]


def _build_timesheet_activity_display_data(
    source_type,
    target_object,
    project_object=None,
    milestone_object=None,
    action_item_object=None,
):
    base_name = _get_target_display_name(target_object)

    resolved_project = project_object
    resolved_milestone = milestone_object
    resolved_action_item = action_item_object

    if source_type == "project":
        resolved_project = resolved_project or target_object
    elif source_type == "project_phase":
        resolved_milestone = resolved_milestone or target_object
        resolved_project = resolved_project or getattr(target_object, "project", None)
    elif source_type == "project_action_item":
        resolved_action_item = resolved_action_item or target_object
        action_item_project, action_item_milestone = _get_action_item_project_context(
            resolved_action_item
        )
        resolved_project = resolved_project or action_item_project
        resolved_milestone = resolved_milestone or action_item_milestone
    elif source_type == "project_purchase_order":
        resolved_project = resolved_project or getattr(target_object, "project", None)

    project_name = getattr(resolved_project, "project_name", None)
    activity_name = base_name
    activity_subtitle = None

    if source_type == "project":
        activity_name = project_name or base_name
    elif source_type == "project_phase":
        activity_name = f"{project_name} / {base_name}" if project_name else base_name
    elif source_type == "project_action_item":
        activity_name = f"{project_name} / {base_name}" if project_name else base_name
        if getattr(resolved_milestone, "name", None):
            activity_subtitle = f"Phase: {resolved_milestone.name}"
    elif source_type == "project_purchase_order":
        activity_name = f"{project_name} / {base_name}" if project_name else base_name
        subtitle_parts = []
        if getattr(resolved_milestone, "name", None):
            subtitle_parts.append(f"Phase: {resolved_milestone.name}")
        if getattr(resolved_action_item, "name", None):
            subtitle_parts.append(f"Action Item: {resolved_action_item.name}")
        activity_subtitle = " · ".join(subtitle_parts) if subtitle_parts else None

    return {
        "activity_code": str(target_object.id),
        "activity_name": activity_name,
        "activity_subtitle": activity_subtitle,
        "project_milestone_name": getattr(resolved_milestone, "name", None),
        "project_action_item_name": getattr(resolved_action_item, "name", None),
    }


def _get_timesheet_activity_display_name_from_instance(activity):
    return _get_timesheet_activity_display_data_from_instance(activity)["activity_name"]


def _get_timesheet_activity_display_subtitle_from_instance(activity):
    return _get_timesheet_activity_display_data_from_instance(activity)[
        "activity_subtitle"
    ]


def _get_timesheet_activity_display_data_from_instance(activity):
    if activity.source_type == "project_purchase_order" and activity.purchase_order:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.purchase_order,
            project_object=activity.project,
            milestone_object=activity.project_milestone,
            action_item_object=activity.project_action_item,
        )

    if activity.source_type == "project_phase" and activity.project_milestone:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.project_milestone,
            project_object=activity.project or getattr(activity.project_milestone, "project", None),
        )

    if activity.source_type == "project_action_item" and activity.project_action_item:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.project_action_item,
            project_object=activity.project,
            milestone_object=activity.project_milestone,
        )

    if activity.source_type == "project" and activity.project:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.project,
        )

    if activity.source_type == "company_action_item" and activity.company_action_item:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.company_action_item,
        )

    if activity.source_type == "company_calendar_event" and activity.company_calendar_event:
        return _build_timesheet_activity_display_data(
            activity.source_type,
            activity.company_calendar_event,
        )

    return {
        "activity_code": activity.activity_code,
        "activity_name": activity.activity_name,
        "activity_subtitle": None,
    }


def _get_project_for_timesheet_activity(activity):
    if activity.source_type in {"project", "project_purchase_order"}:
        return activity.project or getattr(activity.purchase_order, "project", None)
    if activity.source_type == "project_phase" and activity.project_milestone_id:
        return getattr(activity.project_milestone, "project", None)
    if activity.source_type == "project_action_item" and activity.project_action_item_id:
        project, _, _ = get_related_objects_from_action_items(activity.project_action_item)
        return project
    return None


def _build_monthly_client_payload(project):
    project_client = getattr(project, "client", None) if project else None
    if not project_client:
        return None

    if project_client.contact_company_id:
        return {
            "id": project_client.contact_company_id,
            "key": f"company:{project_client.contact_company_id}",
            "type": "company",
            "name": project_client.contact_company.company_name,
        }

    if project_client.contact_user_id:
        full_name = " ".join(
            filter(
                None,
                [
                    project_client.contact_user.name,
                    project_client.contact_user.surname,
                ],
            )
        )
        return {
            "id": project_client.contact_user_id,
            "key": f"user:{project_client.contact_user_id}",
            "type": "user",
            "name": full_name or project_client.contact_user.email,
        }

    return None


def _build_monthly_project_payload(project):
    if not project:
        return None
    return {
        "id": project.id,
        "name": project.project_name,
    }


def _build_monthly_purchase_order_payload(activity):
    if not activity.purchase_order_id:
        return None
    return {
        "id": activity.purchase_order_id,
        "reference": activity.purchase_order.reference,
        "po_type": activity.purchase_order.po_type,
        "document_type": activity.purchase_order.document_type,
        "status": activity.purchase_order.status,
    }


def _build_monthly_activity_row_signature(activity):
    signature_parts = [
        f"source_type:{activity.source_type}",
        f"company_action_item:{activity.company_action_item_id or 0}",
        f"company_calendar_event:{activity.company_calendar_event_id or 0}",
        f"project:{activity.project_id or 0}",
        f"purchase_order:{activity.purchase_order_id or 0}",
        f"project_milestone:{activity.project_milestone_id or 0}",
        f"project_action_item:{activity.project_action_item_id or 0}",
        f"is_invoiceable:{int(bool(activity.is_invoiceable))}",
    ]
    return "|".join(signature_parts)


def _extract_monthly_activity_link_target_ids(activity_payload):
    source_type = activity_payload.get("source_type")
    target_payload = activity_payload.get("target") or {}
    project_payload = activity_payload.get("project") or {}
    purchase_order_payload = activity_payload.get("purchase_order") or {}
    target_id = target_payload.get("id")

    return {
        "source_type": source_type,
        "company_action_item_id": (
            target_id if source_type == "company_action_item" else None
        ),
        "company_calendar_event_id": (
            target_id if source_type == "company_calendar_event" else None
        ),
        "project_id": project_payload.get("id"),
        "purchase_order_id": purchase_order_payload.get("id"),
        "project_milestone_id": (
            target_id if source_type == "project_phase" else None
        ),
        "project_action_item_id": (
            target_id if source_type == "project_action_item" else None
        ),
        "is_invoiceable": bool(activity_payload.get("is_invoiceable", True)),
        "row_signature": activity_payload.get("row_signature"),
    }


def _serialize_monthly_timesheet_documents(documents):
    from project_management.serializers.document import DocumentSerializer

    return DocumentSerializer(documents, many=True).data


def _get_monthly_timesheet_document_links_map(user, month, year):
    links = list(
        MonthlyTimesheetActivityDocumentLink.objects.filter(
            user=user,
            month=month,
            year=year,
        )
        .select_related(
            "document",
            "document__author",
            "document__created_by",
            "document__assignee",
            "document__folder",
        )
        .order_by("document__file_name", "document_id", "id")
    )
    if not links:
        return {}

    serialized_documents_by_id = {
        document["id"]: document
        for document in _serialize_monthly_timesheet_documents(
            [link.document for link in links]
        )
    }
    documents_by_signature = defaultdict(list)
    for link in links:
        serialized_document = serialized_documents_by_id.get(link.document_id)
        if serialized_document:
            documents_by_signature[link.row_signature].append(serialized_document)
    return documents_by_signature

def get_valid_choices(key, project_id, request=None, is_company=False, is_freelancer=False):
    product_name = get_product_name_from_request(request) if request else "Energy Consultant Solution"
    if is_company:
        valid_config_types = CompanyConfig.objects.filter(company_profile=project_id,
                                                          solution=product_name).values_list(key, flat=True)
    elif is_freelancer:
        valid_config_types = CompanyConfig.objects.filter(user=project_id, solution=product_name).values_list(key,
                                                                                                              flat=True)
    else:
        valid_config_types = ECPMPConfig.objects.filter(project_id=project_id).values_list(key, flat=True)
    return list(set([valid_choice for valid_choices in valid_config_types for valid_choice in valid_choices]))


def get_project_owner_valid_choices(key, project, request=None):
    """Return company-level configuration choices for a project owner."""
    if not isinstance(project, Project):
        project = Project.objects.select_related(
            "company_profile",
            "user__company",
        ).filter(pk=project).first()

    if project is None:
        return []

    if project.company_profile_id:
        return get_valid_choices(
            key,
            project.company_profile_id,
            request,
            is_company=True,
        )

    if project.user_id and project.user.is_freelancer:
        return get_valid_choices(
            key,
            project.user_id,
            request,
            is_freelancer=True,
        )

    company_id = getattr(project.user, "company_id", None)
    if company_id:
        return get_valid_choices(
            key,
            company_id,
            request,
            is_company=True,
        )

    return []


def get_related_objects_from_action_items(action_item):
    """Returns related project, company_profile, and freelancer from the action item."""
    root_action_item = action_item.parent_action_item or action_item
    project = root_action_item.milestone.project if root_action_item.milestone else None
    company_profile = root_action_item.company_profile
    freelancer = root_action_item.freelancer
    return project, company_profile, freelancer

def get_related_user_data(obj, user):
    if obj.project:
        return UserNameSerializer(user, context={'scope': 'project', 'project': obj.project}).data
    else:
        return UserNameSerializer(user, context={'scope': 'company'}).data


def _user_can_access_project(user, project):
    if user.is_freelancer:
        return (
            project.user_id == user.id
            or project.team_members.filter(team_member=user).exists()
        )

    return (
        (project.user.company_id == user.company_id if project.user_id and project.user.company_id else False)
        or project.team_members.filter(team_member=user).exists()
    )


def _user_is_project_team_member(user, project):
    return bool(
        user
        and getattr(user, "is_authenticated", False)
        and project
        and project.team_members.filter(team_member=user).exists()
    )


def _build_timesheet_activity_snapshot(source_type, target_object):
    display_data = _build_timesheet_activity_display_data(
        source_type,
        target_object,
    )

    return {
        "activity_code": display_data["activity_code"],
        "activity_name": display_data["activity_name"],
        "activity_subtitle": display_data["activity_subtitle"],
        "source_type": source_type,
    }


def _get_eligible_timesheet_purchase_orders_queryset(project, user):
    queryset = (
        PurchaseOrder.objects.filter(
            project=project,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )
        .exclude(status__in=TIMESHEET_EXCLUDED_PURCHASE_ORDER_STATUSES)
        .order_by("po_date", "id")
    )
    permission = PurchaseOrderPermission(
        action="view",
        scope="company",
    )
    if (
        not _user_is_project_team_member(user, project)
        or not permission.can_select_project_purchase_orders(user)
    ):
        return queryset.none()
    return permission.filter_queryset_for_view(
        queryset,
        user,
    )


def _get_locked_month_submissions(user, week_start_date, week_end_date):
    month_period_filter = Q(
        year=week_start_date.year,
        month=week_start_date.month,
    )
    if week_end_date.year != week_start_date.year or week_end_date.month != week_start_date.month:
        month_period_filter |= Q(
            year=week_end_date.year,
            month=week_end_date.month,
        )

    return MonthlyTimesheetSubmission.objects.filter(
        month_period_filter,
        user=user,
        status__in=TIMESHEET_LOCKED_SUBMISSION_STATUSES,
    )


def _get_week_month_submission_locks(user, week_start_date, week_end_date):
    locked_days = {}
    submissions = _get_locked_month_submissions(user, week_start_date, week_end_date)

    for submission in submissions.order_by("year", "month", "id"):
        for locked_date in _get_submission_locked_dates_for_week(
            submission,
            week_start_date,
            week_end_date,
        ):
            locked_days[locked_date] = {
                "date": locked_date.isoformat(),
                "hours": None,
                "lock_type": "monthly_submission",
                "submission_id": submission.id,
                "submission_status": submission.status,
                "submission_month": submission.month,
                "submission_year": submission.year,
                "reason": "monthly_timesheet_locked",
            }

    return locked_days


def _get_staff_weekly_hours(user):
    try:
        staff = user.staff
    except Exception:
        return 40.0

    return float(staff.weekly_hours or 40.0)


def _get_expected_daily_hours(user):
    return round(_get_staff_weekly_hours(user) / 5, 2)


def _get_daily_leave_hours(user):
    return _get_expected_daily_hours(user)


def _build_daily_warning_metadata(user, daily_totals_map):
    expected_daily_hours = _get_expected_daily_hours(user)
    daily_warning_map = {}
    under_allocated_days_count = 0
    over_allocated_days_count = 0

    for day, total in daily_totals_map.items():
        if day.weekday() >= 5:
            continue

        rounded_total = _round_hours(total)
        if rounded_total < expected_daily_hours:
            warning_state = "under"
            under_allocated_days_count += 1
        elif rounded_total > expected_daily_hours:
            warning_state = "over"
            over_allocated_days_count += 1
        else:
            warning_state = "balanced"

        daily_warning_map[day.isoformat()] = warning_state

    return {
        "expected_daily_hours": expected_daily_hours,
        "daily_warning_map": daily_warning_map,
        "under_allocated_days_count": under_allocated_days_count,
        "over_allocated_days_count": over_allocated_days_count,
    }


def _get_week_leave_locks(user, week_start_date, week_end_date):
    try:
        staff = user.staff
    except Exception:
        return {}

    leave_requests = LeaveRequest.objects.filter(
        employee=staff,
        status="approved",
        is_working_from_home=False,
        start_date__lte=week_end_date,
        end_date__gte=week_start_date,
    ).exclude(type__isnull=True)

    daily_leave_hours = _get_daily_leave_hours(user)
    locked_days = {}

    for leave_request in leave_requests:
        leave_type = str(leave_request.type or "").strip().lower()
        if leave_type not in TIMESHEET_LOCKING_LEAVE_TYPES:
            continue

        current_day = max(leave_request.start_date, week_start_date)
        last_day = min(leave_request.end_date, week_end_date)
        while current_day <= last_day:
            if current_day.weekday() < 5 and current_day not in locked_days:
                locked_days[current_day] = {
                    "date": current_day.isoformat(),
                    "hours": daily_leave_hours,
                    "lock_type": "leave",
                    "leave_type": leave_request.type,
                    "reason": leave_request.reason,
                }
            current_day += timedelta(days=1)

    return locked_days


def _get_week_public_holiday_locks(user, week_start_date, week_end_date):
    company_id = getattr(user, "company_id", None)
    if not company_id:
        return {}

    daily_holiday_hours = _get_daily_leave_hours(user)
    locked_days = {}
    public_holidays = CompanyPublicHoliday.objects.filter(
        company_profile_id=company_id,
        date__gte=week_start_date,
        date__lte=week_end_date,
    ).order_by("date", "id")

    for public_holiday in public_holidays:
        if public_holiday.date.weekday() >= 5:
            continue
        locked_days[public_holiday.date] = {
            "date": public_holiday.date.isoformat(),
            "hours": daily_holiday_hours,
            "lock_type": "public_holiday",
            "holiday_name": public_holiday.name,
            "reason": public_holiday.name,
        }

    return locked_days


def _get_week_locked_days(user, week_start_date, week_end_date):
    locked_days = _get_week_public_holiday_locks(user, week_start_date, week_end_date)
    leave_locks = _get_week_leave_locks(user, week_start_date, week_end_date)

    for lock_date, metadata in leave_locks.items():
        if lock_date not in locked_days:
            locked_days[lock_date] = metadata

    return locked_days


def _get_week_edit_locked_days(user, week_start_date, week_end_date):
    monthly_locks = _get_week_month_submission_locks(user, week_start_date, week_end_date)
    effective_locks = dict(monthly_locks)
    effective_locks.update(_get_week_locked_days(user, week_start_date, week_end_date))
    return effective_locks


def _validate_week_is_editable(user, week_start_date, week_end_date):
    monthly_locked_days = _get_week_month_submission_locks(
        user,
        week_start_date,
        week_end_date,
    )
    if len(monthly_locked_days) >= 7:
        raise serializers.ValidationError(
            "This week cannot be edited because at least one related month is locked for review or already approved."
        )


def _validate_timesheet_target_access(user, source_type, target_object, expected_field):
    if source_type == "project":
        if not _user_can_access_project(user, target_object):
            raise serializers.ValidationError({expected_field: "You do not have access to this project."})
    elif source_type == "project_purchase_order":
        project = target_object.project
        if not project:
            raise serializers.ValidationError(
                {expected_field: "This purchase order is not linked to a project."}
            )
        if target_object.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
            raise serializers.ValidationError({expected_field: "Change notices cannot be booked in timesheets."})
        if not _user_is_project_team_member(user, project):
            raise serializers.ValidationError(
                {
                    expected_field: (
                        "You must be a team member of this purchase order's project."
                    )
                }
            )
    elif source_type == "project_phase":
        if not _user_can_access_project(user, target_object.project):
            raise serializers.ValidationError({expected_field: "You do not have access to this project phase."})
    elif source_type == "project_action_item":
        action_project, _, _ = get_related_objects_from_action_items(target_object)
        if not action_project or not _user_can_access_project(user, action_project):
            raise serializers.ValidationError({expected_field: "You do not have access to this project action item."})
    elif source_type == "company_action_item":
        if user.is_freelancer:
            if target_object.freelancer_id != user.id:
                raise serializers.ValidationError({expected_field: "You do not have access to this company action item."})
        else:
            if target_object.company_profile_id != user.company_id:
                raise serializers.ValidationError({expected_field: "You do not have access to this company action item."})
    elif source_type == "company_calendar_event":
        if user.is_freelancer:
            has_access = (
                target_object.freelancer_id == user.id
                and target_object.assignees.filter(id=user.id).exists()
            )
        else:
            has_access = (
                target_object.company_profile_id == user.company_id
                and target_object.assignees.filter(id=user.id).exists()
            )
        if not has_access:
            raise serializers.ValidationError({expected_field: "You do not have access to this company calendar event."})


def _build_timesheet_activity_payload(
    week_start_date,
    week_end_date,
    source_type,
    target_object,
    activity_id=None,
    is_invoiceable=True,
    project_object=None,
    milestone_object=None,
    action_item_object=None,
):
    display_data = _build_timesheet_activity_display_data(
        source_type,
        target_object,
        project_object=project_object,
        milestone_object=milestone_object,
        action_item_object=action_item_object,
    )
    resolved_project = project_object
    if source_type == "project_purchase_order" and not resolved_project:
        resolved_project = getattr(target_object, "project", None)
    payload = {
        "id": activity_id,
        "week_start_date": week_start_date,
        "week_end_date": week_end_date,
        "source_type": source_type,
        "activity_code": display_data["activity_code"],
        "activity_name": display_data["activity_name"],
        "activity_subtitle": display_data["activity_subtitle"],
        "project_milestone_name": display_data["project_milestone_name"],
        "project_action_item_name": display_data["project_action_item_name"],
        "is_invoiceable": is_invoiceable,
        "company_action_item": None,
        "company_calendar_event": None,
        "project": None,
        "purchase_order": None,
        "project_milestone": None,
        "project_action_item": None,
        "entries": [],
        "target": {
            "id": target_object.id,
            "name": getattr(target_object, "name", None)
            or getattr(target_object, "project_name", None)
            or str(target_object),
        },
    }
    payload[TIMESHEET_SOURCE_TO_FIELD[source_type]] = target_object.id
    if resolved_project:
        payload["project"] = resolved_project.id
    if milestone_object and source_type in {
        "project_action_item",
        "project_purchase_order",
    }:
        payload["project_milestone"] = milestone_object.id
    if source_type == "project_purchase_order":
        if action_item_object:
            payload["project_action_item"] = action_item_object.id
    return payload


def _resolve_activity_scope(activity):
    project = _get_project_for_timesheet_activity(activity)
    if project:
        return {
            "scope_type": "project",
            "project_id": project.id if project else None,
            "project_name": project.project_name if project else activity.activity_name,
        }
    return {
        "scope_type": "company",
        "project_id": None,
        "project_name": None,
    }


def _build_monthly_timesheet_preview(user, month, year):
    period_start, period_end = _month_period_bounds(year, month)
    week_starts = _get_week_starts_for_month(year, month)
    expected_daily_hours = _get_expected_daily_hours(user)
    linked_documents_by_signature = _get_monthly_timesheet_document_links_map(
        user,
        month,
        year,
    )

    activities = (
        TimesheetActivity.objects.filter(
            user=user,
            week_start_date__in=week_starts,
        )
        .select_related(
            "company_action_item",
            "company_calendar_event",
            "project",
            "project__client__contact_company",
            "project__client__contact_user",
            "purchase_order",
            "purchase_order__project",
            "purchase_order__project__client__contact_company",
            "purchase_order__project__client__contact_user",
            "project_milestone",
            "project_milestone__project",
            "project_milestone__project__client__contact_company",
            "project_milestone__project__client__contact_user",
            "project_action_item",
            "project_action_item__milestone",
            "project_action_item__milestone__project",
            "project_action_item__milestone__project__client__contact_company",
            "project_action_item__milestone__project__client__contact_user",
            "project_action_item__parent_action_item",
            "project_action_item__parent_action_item__milestone",
            "project_action_item__parent_action_item__milestone__project",
            "project_action_item__parent_action_item__milestone__project__client__contact_company",
            "project_action_item__parent_action_item__milestone__project__client__contact_user",
        )
        .prefetch_related("entries")
        .order_by("week_start_date", "id")
    )
    activities_by_week_start = defaultdict(list)
    for activity in activities:
        activities_by_week_start[activity.week_start_date].append(activity)

    week_sections = []
    month_total_hours = 0.0
    month_under_allocated_days_count = 0
    month_over_allocated_days_count = 0
    submission_scope_projects = {}
    has_company_scope = False

    for week_start_date in week_starts:
        week_end_date = week_start_date + timedelta(days=6)
        locked_days = _get_week_locked_days(user, week_start_date, week_end_date)
        in_month_dates = set(
            _get_week_dates_in_month(week_start_date, week_end_date, year, month)
        )

        date_cells = []
        daily_totals_map = {}
        for offset in range(7):
            current_day = week_start_date + timedelta(days=offset)
            is_in_month = current_day in in_month_dates
            locked_day = locked_days.get(current_day) if is_in_month else None
            daily_totals_map[current_day] = _round_hours((locked_day or {}).get("hours", 0))
            date_cells.append(
                {
                    "date": current_day,
                    "is_in_month": is_in_month,
                    "is_locked": bool(locked_day),
                    "locked_day_detail": locked_day,
                }
            )

        activity_rows = []
        for activity in activities_by_week_start.get(week_start_date, []):
            filtered_entries = []
            row_total = 0.0
            for entry in activity.entries.all():
                if entry.entry_date not in in_month_dates:
                    continue
                if entry.entry_date in locked_days:
                    continue
                entry_hours = _round_hours(entry.hours)
                filtered_entries.append(
                    {
                        "id": entry.id,
                        "entry_date": entry.entry_date,
                        "hours": entry_hours,
                    }
                )
                daily_totals_map[entry.entry_date] = _round_hours(daily_totals_map.get(entry.entry_date, 0) + entry_hours)
                row_total = _round_hours(row_total + entry_hours)

            if row_total <= 0 and not filtered_entries:
                continue

            scope_data = _resolve_activity_scope(activity)
            activity_project = _get_project_for_timesheet_activity(activity)
            row_signature = _build_monthly_activity_row_signature(activity)
            if scope_data["scope_type"] == "company":
                has_company_scope = True
            elif scope_data["project_id"]:
                submission_scope_projects[scope_data["project_id"]] = scope_data["project_name"]

            display_data = _get_timesheet_activity_display_data_from_instance(activity)
            activity_rows.append(
                {
                    "id": activity.id,
                    "source_type": activity.source_type,
                    "activity_code": activity.activity_code,
                    "activity_name": display_data["activity_name"],
                    "activity_subtitle": display_data["activity_subtitle"],
                    "project_milestone_name": display_data["project_milestone_name"],
                    "project_action_item_name": display_data["project_action_item_name"],
                    "is_invoiceable": activity.is_invoiceable,
                    "row_signature": row_signature,
                    "target": {
                        "id": activity.target_object.id if activity.target_object else None,
                        "name": _get_target_display_name(activity.target_object)
                        if activity.target_object
                        else activity.activity_name,
                    } if activity.target_object else None,
                    "client": _build_monthly_client_payload(activity_project),
                    "project": _build_monthly_project_payload(activity_project),
                    "purchase_order": _build_monthly_purchase_order_payload(activity),
                    "linked_documents": linked_documents_by_signature.get(
                        row_signature,
                        [],
                    ),
                    "entries": filtered_entries,
                    "row_total_hours": row_total,
                    "scope": scope_data,
                }
            )

        daily_totals = [
            {
                "date": cell["date"],
                "hours": _round_hours(daily_totals_map.get(cell["date"], 0)),
            }
            for cell in date_cells
            if cell["is_in_month"]
        ]
        daily_warning_metadata = _build_daily_warning_metadata(
            user,
            {
                cell["date"]: daily_totals_map.get(cell["date"], 0)
                for cell in date_cells
                if cell["is_in_month"]
            },
        )
        week_total_hours = _round_hours(sum(item["hours"] for item in daily_totals))
        if week_total_hours <= 0 and not any(activity["row_total_hours"] > 0 for activity in activity_rows):
            continue

        month_total_hours = _round_hours(month_total_hours + week_total_hours)
        month_under_allocated_days_count += daily_warning_metadata["under_allocated_days_count"]
        month_over_allocated_days_count += daily_warning_metadata["over_allocated_days_count"]

        week_sections.append(
            {
                "week_start_date": week_start_date,
                "week_end_date": week_end_date,
                "days": date_cells,
                "activities": activity_rows,
                "daily_totals": daily_totals,
                "daily_warning_map": daily_warning_metadata["daily_warning_map"],
                "week_total_hours": week_total_hours,
                "locked_dates": [
                    cell["date"]
                    for cell in date_cells
                    if cell["is_in_month"] and cell["is_locked"]
                ],
                "locked_day_details": [
                    cell["locked_day_detail"]
                    for cell in date_cells
                    if cell["is_in_month"] and cell["locked_day_detail"]
                ],
            }
        )

    scope_parts = []
    if has_company_scope:
        scope_parts.append("Company")
    if submission_scope_projects:
        scope_parts.append(
            "Projects" if len(submission_scope_projects) > 1 else "Project"
        )

    return {
        "month": month,
        "year": year,
        "period_start": period_start,
        "period_end": period_end,
        "weeks": week_sections,
        "month_total_hours": month_total_hours,
        "expected_daily_hours": expected_daily_hours,
        "under_allocated_days_count": month_under_allocated_days_count,
        "over_allocated_days_count": month_over_allocated_days_count,
        "scope_summary": " + ".join(scope_parts) if scope_parts else "None",
        "project_count": len(submission_scope_projects),
        "has_company_scope": has_company_scope,
    }


class TimesheetEntrySerializer(serializers.ModelSerializer):
    class Meta:
        model = TimesheetEntry
        fields = ["id", "entry_date", "hours"]


class TimesheetEntryPayloadSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    entry_date = serializers.DateField()
    hours = serializers.FloatField()


class TimesheetActivitySerializer(serializers.ModelSerializer):
    entries = TimesheetEntrySerializer(many=True, read_only=True)
    target = serializers.SerializerMethodField()
    activity_name = serializers.SerializerMethodField()
    activity_subtitle = serializers.SerializerMethodField()
    project_milestone_name = serializers.SerializerMethodField()
    project_action_item_name = serializers.SerializerMethodField()

    class Meta:
        model = TimesheetActivity
        fields = [
            "id",
            "week_start_date",
            "week_end_date",
            "source_type",
            "activity_code",
            "activity_name",
            "activity_subtitle",
            "project_milestone_name",
            "project_action_item_name",
            "is_invoiceable",
            "company_action_item",
            "company_calendar_event",
            "project",
            "purchase_order",
            "project_milestone",
            "project_action_item",
            "entries",
            "target",
        ]

    def get_target(self, obj):
        target = obj.target_object
        if not target:
            return None
        return {
            "id": target.id,
            "name": _get_target_display_name(target),
        }

    def get_activity_name(self, obj):
        return _get_timesheet_activity_display_name_from_instance(obj)

    def get_activity_subtitle(self, obj):
        return _get_timesheet_activity_display_subtitle_from_instance(obj)

    def get_project_milestone_name(self, obj):
        return _get_timesheet_activity_display_data_from_instance(obj)[
            "project_milestone_name"
        ]

    def get_project_action_item_name(self, obj):
        return _get_timesheet_activity_display_data_from_instance(obj)[
            "project_action_item_name"
        ]


class TimesheetActivityPayloadSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    week_start_date = serializers.DateField()
    week_end_date = serializers.DateField()
    source_type = serializers.ChoiceField(choices=TimesheetActivity._meta.get_field("source_type").choices)
    activity_code = serializers.CharField()
    activity_name = serializers.CharField()
    activity_subtitle = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    project_milestone_name = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    project_action_item_name = serializers.CharField(
        required=False,
        allow_null=True,
        allow_blank=True,
    )
    is_invoiceable = serializers.BooleanField()
    company_action_item = serializers.IntegerField(required=False, allow_null=True)
    company_calendar_event = serializers.IntegerField(required=False, allow_null=True)
    project = serializers.IntegerField(required=False, allow_null=True)
    purchase_order = serializers.IntegerField(required=False, allow_null=True)
    project_milestone = serializers.IntegerField(required=False, allow_null=True)
    project_action_item = serializers.IntegerField(required=False, allow_null=True)
    entries = TimesheetEntryPayloadSerializer(many=True)
    target = serializers.JSONField(allow_null=True)


class TimesheetWeekReadSerializer(serializers.Serializer):
    week_start_date = serializers.DateField()
    week_end_date = serializers.DateField()
    activities = TimesheetActivityPayloadSerializer(many=True)
    daily_totals = serializers.ListField()
    week_total_hours = serializers.FloatField()
    expected_daily_hours = serializers.FloatField(required=False)
    daily_warning_map = serializers.JSONField(required=False)
    under_allocated_days_count = serializers.IntegerField(required=False)
    over_allocated_days_count = serializers.IntegerField(required=False)
    locked_dates = serializers.ListField()
    locked_day_details = serializers.ListField(required=False)
    can_edit = serializers.BooleanField()
    submission = serializers.JSONField(allow_null=True)
    submissions = serializers.ListField(required=False)


class TimesheetEntryWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    entry_date = serializers.DateField()
    hours = serializers.FloatField()

    def validate_hours(self, value):
        if value < 0:
            raise serializers.ValidationError("Hours cannot be negative.")
        return value


class TimesheetActivityWriteSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False)
    source_type = serializers.ChoiceField(choices=TimesheetActivity._meta.get_field("source_type").choices)
    is_invoiceable = serializers.BooleanField(required=False)
    company_action_item = serializers.IntegerField(required=False, allow_null=True)
    company_calendar_event = serializers.IntegerField(required=False, allow_null=True)
    project = serializers.IntegerField(required=False, allow_null=True)
    purchase_order = serializers.IntegerField(required=False, allow_null=True)
    project_milestone = serializers.IntegerField(required=False, allow_null=True)
    project_action_item = serializers.IntegerField(required=False, allow_null=True)
    entries = TimesheetEntryWriteSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        request = self.context["request"]
        user = request.user
        source_type = attrs["source_type"]
        project_object = None
        if attrs.get("project"):
            project_object = Project.objects.filter(id=attrs["project"]).first()
            if not project_object:
                raise serializers.ValidationError(
                    {"project": "Selected project does not exist."}
                )

        purchase_order_object = None
        if (
            source_type == "project_purchase_order"
            and attrs.get("purchase_order")
            and project_object
        ):
            purchase_order_object = (
                _get_eligible_timesheet_purchase_orders_queryset(
                    project_object,
                    user,
                )
                .filter(id=attrs["purchase_order"])
                .first()
            )
            if not purchase_order_object and attrs.get("id"):
                # Preserve an existing PO link when permissions changed after
                # the activity was created; changing the link still requires access.
                existing_activity = (
                    TimesheetActivity.objects.select_related("purchase_order")
                    .filter(
                        id=attrs["id"],
                        user=user,
                        source_type="project_purchase_order",
                        project_id=project_object.id,
                        purchase_order_id=attrs["purchase_order"],
                    )
                    .first()
                )
                if existing_activity:
                    purchase_order_object = existing_activity.purchase_order

        milestone_object = None
        if attrs.get("project_milestone"):
            milestone_object = Milestone.objects.filter(
                id=attrs["project_milestone"]
            ).first()
            if not milestone_object:
                raise serializers.ValidationError(
                    {"project_milestone": "Selected project phase does not exist."}
                )

        action_item_object = None
        if attrs.get("project_action_item"):
            action_item_object = ActionItem.objects.filter(
                id=attrs["project_action_item"]
            ).first()
            if not action_item_object:
                raise serializers.ValidationError(
                    {"project_action_item": "Selected project action item does not exist."}
                )

        if source_type == "company_action_item":
            target_object = TIMESHEET_TARGET_FIELD_MODEL_MAP["company_action_item"].objects.filter(
                id=attrs.get("company_action_item")
            ).first()
            if not target_object:
                raise serializers.ValidationError(
                    {"company_action_item": "Selected target does not exist."}
                )
            if any(
                [
                    attrs.get("company_calendar_event"),
                    attrs.get("project"),
                    attrs.get("purchase_order"),
                    attrs.get("project_milestone"),
                    attrs.get("project_action_item"),
                ]
            ):
                raise serializers.ValidationError(
                    "Company action item timesheet activity cannot reference project-related targets."
                )
            _validate_timesheet_target_access(
                user,
                source_type,
                target_object,
                "company_action_item",
            )

        elif source_type == "company_calendar_event":
            target_object = TIMESHEET_TARGET_FIELD_MODEL_MAP["company_calendar_event"].objects.filter(
                id=attrs.get("company_calendar_event")
            ).first()
            if not target_object:
                raise serializers.ValidationError(
                    {"company_calendar_event": "Selected target does not exist."}
                )
            if any(
                [
                    attrs.get("company_action_item"),
                    attrs.get("project"),
                    attrs.get("purchase_order"),
                    attrs.get("project_milestone"),
                    attrs.get("project_action_item"),
                ]
            ):
                raise serializers.ValidationError(
                    "Company calendar event timesheet activity cannot reference project-related targets."
                )
            _validate_timesheet_target_access(
                user,
                source_type,
                target_object,
                "company_calendar_event",
            )

        elif source_type == "project":
            if not project_object:
                raise serializers.ValidationError(
                    {"project": "Selected project does not exist."}
                )
            if any(
                [
                    attrs.get("purchase_order"),
                    attrs.get("project_milestone"),
                    attrs.get("project_action_item"),
                    attrs.get("company_action_item"),
                    attrs.get("company_calendar_event"),
                ]
            ):
                raise serializers.ValidationError(
                    "Project timesheet activity cannot reference other timesheet targets."
                )
            target_object = project_object
            _validate_timesheet_target_access(user, source_type, target_object, "project")

        elif source_type == "project_phase":
            if not milestone_object:
                raise serializers.ValidationError(
                    {"project_milestone": "Selected project phase does not exist."}
                )
            if any(
                [
                    attrs.get("purchase_order"),
                    attrs.get("project_action_item"),
                    attrs.get("company_action_item"),
                    attrs.get("company_calendar_event"),
                ]
            ):
                raise serializers.ValidationError(
                    "Project phase timesheet activity cannot reference other timesheet targets."
                )
            if project_object and milestone_object.project_id != project_object.id:
                raise serializers.ValidationError(
                    {
                        "project_milestone": (
                            "Selected project phase must belong to the selected project."
                        )
                    }
                )
            target_object = milestone_object
            _validate_timesheet_target_access(
                user,
                source_type,
                target_object,
                "project_milestone",
            )

        elif source_type == "project_action_item":
            if not action_item_object:
                raise serializers.ValidationError(
                    {"project_action_item": "Selected project action item does not exist."}
                )
            if any(
                [
                    attrs.get("purchase_order"),
                    attrs.get("company_action_item"),
                    attrs.get("company_calendar_event"),
                ]
            ):
                raise serializers.ValidationError(
                    "Project action item timesheet activity cannot reference a purchase order or company targets."
                )
            action_item_project, action_item_milestone = _get_action_item_project_context(
                action_item_object
            )
            if not action_item_project:
                raise serializers.ValidationError(
                    {
                        "project_action_item": (
                            "Selected project action item must belong to a project."
                        )
                    }
                )
            if project_object and action_item_project.id != project_object.id:
                raise serializers.ValidationError(
                    {
                        "project_action_item": (
                            "Selected project action item must belong to the selected project."
                        )
                    }
                )
            if (
                milestone_object
                and action_item_milestone
                and action_item_milestone.id != milestone_object.id
            ):
                raise serializers.ValidationError(
                    {
                        "project_action_item": (
                            "Selected project action item must belong to the selected project phase."
                        )
                    }
                )
            target_object = action_item_object
            _validate_timesheet_target_access(
                user,
                source_type,
                target_object,
                "project_action_item",
            )

        elif source_type == "project_purchase_order":
            if not project_object:
                raise serializers.ValidationError(
                    {
                        "project": (
                            "Project is required for a purchase order timesheet activity."
                        )
                    }
                )
            if not purchase_order_object:
                raise serializers.ValidationError(
                    {
                        "purchase_order": (
                            "Selected purchase order does not exist."
                        )
                    }
                )
            if any(
                [
                    attrs.get("company_action_item"),
                    attrs.get("company_calendar_event"),
                ]
            ):
                raise serializers.ValidationError(
                    "Project purchase order timesheet activity cannot reference company targets."
                )
            if purchase_order_object.project_id != project_object.id:
                raise serializers.ValidationError(
                    {
                        "purchase_order": (
                            "Selected purchase order must belong to the selected project."
                        )
                    }
                )
            if milestone_object and milestone_object.project_id != project_object.id:
                raise serializers.ValidationError(
                    {
                        "project_milestone": (
                            "Selected project phase must belong to the selected project."
                        )
                    }
                )
            if action_item_object:
                action_item_project, action_item_milestone = _get_action_item_project_context(
                    action_item_object
                )
                if not action_item_project or action_item_project.id != project_object.id:
                    raise serializers.ValidationError(
                        {
                            "project_action_item": (
                                "Selected project action item must belong to the selected project."
                            )
                        }
                    )
                if (
                    milestone_object
                    and action_item_milestone
                    and action_item_milestone.id != milestone_object.id
                ):
                    raise serializers.ValidationError(
                        {
                            "project_action_item": (
                                "Selected project action item must belong to the selected project phase."
                            )
                        }
                    )
            target_object = purchase_order_object
            _validate_timesheet_target_access(
                user,
                source_type,
                target_object,
                "purchase_order",
            )
            attrs["project_object"] = project_object
        else:
            raise serializers.ValidationError("Invalid timesheet activity source type.")

        entry_dates = set()
        for entry in attrs.get("entries", []):
            if entry["entry_date"] in entry_dates:
                raise serializers.ValidationError(
                    {"entries": "Duplicate entry dates are not allowed within the same activity."}
                )
            entry_dates.add(entry["entry_date"])

        attrs["target_object"] = target_object
        attrs["company_action_item_object"] = (
            target_object if source_type == "company_action_item" else None
        )
        attrs["company_calendar_event_object"] = (
            target_object if source_type == "company_calendar_event" else None
        )
        attrs["project_object"] = project_object
        attrs["purchase_order_object"] = purchase_order_object
        attrs["project_milestone_object"] = milestone_object
        attrs["project_action_item_object"] = action_item_object
        return attrs


class TimesheetActivityDraftCreateSerializer(TimesheetActivityWriteSerializer):
    week_start_date = serializers.DateField()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        week_start_date = attrs["week_start_date"]
        week_end_date = week_start_date + timezone.timedelta(days=6)
        _validate_week_is_editable(request.user, week_start_date, week_end_date)
        attrs["week_end_date"] = week_end_date
        return attrs

    def build_activity_payload(self):
        payload = _build_timesheet_activity_payload(
            self.validated_data["week_start_date"],
            self.validated_data["week_end_date"],
            self.validated_data["source_type"],
            self.validated_data["target_object"],
            is_invoiceable=self.validated_data.get("is_invoiceable", True),
            project_object=self.validated_data.get("project_object"),
            milestone_object=self.validated_data.get("project_milestone_object"),
            action_item_object=self.validated_data.get("project_action_item_object"),
        )
        if self.validated_data["source_type"] == "project_purchase_order":
            if self.validated_data.get("project_milestone_object"):
                payload["project_milestone"] = self.validated_data[
                    "project_milestone_object"
                ].id
            if self.validated_data.get("project_action_item_object"):
                payload["project_action_item"] = self.validated_data[
                    "project_action_item_object"
                ].id
        return payload


class TimesheetWeekBaseDraftSerializer(serializers.Serializer):
    week_start_date = serializers.DateField()

    def validate(self, attrs):
        request = self.context["request"]
        week_start_date = attrs["week_start_date"]
        week_end_date = week_start_date + timezone.timedelta(days=6)
        _validate_week_is_editable(request.user, week_start_date, week_end_date)
        attrs["week_end_date"] = week_end_date
        return attrs

    def _get_current_week_target_signatures(self, user):
        return set(
            TimesheetActivity.objects.filter(
                user=user,
                week_start_date=self.validated_data["week_start_date"],
                week_end_date=self.validated_data["week_end_date"],
            )
            .exclude(
                company_action_item__isnull=True,
                company_calendar_event__isnull=True,
                project__isnull=True,
                purchase_order__isnull=True,
                project_milestone__isnull=True,
                project_action_item__isnull=True,
            )
            .values_list(
                "source_type",
                "company_action_item_id",
                "company_calendar_event_id",
                "project_id",
                "purchase_order_id",
                "project_milestone_id",
                "project_action_item_id",
            )
        )

    def _build_signature(self, activity):
        return (
            activity.source_type,
            activity.company_action_item_id,
            activity.company_calendar_event_id,
            activity.project_id,
            activity.purchase_order_id,
            activity.project_milestone_id,
            activity.project_action_item_id,
        )


class TimesheetCopyPreviousWeekSerializer(TimesheetWeekBaseDraftSerializer):
    def build_activity_payloads(self):
        user = self.context["request"].user
        week_start_date = self.validated_data["week_start_date"]
        week_end_date = self.validated_data["week_end_date"]
        previous_week_start = week_start_date - timezone.timedelta(days=7)
        previous_week_end = week_end_date - timezone.timedelta(days=7)

        existing_signatures = self._get_current_week_target_signatures(user)
        copied_signatures = set()
        payloads = []

        previous_activities = (
            TimesheetActivity.objects.filter(
                user=user,
                week_start_date=previous_week_start,
                week_end_date=previous_week_end,
            )
            .select_related(
                "company_action_item",
                "company_calendar_event",
                "project",
                "purchase_order",
                "project_milestone",
                "project_action_item",
            )
            .order_by("id")
        )

        for activity in previous_activities:
            target_object = activity.target_object
            if not target_object:
                continue
            signature = self._build_signature(activity)
            if signature in existing_signatures or signature in copied_signatures:
                continue
            payloads.append(
                _build_timesheet_activity_payload(
                    week_start_date,
                    week_end_date,
                    activity.source_type,
                    target_object,
                    is_invoiceable=activity.is_invoiceable,
                    project_object=activity.project,
                    milestone_object=activity.project_milestone,
                    action_item_object=activity.project_action_item,
                )
            )
            copied_signatures.add(signature)

        return payloads


class TimesheetPlannedActivitiesSerializer(TimesheetWeekBaseDraftSerializer):
    project_id = serializers.IntegerField(required=False, min_value=1)
    purchase_order_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )
    project_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        user = request.user

        project_id = attrs.get("project_id")
        purchase_order_ids = list(dict.fromkeys(attrs.get("purchase_order_ids", [])))
        project_ids = list(dict.fromkeys(attrs.get("project_ids", [])))

        if project_id and project_ids:
            raise serializers.ValidationError(
                {"project_id": "Use either project_id with purchase_order_ids or project_ids, not both."}
            )

        if project_id:
            project = Project.objects.filter(id=project_id).first()
            if not project:
                raise serializers.ValidationError({"project_id": "Selected project does not exist."})
            if not purchase_order_ids:
                raise serializers.ValidationError(
                    {"purchase_order_ids": "Select at least one purchase order."}
                )

            eligible_purchase_orders = list(
                _get_eligible_timesheet_purchase_orders_queryset(project, user).filter(
                    id__in=purchase_order_ids
                )
            )
            eligible_purchase_order_ids = {po.id for po in eligible_purchase_orders}
            invalid_purchase_order_ids = [
                purchase_order_id
                for purchase_order_id in purchase_order_ids
                if purchase_order_id not in eligible_purchase_order_ids
            ]
            if invalid_purchase_order_ids:
                raise serializers.ValidationError(
                    {
                        "purchase_order_ids": (
                            "One or more selected purchase orders are invalid, inaccessible, "
                            "or not eligible for planned activities."
                        )
                    }
                )

            attrs["planned_project"] = project
            attrs["purchase_order_ids"] = purchase_order_ids
            attrs["purchase_orders_by_id"] = {
                purchase_order.id: purchase_order
                for purchase_order in eligible_purchase_orders
            }
            attrs["planned_mode"] = "purchase_orders"
            return attrs

        projects = list(Project.objects.filter(id__in=project_ids))

        if len(projects) != len(project_ids):
            raise serializers.ValidationError({"project_ids": "One or more selected projects do not exist."})

        inaccessible_project_ids = [
            project.id
            for project in projects
            if not _user_can_access_project(user, project)
        ]
        if inaccessible_project_ids:
            raise serializers.ValidationError(
                {"project_ids": f"You do not have access to project(s): {', '.join(map(str, inaccessible_project_ids))}."}
            )

        attrs["project_ids"] = project_ids
        attrs["projects_by_id"] = {project.id: project for project in projects}
        attrs["planned_mode"] = "projects"
        return attrs

    def build_activity_payloads(self):
        week_start_date = self.validated_data["week_start_date"]
        week_end_date = self.validated_data["week_end_date"]
        current_signatures = self._get_current_week_target_signatures(
            self.context["request"].user
        )
        payloads = []
        if self.validated_data.get("planned_mode") == "purchase_orders":
            project = self.validated_data["planned_project"]
            purchase_orders_by_id = self.validated_data["purchase_orders_by_id"]
            for purchase_order_id in self.validated_data["purchase_order_ids"]:
                purchase_order = purchase_orders_by_id.get(purchase_order_id)
                if not purchase_order:
                    continue
                signature = (
                    "project_purchase_order",
                    None,
                    None,
                    project.id,
                    purchase_order.id,
                    None,
                    None,
                )
                if signature in current_signatures:
                    continue
                payloads.append(
                    _build_timesheet_activity_payload(
                        week_start_date,
                        week_end_date,
                        "project_purchase_order",
                        purchase_order,
                        is_invoiceable=True,
                        project_object=project,
                    )
                )
            return payloads

        project_ids = self.validated_data["project_ids"]
        projects_by_id = self.validated_data["projects_by_id"]
        for project_id in project_ids:
            project = projects_by_id.get(project_id)
            if not project:
                continue
            signature = (
                "project",
                None,
                None,
                project.id,
                None,
                None,
                None,
            )
            if signature in current_signatures:
                continue
            payloads.append(
                _build_timesheet_activity_payload(
                    week_start_date,
                    week_end_date,
                    "project",
                    project,
                    is_invoiceable=True,
                )
            )

        return payloads


class TimesheetWeekSaveSerializer(serializers.Serializer):
    week_start_date = serializers.DateField()
    activities = TimesheetActivityWriteSerializer(many=True, required=False, default=list)

    def validate(self, attrs):
        request = self.context["request"]
        user = request.user
        week_start_date = attrs["week_start_date"]
        week_end_date = week_start_date + timezone.timedelta(days=6)
        attrs["week_end_date"] = week_end_date

        _validate_week_is_editable(user, week_start_date, week_end_date)
        locked_days = _get_week_locked_days(user, week_start_date, week_end_date)
        monthly_submission_locked_days = _get_week_month_submission_locks(
            user,
            week_start_date,
            week_end_date,
        )

        existing_activities = list(
            TimesheetActivity.objects.filter(
                user=user,
                week_start_date=week_start_date,
                week_end_date=week_end_date,
            ).prefetch_related("entries")
        )
        existing_activities_by_id = {activity.id: activity for activity in existing_activities}
        existing_activity_ids = set(existing_activities_by_id.keys())

        seen_activity_ids = set()
        seen_target_signatures = set()
        daily_totals = {}

        for index, activity in enumerate(attrs.get("activities", [])):
            activity_id = activity.get("id")
            if activity_id:
                if activity_id not in existing_activity_ids:
                    raise serializers.ValidationError(
                        {"activities": {index: {"id": "Activity does not belong to this week."}}}
                    )
                if activity_id in seen_activity_ids:
                    raise serializers.ValidationError(
                        {"activities": {index: {"id": "Duplicate activity id in payload."}}}
                    )
                seen_activity_ids.add(activity_id)

            target_signature = (
                activity["source_type"],
                activity.get("company_action_item_object").id
                if activity.get("company_action_item_object")
                else None,
                activity.get("company_calendar_event_object").id
                if activity.get("company_calendar_event_object")
                else None,
                activity.get("project_object").id
                if activity.get("project_object")
                else None,
                activity.get("purchase_order_object").id
                if activity.get("purchase_order_object")
                else None,
                activity.get("project_milestone_object").id
                if activity.get("project_milestone_object")
                else None,
                activity.get("project_action_item_object").id
                if activity.get("project_action_item_object")
                else None,
            )
            if target_signature in seen_target_signatures:
                raise serializers.ValidationError(
                    {"activities": {index: "Duplicate activity target in the same week is not allowed."}}
                )
            seen_target_signatures.add(target_signature)

            submitted_entries_by_date = {
                entry["entry_date"]: float(entry["hours"] or 0)
                for entry in activity.get("entries", [])
            }

            if activity_id:
                existing_activity = existing_activities_by_id[activity_id]
                existing_locked_entries = {
                    entry.entry_date: float(entry.hours or 0)
                    for entry in existing_activity.entries.all()
                    if entry.entry_date in monthly_submission_locked_days
                }
                for locked_date, existing_hours in existing_locked_entries.items():
                    submitted_hours = submitted_entries_by_date.get(locked_date)
                    if submitted_hours is None or _round_hours(submitted_hours) != _round_hours(existing_hours):
                        raise serializers.ValidationError(
                            {
                                "activities": {
                                    index: {
                                        "entries": "Locked monthly timesheet dates cannot be changed or removed."
                                    }
                                }
                            }
                        )
            elif any(
                entry_date in monthly_submission_locked_days and hours > 0
                for entry_date, hours in submitted_entries_by_date.items()
            ):
                raise serializers.ValidationError(
                    {
                        "activities": {
                            index: {
                                "entries": "Locked monthly timesheet dates cannot accept new time entries."
                            }
                        }
                    }
                )

            for entry_index, entry in enumerate(activity.get("entries", [])):
                entry_date = entry["entry_date"]
                if entry_date < week_start_date or entry_date > week_end_date:
                    raise serializers.ValidationError(
                        {
                            "activities": {
                                index: {
                                    "entries": {
                                        entry_index: {
                                            "entry_date": "Entry date must fall within the selected week."
                                        }
                                    }
                                }
                            }
                        }
                    )
                if entry_date in locked_days and float(entry["hours"] or 0) > 0:
                    raise serializers.ValidationError(
                        {
                            "activities": {
                                index: {
                                    "entries": {
                                        entry_index: {
                                            "entry_date": "This day is locked and cannot accept time entries."
                                        }
                                    }
                                }
                            }
                        }
                    )
                if (
                    entry_date in monthly_submission_locked_days
                    and not activity_id
                    and float(entry["hours"] or 0) > 0
                ):
                    raise serializers.ValidationError(
                        {
                            "activities": {
                                index: {
                                    "entries": {
                                        entry_index: {
                                            "entry_date": "This day belongs to a locked monthly timesheet and cannot accept new time entries."
                                        }
                                    }
                                }
                            }
                        }
                    )
                daily_totals[entry_date] = daily_totals.get(entry_date, 0) + float(entry["hours"] or 0)

        for existing_activity in existing_activities:
            if existing_activity.id in seen_activity_ids:
                continue
            if any(
                entry.entry_date in monthly_submission_locked_days
                and float(entry.hours or 0) > 0
                for entry in existing_activity.entries.all()
            ):
                raise serializers.ValidationError(
                    {
                        "activities": "Activities containing locked monthly timesheet dates cannot be removed."
                    }
                )

        day_errors = {}
        for day, total in daily_totals.items():
            if total > 24:
                day_errors[day.isoformat()] = ["Daily total cannot exceed 24 hours."]

        if day_errors:
            raise serializers.ValidationError({"day_errors": day_errors})

        return attrs

    def save(self, **kwargs):
        request = self.context["request"]
        user = kwargs.get("user", request.user)
        week_start_date = self.validated_data["week_start_date"]
        week_end_date = self.validated_data["week_end_date"]
        activities_data = self.validated_data.get("activities", [])

        existing_activities = {
            activity.id: activity
            for activity in TimesheetActivity.objects.filter(
                user=user,
                week_start_date=week_start_date,
                week_end_date=week_end_date,
            ).prefetch_related("entries")
        }

        retained_activity_ids = set()

        with transaction.atomic():
            for activity_data in activities_data:
                activity_id = activity_data.get("id")
                entries_data = activity_data.get("entries", [])
                target_object = activity_data["target_object"]
                snapshot = _build_timesheet_activity_snapshot(
                    activity_data["source_type"],
                    target_object,
                )

                target_fields = {
                    "company_action_item": None,
                    "company_calendar_event": None,
                    "project": None,
                    "purchase_order": None,
                    "project_milestone": None,
                    "project_action_item": None,
                }
                target_fields[TIMESHEET_SOURCE_TO_FIELD[activity_data["source_type"]]] = target_object
                if activity_data["source_type"] == "project_purchase_order":
                    target_fields["project"] = activity_data["project_object"]
                    target_fields["project_milestone"] = activity_data.get(
                        "project_milestone_object"
                    )
                    target_fields["project_action_item"] = activity_data.get(
                        "project_action_item_object"
                    )

                if activity_id:
                    activity = existing_activities[activity_id]
                    for field_name, value in target_fields.items():
                        setattr(activity, field_name, value)
                    activity.source_type = snapshot["source_type"]
                    activity.activity_code = snapshot["activity_code"]
                    activity.activity_name = snapshot["activity_name"]
                    if "is_invoiceable" in activity_data:
                        activity.is_invoiceable = activity_data["is_invoiceable"]
                    activity.week_start_date = week_start_date
                    activity.week_end_date = week_end_date
                    activity.save()
                else:
                    activity = TimesheetActivity.objects.create(
                        user=user,
                        week_start_date=week_start_date,
                        week_end_date=week_end_date,
                        created_by=user,
                        source_type=snapshot["source_type"],
                        activity_code=snapshot["activity_code"],
                        activity_name=snapshot["activity_name"],
                        is_invoiceable=activity_data.get("is_invoiceable", True),
                        **target_fields,
                    )

                retained_activity_ids.add(activity.id)

                incoming_dates = set()
                for entry_data in entries_data:
                    incoming_dates.add(entry_data["entry_date"])
                    if float(entry_data["hours"] or 0) <= 0:
                        activity.entries.filter(entry_date=entry_data["entry_date"]).delete()
                        continue
                    TimesheetEntry.objects.update_or_create(
                        activity=activity,
                        entry_date=entry_data["entry_date"],
                        defaults={"hours": entry_data["hours"]},
                    )

                activity.entries.exclude(entry_date__in=incoming_dates).delete()

            TimesheetActivity.objects.filter(
                user=user,
                week_start_date=week_start_date,
                week_end_date=week_end_date,
            ).exclude(id__in=retained_activity_ids).delete()

        return {
            "week_start_date": week_start_date,
            "week_end_date": week_end_date,
        }


class MonthlyTimesheetFeedbackSerializer(serializers.ModelSerializer):
    author = serializers.SerializerMethodField()

    class Meta:
        model = MonthlyTimesheetFeedback
        fields = ["id", "feedback_type", "comment", "created_at", "updated_at", "author"]

    def get_author(self, obj):
        if not obj.author:
            return None
        return {
            "id": obj.author.id,
            "name": obj.author.full_name,
        }


class MonthlyTimesheetSubmissionSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    reviewed_by = serializers.SerializerMethodField()
    feedback_entries = MonthlyTimesheetFeedbackSerializer(many=True, read_only=True)
    total_hours = serializers.SerializerMethodField()
    scope_summary = serializers.SerializerMethodField()
    last_feedback = serializers.SerializerMethodField()
    is_invoiced = serializers.SerializerMethodField()
    has_company_scope = serializers.SerializerMethodField()
    has_project_scope = serializers.SerializerMethodField()

    class Meta:
        model = MonthlyTimesheetSubmission
        fields = [
            "id",
            "user",
            "month",
            "year",
            "status",
            "submitted_at",
            "reviewed_at",
            "reviewed_by",
            "created_at",
            "updated_at",
            "total_hours",
            "scope_summary",
            "last_feedback",
            "feedback_entries",
            "is_invoiced",
            "has_company_scope",
            "has_project_scope",
        ]

    def get_reviewed_by(self, obj):
        if not obj.reviewed_by:
            return None
        return {
            "id": obj.reviewed_by.id,
            "name": obj.reviewed_by.full_name,
        }

    def get_user(self, obj):
        return {
            "id": obj.user.id,
            "name": obj.user.full_name,
            "email": obj.user.email,
        }

    def get_total_hours(self, obj):
        preview = self.context.get("preview_by_submission_id", {}).get(obj.id)
        if preview is None:
            preview = _build_monthly_timesheet_preview(obj.user, obj.month, obj.year)
        return preview["month_total_hours"]

    def get_scope_summary(self, obj):
        preview = self.context.get("preview_by_submission_id", {}).get(obj.id)
        if preview is None:
            preview = _build_monthly_timesheet_preview(obj.user, obj.month, obj.year)
        return preview["scope_summary"]

    def get_last_feedback(self, obj):
        feedback = obj.feedback_entries.order_by("-created_at", "-id").first()
        if not feedback:
            return None
        return {
            "id": feedback.id,
            "feedback_type": feedback.feedback_type,
            "comment": feedback.comment,
            "created_at": feedback.created_at,
        }

    def get_is_invoiced(self, obj):
        return obj.id in self.context.get("invoiced_submission_ids", set())

    def get_has_company_scope(self, obj):
        preview = self.context.get("preview_by_submission_id", {}).get(obj.id)
        if preview is None:
            preview = _build_monthly_timesheet_preview(obj.user, obj.month, obj.year)
        return bool(preview["has_company_scope"])

    def get_has_project_scope(self, obj):
        preview = self.context.get("preview_by_submission_id", {}).get(obj.id)
        if preview is None:
            preview = _build_monthly_timesheet_preview(obj.user, obj.month, obj.year)
        return bool(preview["project_count"])


class MonthlyTimesheetPreviewRequestSerializer(serializers.Serializer):
    month = serializers.IntegerField(min_value=1, max_value=12)
    year = serializers.IntegerField(min_value=2000, max_value=2100)

    def validate(self, attrs):
        _month_period_bounds(attrs["year"], attrs["month"])
        if _is_future_month_period(attrs["month"], attrs["year"]):
            raise serializers.ValidationError(
                "Future monthly timesheets cannot be generated or submitted."
            )
        return attrs


class MonthlyTimesheetSubmissionActionSerializer(MonthlyTimesheetPreviewRequestSerializer):
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class MonthlyTimesheetReviewSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, allow_null=True)


class MonthlyTimesheetActivityDocumentsSerializer(serializers.Serializer):
    month = serializers.IntegerField(min_value=1, max_value=12)
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    row_signature = serializers.CharField()
    document_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
    )

    def validate(self, attrs):
        _month_period_bounds(attrs["year"], attrs["month"])

        request = self.context.get("request")
        user = request.user
        preview = _build_monthly_timesheet_preview(
            user,
            attrs["month"],
            attrs["year"],
        )
        monthly_rows = [
            activity
            for week in preview.get("weeks", [])
            for activity in week.get("activities", [])
        ]
        row_payload = next(
            (
                activity
                for activity in monthly_rows
                if activity.get("row_signature") == attrs["row_signature"]
            ),
            None,
        )
        if not row_payload:
            raise serializers.ValidationError(
                {"row_signature": "Selected monthly activity row does not exist."}
            )

        document_ids = list(dict.fromkeys(attrs.get("document_ids", [])))
        documents = list(
            Document.objects.filter(id__in=document_ids).select_related(
                "folder",
                "folder__project",
                "folder__company_profile",
                "folder__freelancer",
                "author",
                "created_by",
                "assignee",
            )
        )
        found_ids = {document.id for document in documents}
        missing_ids = [document_id for document_id in document_ids if document_id not in found_ids]
        if missing_ids:
            raise serializers.ValidationError(
                {"document_ids": f"Documents not found: {', '.join(map(str, missing_ids))}."}
            )

        row_project_id = (row_payload.get("project") or {}).get("id")
        for document in documents:
            if row_project_id:
                if document.folder.project_id != row_project_id:
                    raise serializers.ValidationError(
                        {
                            "document_ids": (
                                f"Document {document.file_name} does not belong to the selected project."
                            )
                        }
                    )
                continue

            if user.is_freelancer:
                if document.folder.freelancer_id != user.id:
                    raise serializers.ValidationError(
                        {
                            "document_ids": (
                                f"Document {document.file_name} does not belong to this freelancer."
                            )
                        }
                    )
            elif document.folder.company_profile_id != user.company_id:
                raise serializers.ValidationError(
                    {
                        "document_ids": (
                            f"Document {document.file_name} does not belong to this company."
                        )
                    }
                )

        attrs["document_ids"] = document_ids
        attrs["documents"] = documents
        attrs["row_payload"] = row_payload
        attrs["row_target_ids"] = _extract_monthly_activity_link_target_ids(
            row_payload,
        )
        return attrs

    def save(self, **kwargs):
        user = kwargs["user"]
        month = self.validated_data["month"]
        year = self.validated_data["year"]
        row_signature = self.validated_data["row_signature"]
        document_ids = self.validated_data["document_ids"]
        documents_by_id = {
            document.id: document for document in self.validated_data["documents"]
        }
        row_target_ids = self.validated_data["row_target_ids"]

        existing_links = MonthlyTimesheetActivityDocumentLink.objects.filter(
            user=user,
            month=month,
            year=year,
            row_signature=row_signature,
        )
        existing_document_ids = set(
            existing_links.values_list("document_id", flat=True)
        )

        existing_links.exclude(document_id__in=document_ids).delete()

        missing_document_ids = [
            document_id
            for document_id in document_ids
            if document_id not in existing_document_ids
        ]
        if missing_document_ids:
            MonthlyTimesheetActivityDocumentLink.objects.bulk_create(
                [
                    MonthlyTimesheetActivityDocumentLink(
                        user=user,
                        month=month,
                        year=year,
                        row_signature=row_signature,
                        source_type=row_target_ids["source_type"],
                        company_action_item_id=row_target_ids["company_action_item_id"],
                        company_calendar_event_id=row_target_ids["company_calendar_event_id"],
                        project_id=row_target_ids["project_id"],
                        purchase_order_id=row_target_ids["purchase_order_id"],
                        project_milestone_id=row_target_ids["project_milestone_id"],
                        project_action_item_id=row_target_ids["project_action_item_id"],
                        document=documents_by_id[document_id],
                        is_invoiceable=row_target_ids["is_invoiceable"],
                    )
                    for document_id in missing_document_ids
                ]
            )

        return [
            documents_by_id[document_id]
            for document_id in document_ids
            if document_id in documents_by_id
        ]


class MonthlyTimesheetPreviewSerializer(serializers.Serializer):
    submission = MonthlyTimesheetSubmissionSerializer(allow_null=True)
    month = serializers.IntegerField()
    year = serializers.IntegerField()
    period_start = serializers.DateField()
    period_end = serializers.DateField()
    scope_summary = serializers.CharField()
    project_count = serializers.IntegerField()
    has_company_scope = serializers.BooleanField()
    month_total_hours = serializers.FloatField()
    expected_daily_hours = serializers.FloatField(required=False)
    under_allocated_days_count = serializers.IntegerField(required=False)
    over_allocated_days_count = serializers.IntegerField(required=False)
    weeks = serializers.ListField()


class TimeCardSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    approved_by = serializers.SerializerMethodField()
    rejected_by = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = TimeCard
        fields = '__all__'
    
    def get_created_by(self, obj):
        if obj.created_by:
            return get_related_user_data(obj, obj.created_by)
        return None
    
    def get_approved_by(self, obj):
        if obj.approved_by:
            return get_related_user_data(obj, obj.approved_by)
        return None

    def validate(self, data):
        request = self.context.get('request')
        request_user = request.user

        # Use instance values for partial updates
        project = data.get('project', getattr(self.instance, 'project', None))
        company_profile = data.get('company_profile', getattr(self.instance, 'company_profile', None))
        freelancer = data.get('freelancer', getattr(self.instance, 'freelancer', None))
        milestone = data.get('milestone', getattr(self.instance, 'milestone', None))
        action_item = data.get('action_item', getattr(self.instance, 'action_item', None))
        category = data.get('category', getattr(self.instance, 'category', None))

        # Ensure only one of project or (user/company_profile) is set
        if project and (freelancer or company_profile):
            raise serializers.ValidationError(
                "TimeCard can have either a project or a freelancer/company profile, not both."
            )

        if not project and not (freelancer or company_profile):
            raise serializers.ValidationError(
                "TimeCard must have either a project or a freelancer/company profile."
            )

        # Project-specific validation
        if project:
            # Check if request user has access to the project
            # TODO: Adjust as per new permission system
            # if not has_project_permission("GET", request_user, project, get_product_name_from_request(request)):
            #     raise serializers.ValidationError("You do not have access to this project.")

            if project.solution in SPECIAL_SOLUTION:
                if milestone or action_item:
                    raise serializers.ValidationError(
                        "TimeCard with a project can not associate a milestone or an action item."
                    )

            # Milestone or ActionItem validation
            if milestone and action_item:
                raise serializers.ValidationError(
                    "TimeCard with a project can associate either a milestone or an action item, not both."
                )

            if milestone and milestone.project != project:
                raise serializers.ValidationError("Milestone must belong to the associated project.")

            if action_item:
                action_project, action_company_profile, action_freelancer = get_related_objects_from_action_items(
                    action_item)
                if project != action_project:
                    raise serializers.ValidationError("Action item must belong to the associated project.")

        # User or CompanyProfile-specific validation
        if freelancer or company_profile:
            if milestone:
                raise serializers.ValidationError(
                    "TimeCard with a freelancer or company profile can only associate an action item, not a milestone."
                )
            if company_profile and request_user.company != company_profile:
                raise serializers.ValidationError("You do not have permission to add time in this company.")
            if freelancer and freelancer != request_user:
                raise serializers.ValidationError("You do not have permission to add time in this freelancer.")

            # Ensure action item's user or company_profile matches the one in the request
            if action_item:
                action_project, action_company_profile, action_freelancer = get_related_objects_from_action_items(
                    action_item)
                if freelancer and action_freelancer != freelancer:
                    raise serializers.ValidationError(
                        "ActionItem must belong to the specified freelancer."
                    )

                if company_profile and action_company_profile != company_profile:
                    raise serializers.ValidationError(
                        "ActionItem must belong to the specified company profile."
                    )

        return data

    def validate_status(self, value):
        if value and value not in [StatusType.PENDING.value, StatusType.APPROVED.value, StatusType.REJECTED.value]:
            raise serializers.ValidationError("Invalid status")

        request_user = self.context['request'].user

        if not self.instance:
            if value != StatusType.PENDING.value:
                raise serializers.ValidationError("Cannot set approved by during creation.")

        if self.instance and value != StatusType.PENDING.value:
            project = self.instance.project
            if project:
                if project.user.is_freelancer:
                    if request_user != project.user:
                        raise serializers.ValidationError(
                            "Only the assigned freelancer can approve/reject this TimeCard.")
                else:  # project.user is a company
                    has_review_permission = require_any_permission_multiple_modules(
                        request_user,
                        {"time_booking": ["review", "full_access"]},
                        "company",
                        {"project": project.id}
                    )
                    if not has_review_permission:
                        raise serializers.ValidationError("You do not have permission to approve/reject this TimeCard.")

            company_profile = self.instance.company_profile
            if company_profile:
                has_review_permission = require_any_permission_multiple_modules(
                    request_user,
                    {"time_booking": ["review", "full_access"]},
                    "company",
                    {"company_profile": company_profile.id}
                )
                if not has_review_permission:
                    raise serializers.ValidationError("You do not have permission to approve/reject this TimeCard.")

            freelancer = self.instance.freelancer
            if freelancer and request_user != freelancer:
                raise serializers.ValidationError("Only the freelancer can approve/reject this TimeCard.")
        return value

    def create(self, validated_data):
        """
        Custom create method to ensure `approved_by` is not set during create.
        """
        request_user = self.context['request'].user

        has_create_without_approval = require_any_permission_multiple_modules(
            request_user,
            {"time_booking": ["create_without_approval", "full_access"]},
            "company",
            None
        )

        # 3. Set approval if required
        if validated_data.get("freelancer"):
            validated_data["status"] = StatusType.APPROVED.value
            validated_data["approved_by"] = request_user
        if validated_data.get("company_profile") and request_user.company == validated_data.get("company_profile") and has_create_without_approval:
            validated_data["status"] = StatusType.APPROVED.value
            validated_data["approved_by"] = request_user
        if validated_data.get("project"):
            project = validated_data.get("project")
            if project.user.is_freelancer and request_user == project.user:
                validated_data["status"] = StatusType.APPROVED.value
                validated_data["approved_by"] = request_user
            if project.user.company and request_user.company == project.user.company and has_create_without_approval:
                validated_data["status"] = StatusType.APPROVED.value
                validated_data["approved_by"] = request_user

        return super().create(validated_data)

    def update(self, instance, validated_data):
        """
        Custom update method to enforce approval rules.
        """
        request_user = self.context['request'].user
        status = validated_data.get('status')

        if instance.approved_by:
            raise serializers.ValidationError("Approved TimeCards cannot be modified.")

        # Check if status is being updated to approved or rejected
        if status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
            # Check if the creator is a company admin using utility function
            from ecpmpapp.utils import is_company_admin, is_user_company_owner
            
            is_creator_company_admin = is_company_admin(instance.created_by)
            
            # Check if the request user is the company owner using utility function
            is_request_user_company_owner = is_user_company_owner(request_user)
            
            # Check if request user has time_booking review permissions
            has_time_booking_review_permissions = require_any_permission_multiple_modules(
                request_user, 
                {'time_booking': ['full_access', 'review']}, 
                'company', 
                self.context['request'].query_params
            )
            
            # Permission logic:
            # 1. If creator is company admin, only company owner can approve/reject
            # 2. For other creators, user must have time_booking: full_access or review permissions
            if is_creator_company_admin:
                if not is_request_user_company_owner:
                    raise serializers.ValidationError(
                        "Only company owner can approve/reject time cards created by company administrators."
                    )
            else:
                if not has_time_booking_review_permissions:
                    raise serializers.ValidationError(
                        "You don't have permission to approve/reject time cards. "
                        "You need 'time_booking: full_access' or 'time_booking: review' permissions."
                    )

        if status == StatusType.APPROVED.value:
            validated_data['approved_by'] = request_user
            validated_data['rejected_by'] = None
        elif status == StatusType.REJECTED.value:
            validated_data['rejected_by'] = request_user
            validated_data['approved_by'] = None

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)

        if instance.rejected_by:
            representation["rejected_by"] = {
                "id": instance.rejected_by.id,
                "name": instance.rejected_by.full_name,
            }

        # Invoice info
        related_line_items = instance.invoice_line_items.filter(
            invoice__document_type='invoice'
        ).order_by('-created_at')
        if related_line_items.exists():
            representation["invoice"] = {
                "id": related_line_items.first().invoice.id,
                "reference_number": related_line_items.first().invoice.reference_number,
                "status": related_line_items.first().invoice.status,
            }
        else:
            representation["invoice"] = None

        # Project name
        representation["project_name"] = instance.project.project_name if instance.project else None
        representation["project_image"] = instance.project.project_image if instance.project else None

        # ✅ Project's company info
        if instance.project and instance.project.user.company:
            representation["project_company"] = {
                "id": instance.project.user.company.id,
                "name": instance.project.user.company.company_name
            }
        else:
            representation["project_company"] = None

        representation["milestone_name"] = instance.milestone.name if instance.milestone else None

        representation["action_item_name"] = instance.action_item.name if instance.action_item else None

        representation["company_profile_name"] = instance.company_profile.company_name if instance.company_profile else None

        # Add PO numbers count
        if instance.project:
            request = self.context.get("request")
            user = getattr(request, "user", None)
            po_count = PurchaseOrderPermission(
                action="view",
                scope="company",
            ).filter_queryset_for_view(
                instance.project.purchase_orders.all(),
                user,
            ).count()
            representation["po_numbers"] = po_count
        else:
            representation["po_numbers"] = 0

        user_solution_role = UserRole.objects.filter(user=instance.created_by, role__type="solution", role__scope="company").first()
        representation["created_by_solution_role_details"] = None 
        if user_solution_role:
            representation["created_by_solution_role_details"] = {
                "id": user_solution_role.id,
                "name": user_solution_role.role.name,
                "description": user_solution_role.role.description,
                "is_default": user_solution_role.role.is_default,
                "is_active": user_solution_role.role.is_active,
            }

        return representation


class ExpenseCardSerializer(serializers.ModelSerializer):
    created_by = serializers.SerializerMethodField()
    approved_by = serializers.SerializerMethodField()
    rejected_by = serializers.PrimaryKeyRelatedField(read_only=True)
    
    class Meta:
        model = ExpenseCard
        fields = '__all__'
        read_only_fields = ['total', 'currency', 'converted_amount', 'converted_currency']


    def get_created_by(self, obj):
        if obj.created_by:
            return get_related_user_data(obj, obj.created_by)
        return None
    
    def get_approved_by(self, obj):
        if obj.approved_by:
            return get_related_user_data(obj, obj.approved_by)
        return None

    def validate(self, data):
        request = self.context.get('request')
        request_user = request.user

        # Use instance values for partial updates
        project = data.get('project', getattr(self.instance, 'project', None))
        company_profile = data.get('company_profile', getattr(self.instance, 'company_profile', None))
        freelancer = data.get('freelancer', getattr(self.instance, 'freelancer', None))
        milestone = data.get('milestone', getattr(self.instance, 'milestone', None))
        action_item = data.get('action_item', getattr(self.instance, 'action_item', None))
        category = data.get('category', getattr(self.instance, 'category', None))
        valid_choices = []

        # Ensure only one of project or (company_profile/freelancer) is set
        if project and (company_profile or freelancer):
            raise serializers.ValidationError(
                "ExpenseCard can have either a project or a company/freelancer, not both."
            )

        if not project and not (company_profile or freelancer):
            raise serializers.ValidationError(
                "ExpenseCard must have either a project or a company/freelancer."
            )

        # Project-specific validation
        if project:
            # TODO: Adjust as per new permission system
            # if not has_project_permission("GET", request_user, project, get_product_name_from_request(request)):
            #     raise serializers.ValidationError("You do not have access to this project.")

            if project.solution in SPECIAL_SOLUTION:
                if milestone or action_item:
                    raise serializers.ValidationError(
                        "ExpenseCard with a project can not associate a milestone or an action item."
                    )

            if milestone and action_item:
                raise serializers.ValidationError(
                    "ExpenseCard with a project can associate either a milestone or an action item, not both."
                )

            if milestone and milestone.project != project:
                raise serializers.ValidationError("Milestone must belong to the associated project.")

            if action_item:
                action_project, action_company_profile, action_freelancer = get_related_objects_from_action_items(
                    action_item)
                if action_project != project:
                    raise serializers.ValidationError("ActionItem must belong to the associated project.")

            valid_choices = get_project_owner_valid_choices(
                "expense_types",
                project,
                request,
            )

        # Company/Freelancer-specific validation
        if company_profile or freelancer:
            if milestone:
                raise serializers.ValidationError(
                    "ExpenseCard with a company or freelancer can only associate an action item, not a milestone."
                )
            if company_profile and request_user.company != company_profile:
                raise serializers.ValidationError("You do not have permission to add time in this company.")
            if freelancer and freelancer != request_user:
                raise serializers.ValidationError("You do not have permission to add time in this freelancer.")

            if action_item:
                action_project, action_company_profile, action_freelancer = get_related_objects_from_action_items(
                    action_item)
                if company_profile:
                    valid_choices = get_valid_choices("expense_types", company_profile.id, request, is_company=True)
                    if action_company_profile != company_profile:
                        raise serializers.ValidationError("ActionItem must belong to the specified company.")
                if freelancer:
                    valid_choices = get_valid_choices("expense_types", freelancer.id, request, is_freelancer=True)
                    if action_freelancer != freelancer:
                        raise serializers.ValidationError("ActionItem must belong to the specified freelancer.")

        if valid_choices and category not in valid_choices:
            raise serializers.ValidationError(f"Invalid category. Allowed choices are: {', '.join(valid_choices)}")

        return data

    def validate_status(self, value):
        if value and value not in [StatusType.PENDING.value, StatusType.APPROVED.value, StatusType.REJECTED.value]:
            raise serializers.ValidationError("Invalid status")

        request_user = self.context['request'].user

        if not self.instance:
            if value != StatusType.PENDING.value:
                raise serializers.ValidationError("Cannot set approved by during creation.")

        if self.instance and value in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
            if self.instance.created_by == request_user:
                raise serializers.ValidationError(
                    "You cannot approve/reject your own expense card."
                )

            project = self.instance.project
            if project:
                if project.user.is_freelancer:
                    if request_user != project.user:
                        raise serializers.ValidationError(
                            "Only the assigned freelancer can approve/reject this ExpenseCard.")
                else:  # project.user is a company
                    has_review_permission = require_any_permission_multiple_modules(
                        request_user,
                        {"expense_booking": ["review", "full_access"]},
                        "company",
                        {"project": project.id}
                    )
                    if not has_review_permission:
                        raise serializers.ValidationError("You do not have permission to approve/reject this ExpenseCard.")

            company_profile = self.instance.company_profile
            if company_profile:
                has_review_permission = require_any_permission_multiple_modules(
                    request_user,
                    {"expense_booking": ["review", "full_access"]},
                    "company",
                    {"company_profile": company_profile.id}
                )
                if not has_review_permission:
                    raise serializers.ValidationError("You do not have permission to approve/reject this ExpenseCard.")

            freelancer = self.instance.freelancer
            if freelancer and request_user != freelancer:
                raise serializers.ValidationError("Only the freelancer can approve/reject this ExpenseCard.")
        return value

    def create(self, validated_data):
        request = self.context.get('request')
        request_user = request.user
        validated_data['created_by'] = request_user

        # 1. Calculate total
        rate = validated_data.get('rate', 0)
        quantity = validated_data.get('quantity', 0)
        total = float(rate) * float(quantity)
        validated_data['total'] = total

        # 2. Currency conversion logic
        # Determine target currency
        if validated_data.get('company_profile'):
            company_currency_symbol = validated_data['company_profile'].currency or '€'
        elif validated_data.get('freelancer') and hasattr(validated_data['freelancer'], 'freelancer_profile'):
            company_currency_symbol = validated_data['freelancer'].freelancer_profile.currency or '€'
        elif validated_data.get('project') and validated_data['project'].company_profile:
            company_currency_symbol = validated_data['project'].company_profile.currency or '€'


        else:
            company_currency_symbol = '€'  # fallback

        company_currency_code = symbol_to_currency_code(company_currency_symbol)

        # Determine expense currency
        expense_currency_symbol = None
        if validated_data.get('project') and validated_data['project'].currency:
            expense_currency_symbol = validated_data['project'].currency
        elif validated_data.get('company_profile') and validated_data['company_profile'].currency:
            expense_currency_symbol = validated_data['company_profile'].currency
        elif validated_data.get('freelancer') and hasattr(validated_data['freelancer'], 'freelancer_profile') and validated_data['freelancer'].freelancer_profile.currency:
            expense_currency_symbol = validated_data['freelancer'].freelancer_profile.currency

        expense_currency_code = symbol_to_currency_code(expense_currency_symbol)

        validated_data['currency'] = expense_currency_code
        validated_data['converted_currency'] = company_currency_code

        if expense_currency_code.upper() == company_currency_code.upper():
            validated_data['converted_amount'] = total
        else:
            converter = ECBCurrencyConverter()
            converted = converter.convert_currency(total, expense_currency_code, company_currency_code)
            validated_data['converted_amount'] = converted if converted is not None else 0

        # Check if user has create_without_approval permission
        has_create_without_approval = require_any_permission_multiple_modules(
            request_user,
            {"expense_booking": ["create_without_approval", "full_access"]},
            "company",
            None
        )

        # 3. Set approval if required
        if validated_data.get("freelancer"):
            validated_data["status"] = StatusType.APPROVED.value
            validated_data["approved_by"] = request_user
        if validated_data.get("company_profile") and request_user.company == validated_data.get("company_profile") and has_create_without_approval:
            validated_data["status"] = StatusType.APPROVED.value
            validated_data["approved_by"] = request_user
        if validated_data.get("project"):
            project = validated_data.get("project")
            if project.user.is_freelancer and request_user == project.user:
                validated_data["status"] = StatusType.APPROVED.value
                validated_data["approved_by"] = request_user
            if project.user.company and request_user.company == project.user.company and has_create_without_approval:
                validated_data["status"] = StatusType.APPROVED.value
                validated_data["approved_by"] = request_user

        return super().create(validated_data)



    def update(self, instance, validated_data):
        """
        Custom update method to enforce approval rules.
        """
        request_user = self.context['request'].user
        status = validated_data.get('status', instance.status)

        if "total" in validated_data and instance.total != validated_data.get('total'):
            # Convert total to company currency
            if instance.company_profile:
                company_currency_symbol = instance.company_profile.currency or '€'
            elif instance.freelancer and hasattr(instance.freelancer, 'freelancer_profile'):
                company_currency_symbol = instance.freelancer.freelancer_profile.currency or '€'
            elif instance.project and instance.project.company_profile:
                company_currency_symbol = instance.project.company_profile.currency or '€'


            else:
                company_currency_symbol = '€'  # fallback

            company_currency_code = symbol_to_currency_code(company_currency_symbol)

            # Determine expense currency
            expense_currency_symbol = None
            if instance.project and instance.project.currency:
                expense_currency_symbol = instance.project.currency
            elif instance.company_profile and instance.company_profile.currency:
                expense_currency_symbol = instance.company_profile.currency
            elif validated_data.get('freelancer') and hasattr(validated_data['freelancer'], 'freelancer_profile') and validated_data['freelancer'].freelancer_profile.currency:
                expense_currency_symbol = validated_data['freelancer'].freelancer_profile.currency

            expense_currency_code = symbol_to_currency_code(expense_currency_symbol)

            validated_data['currency'] = expense_currency_code
            validated_data['converted_currency'] = company_currency_code

            if expense_currency_code.upper() == company_currency_code.upper():
                validated_data['converted_amount'] = validated_data.get('total')
            else:
                converter = ECBCurrencyConverter()
                converted = converter.convert_currency(validated_data.get('total')  , expense_currency_code, company_currency_code)
                validated_data['converted_amount'] = converted if converted is not None else 0



        if instance.approved_by:
            raise serializers.ValidationError("Approved ExpenseCards cannot be modified.")

        # Check if status is being updated to approved or rejected
        if status in [StatusType.APPROVED.value, StatusType.REJECTED.value]:
            if instance.created_by == request_user:
                raise serializers.ValidationError(
                    "You cannot approve/reject your own expense card."
                )

            has_expense_booking_review_permissions = require_any_permission_multiple_modules(
                request_user, 
                {'expense_booking': ['full_access', 'review']}, 
                'company', 
                self.context['request'].query_params
            )

            if not has_expense_booking_review_permissions:
                raise serializers.ValidationError(
                    "You don't have permission to approve/reject expense cards. "
                    "You need 'expense_booking: full_access' or 'expense_booking: review' permissions."
                )

        if status == StatusType.APPROVED.value:
            validated_data['approved_by'] = request_user
            validated_data['rejected_by'] = None
        elif status == StatusType.REJECTED.value:
            validated_data['rejected_by'] = request_user
            validated_data['approved_by'] = None

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.rejected_by:
            representation["rejected_by"] = {
                "id": instance.rejected_by.id,
                "name": instance.rejected_by.full_name,
            }
        related_line_items = instance.invoice_line_items.filter(
            invoice__document_type='invoice'
        ).order_by('-created_at')
        if related_line_items.exists():
            representation["invoice"] = {
                "id": related_line_items.first().invoice.id,
                "reference_number": related_line_items.first().invoice.reference_number,
                "status": related_line_items.first().invoice.status,
            }
        else:
            representation["invoice"] = None

        representation["project_name"] = instance.project.project_name if instance.project else None
        representation["project_image"] = instance.project.project_image if instance.project else None

        if instance.project and instance.project.user.company:
            representation["project_company"] = {
                "id": instance.project.user.company.id,
                "name": instance.project.user.company.company_name
            }
        else:
            representation["project_company"] = None

        representation["milestone_name"] = instance.milestone.name if instance.milestone else None

        representation["action_item_name"] = instance.action_item.name if instance.action_item else None

        representation["company_profile_name"] = instance.company_profile.company_name if instance.company_profile else None

        user_solution_role = UserRole.objects.filter(user=instance.created_by, role__type="solution", role__scope="company").first()
        representation["created_by_solution_role_details"] = None 
        if user_solution_role:
            representation["created_by_solution_role_details"] = {
                "id": user_solution_role.id,
                "name": user_solution_role.role.name,
                "description": user_solution_role.role.description,
                "is_default": user_solution_role.role.is_default,
                "is_active": user_solution_role.role.is_active,
            }

        return representation


class ProjectWorkSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = ProjectWork
        fields = ['id', 'description', 'hours', 'action_item', 'date', 'files']


class GeneralWorkSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = GeneralWork
        fields = ['id', 'description', 'hours', 'date', 'category', 'description', 'amount']


class ProjectExpenseSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = ProjectExpense
        fields = ['id', 'expense_description', 'expense_category', 'expense_rate', 'expense_quantity', 'expense_total',
                  'description', 'files']


class GeneralExpenseSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False, allow_null=True)

    class Meta:
        model = GeneralExpense
        fields = ['id', 'expense_description', 'expense_category', 'expense_rate', 'expense_quantity', 'expense_total',
                  'description', 'contact', 'files']


class ProjectTimeSerializer(serializers.ModelSerializer):
    project_works = ProjectWorkSerializer(many=True, required=False)
    general_works = GeneralWorkSerializer(many=True, required=False)
    project_expenses = ProjectExpenseSerializer(many=True, required=False)
    general_expenses = GeneralExpenseSerializer(many=True, required=False)
    project = serializers.PrimaryKeyRelatedField(
        queryset=Project.objects.all(),
        required=True
    )

    class Meta:
        model = ProjectTime
        fields = [
            'id', 'user', 'project', 'date', 'total_project_hours',
            'total_general_hours', 'total_project_expenses', 'total_expenses', 'created_at', 'updated_at',
            'project_works', 'general_works', 'project_expenses', 'general_expenses'
        ]

    def validate_category_fields(self, project_id, field_name, value):
        request = self.context.get("request")
        valid_choices = get_project_owner_valid_choices(
            field_name,
            project_id,
            request,
        )
        if valid_choices and value not in valid_choices:
            raise serializers.ValidationError(f"Invalid {field_name}. Allowed choices are: {', '.join(valid_choices)}")
        return value


    # TODO: Adjust as per new permission system
    # def validate_project(self, value):
    #     request_user = self.context.get('user')
    #     request = self.context.get("request")
    #     product_name = get_product_name_from_request(request)
    #     if product_name in COLLABORATIVE_SOLUTION:
    #         products = COLLABORATIVE_SOLUTION
    #     else:
    #         products = [product_name]
    #     if value.user == request_user and value.solution in products:
    #         return value

    #     # if has_project_permission("PUT", request_user, value, product_name):
    #     #     return value
    #     # raise serializers.ValidationError(f"Project {value} does not belong to this user")

    def validate_user(self, value):
        if value != self.context.get('user'):
            raise serializers.ValidationError(f"User having {value.id} id does not belong to signed in user")
        return value

    def validate_project_work_milestone(self, milestone):
        request_user = self.context.get('user')
        request = self.context.get("request")
        product_name = get_product_name_from_request(request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
        try:
            if milestone.project.user == request_user and milestone.project.solution in products:
                return milestone
            if milestone.project.team_members.filter(team_member=request_user).first():
                return milestone
        except:
            pass
        raise serializers.ValidationError(f"Milestone {milestone.id} does not belong to signed in user")

    def validate_project_work_action_item(self, action_item):
        request_user = self.context.get('user')
        request = self.context.get("request")
        product_name = get_product_name_from_request(request)
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
        try:
            if action_item.milestone.project.solution in products:
                if action_item.milestone.project.user == request_user:
                    return action_item
                if action_item.milestone.project.team_members.filter(team_member=request_user).first():
                    return action_item
        except:
            pass
        raise serializers.ValidationError(f"Action item {action_item.id} does not belong to signed in user")

    @transaction.atomic
    def create(self, validated_data):
        project_works_data = validated_data.pop('project_works', [])
        general_works_data = validated_data.pop('general_works', [])
        project_expenses_data = validated_data.pop('project_expenses', [])
        general_expenses_data = validated_data.pop('general_expenses', [])

        # Create the ProjectTime record
        project_time = ProjectTime.objects.create(**validated_data)

        total_proj_hours = 0
        total_general_hours = 0
        total_project_expenses = 0
        total_general_expenses = 0

        # Create related expenses
        for project_work_data in project_works_data:
            # self.validate_project_work_action_item(dict(project_work_data).get('action_item'))
            ProjectWork.objects.create(time_expense=project_time, **project_work_data)
            total_proj_hours += project_work_data.get('hours', 0)

        for general_work_data in general_works_data:
            GeneralWork.objects.create(time_expense=project_time, **general_work_data)
            total_general_hours += general_work_data.get('hours', 0)

        for project_expense_data in project_expenses_data:
            self.validate_category_fields(project_time.project.id, 'expense_types',
                                          dict(project_expense_data).get('expense_category'))
            ProjectExpense.objects.create(time_expense=project_time, **project_expense_data)
            total_project_expenses += project_expense_data.get('expense_total', 0)

        for general_expense_data in general_expenses_data:
            self.validate_category_fields(project_time.project.id, 'expense_types',
                                          dict(general_expense_data).get('expense_category'))
            GeneralExpense.objects.create(time_expense=project_time, **general_expense_data)
            total_general_expenses += general_expense_data.get('expense_total', 0)

        project_time.total_project_hours = total_proj_hours
        project_time.total_general_hours = total_general_hours
        project_time.total_project_expenses = total_project_expenses
        project_time.total_expenses = total_general_expenses
        project_time.save()

        return project_time

    def update_related_items(self, instance, related_model, related_data_key, related_field):
        related_data = self.validated_data.pop(related_data_key, [])

        # Get existing related item IDs
        existing_ids = {item.id for item in getattr(instance, related_data_key).all()}
        incoming_ids = {data.get('id') for data in related_data if data.get('id')}

        # Delete items that are no longer present in the incoming data
        for item_id in existing_ids - incoming_ids:
            related_model.objects.filter(id=item_id).delete()

        total = 0
        # Update or create related items
        for item_data in related_data:
            if related_data_key in ["general_expenses", "project_expenses"]:
                self.validate_category_fields(instance.project.id, 'expense_types',
                                              dict(item_data).get('expense_category'))
            item_id = item_data.get('id')
            if item_id:
                related_model.objects.update_or_create(
                    id=item_id,
                    defaults={**item_data, related_field: instance}
                )
            else:
                related_model.objects.create(**item_data, **{related_field: instance})

            if related_data_key in ['project_works', 'general_works']:
                total += item_data.get('hours', 0)
            else:
                total += item_data.get('expense_total', 0)

        return total

    @transaction.atomic
    def update(self, instance, validated_data):
        instance.date = validated_data.get('date', instance.date)
        instance.save()

        total_proj_hours = self.update_related_items(instance, ProjectWork, 'project_works', 'time_expense')
        total_general_hours = self.update_related_items(instance, GeneralWork, 'general_works', 'time_expense')
        total_proj_expenses = self.update_related_items(instance, ProjectExpense, 'project_expenses', 'time_expense')
        total_general_expenses = self.update_related_items(instance, GeneralExpense, 'general_expenses', 'time_expense')

        instance.total_project_hours = total_proj_hours
        instance.total_general_hours = total_general_hours
        instance.total_project_expenses = total_proj_expenses
        instance.total_expenses = total_general_expenses
        instance.save()

        return instance

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation['user'] = UserSerializer(instance.user).data
        return representation
