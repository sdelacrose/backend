from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core import signing
from django.db.models import Count, FloatField, OuterRef, Q, Subquery, Sum
from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from rest_framework.exceptions import PermissionDenied, ValidationError

from models.models import CompanyConfig, CompanyProfile, Connection, User
from project_management.models import (
    ActionItem,
    Document,
    ExpenseCard,
    Invoice,
    Folder,
    Milestone,
    ObjectAssessment,
    Project,
    ProjectPhase,
    PurchaseOrder,
    ECPMPConfig,
    TimeCard,
)
from project_management.views.action_item import ActionItemViewSet
from project_management.views.billing import InvoiceViewSet
from project_management.views.document import DocumentViewSet, FolderViewSet
from project_management.views.object_assessment import ObjectAssessmentViewSet
from project_management.views.project import ProjectViewSet
from project_management.views.purchase_order import PurchaseOrderViewSet
from project_management.permissions import ContactPermission, InvoicePermission, PurchaseOrderPermission
from project_management.models.contact import ContactUser
from project_management.serializers.purchase_order import get_po_revised_amount_incl_vat
from userapis.models import CalendarEvent
from userapis.permissions import CalendarEventPermission
from userapis.serializers import CalendarEventSerializer
from userapis.microsoft_outlook import (
    create_outlook_calendar_event,
    get_outlook_contacts,
    is_outlook_token_valid,
)
from userapis.views.microsoft_auth import build_outlook_oauth_url
from ecpmpapp.constants import StatusType
from ecpmpapp.utils import get_user_active_solution

from .exceptions import AIToolClarificationRequired


CALENDAR_CONFIRMATION_MAX_AGE_SECONDS = 30 * 60

try:
    from project_management.models import AvailableProductPart, BillOfMaterialPart, Requirement, SolutionDemand
    from project_management.views.matrix import (
        AvailableProductPartViewSet,
        BillOfMaterialPartViewSet,
        RequirementViewSet,
        SolutionDemandViewSet,
    )

    MATRIX_AND_PARTS_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    AvailableProductPart = BillOfMaterialPart = Requirement = SolutionDemand = None
    AvailableProductPartViewSet = BillOfMaterialPartViewSet = RequirementViewSet = SolutionDemandViewSet = None
    MATRIX_AND_PARTS_AVAILABLE = False


COMPLETED_STATUS_VALUES = {"closed", "complete", "completed", "done"}
ACTIVE_STATUS_VALUES = {"open", "opened", "in progress", "in_progress"}
OPEN_STATUS_VALUES = {"open", "opened", "active"}
DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MATRIX_AND_PARTS_UNAVAILABLE_WARNING = (
    "Demand, requirement, and parts tools are not available on this backend branch yet. "
    "Deploy the matrix/parts backend feature before enabling these AI queries."
)
ACTION_ITEM_HAS_FORECAST_DATE = any(field.name == "forecast_date" for field in ActionItem._meta.get_fields())
ACTION_ITEM_HAS_ASSIGNEES = any(field.name == "assignees" for field in ActionItem._meta.get_fields())
ACTION_ITEM_HAS_ASSIGNEE = any(field.name == "assignee" for field in ActionItem._meta.get_fields())


@dataclass
class ToolRequest:
    user: Any
    query_params: dict[str, Any]
    method: str = "GET"
    data: dict[str, Any] | None = None


def normalize_limit(limit: int | None) -> int:
    if not limit:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def parse_date_value(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return parse_date(str(value))


def parse_datetime_value(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(str(value))
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def request_timezone(filters=None):
    timezone_name = (filters or {}).get("timezone")
    if timezone_name:
        try:
            return ZoneInfo(str(timezone_name))
        except ZoneInfoNotFoundError:
            raise ValidationError("Timezone must be a valid IANA timezone name.")
    return timezone.get_current_timezone()


def date_bounds_for_day(value, tz=None) -> tuple[datetime, datetime] | tuple[None, None]:
    day = parse_date_value(value)
    if not day:
        return None, None
    tz = tz or timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(day, time.min), tz),
        timezone.make_aware(datetime.combine(day, time.max), tz),
    )


def compact_user(user):
    if not user:
        return None
    return {
        "id": user.id,
        "name": getattr(user, "full_name", None) or f"{user.first_name} {user.last_name}".strip(),
    }


def source_ref(kind: str, obj_id: int, label: str, url: str, **extra):
    data = {"type": kind, "id": obj_id, "label": label, "url": url}
    data.update(extra)
    return data


def response_payload(answer_type, *, rows=None, summary=None, chart=None, sources=None, warnings=None, has_more=False):
    return {
        "answer_type": answer_type,
        "summary": summary or {},
        "rows": rows or [],
        "chart": chart,
        "sources": sources or [],
        "warnings": warnings or [],
        "has_more": has_more,
    }


def not_closed_status_queryset(queryset):
    """Return every configured status except the product's Closed status."""
    return queryset.exclude(status__iexact="closed")


def has_explicit_status_filter(filters):
    return bool(filters.get("status") or filters.get("excluded_statuses"))


def apply_status_filter(queryset, raw_value, *, field_name="status"):
    if not raw_value:
        return queryset

    raw_value = str(raw_value).strip()
    is_negative = raw_value.startswith("-")
    status_values = [value.strip() for value in raw_value[1 if is_negative else 0 :].split(",") if value.strip()]
    if not status_values:
        return queryset

    status_filter = Q()
    for value in status_values:
        status_filter |= Q(**{f"{field_name}__iexact": value})

    return queryset.exclude(status_filter) if is_negative else queryset.filter(status_filter)


def _configured_option_names(values):
    names = []
    for value in values or []:
        name = str(value or "").split(":", 1)[0].strip()
        if name and name.lower() not in {item.lower() for item in names}:
            names.append(name)
    return names


def _configured_project_or_company_choices(user, payload, field_name):
    project_id = payload.get("project_id")
    if project_id:
        config = ECPMPConfig.objects.filter(project_id=project_id).only(field_name).first()
        if config:
            return _configured_option_names(getattr(config, field_name, []))

    company_id = resolve_company_profile_id(user, payload) or getattr(user, "company_id", None)
    if company_id:
        solution = get_user_active_solution(user.id)
        configs = CompanyConfig.objects.filter(company_profile_id=company_id)
        config = configs.filter(solution=solution).only(field_name).first() or configs.only(field_name).first()
        if config:
            return _configured_option_names(getattr(config, field_name, []))
    return []


def visible_action_item_statuses(user, project_queryset=None):
    """Return configured Action Item statuses without exposing hidden projects."""
    statuses = []
    seen_statuses = set()

    def add_values(values):
        for name in _configured_option_names(values):
            normalized_name = name.casefold()
            if normalized_name not in seen_statuses:
                statuses.append(name)
                seen_statuses.add(normalized_name)

    add_values(
        _configured_project_or_company_choices(
            user,
            {"scope": "company", "filters": {}},
            "status_options",
        )
    )

    if project_queryset is None:
        project_queryset = visible_projects(
            user,
            {"scope": "all_accessible", "filters": {}},
        )
    project_ids = project_queryset.values_list("id", flat=True)
    for options in (
        ECPMPConfig.objects.filter(project_id__in=project_ids)
        .order_by("project_id")
        .values_list("status_options", flat=True)
    ):
        add_values(options)

    return statuses


def _validate_choice_filter(
    queryset,
    raw_value,
    *,
    field_name,
    configured_choices=None,
    aliases=None,
):
    if not raw_value:
        return
    value = str(raw_value).strip().lstrip("-")
    requested = [item.strip() for item in value.split(",") if item.strip()]
    if not requested:
        return

    database_choices = [
        str(item).strip()
        for item in queryset.order_by().values_list(field_name, flat=True).distinct()
        if item
    ]
    choices = []
    for choice in [*(configured_choices or []), *database_choices]:
        if choice and choice.lower() not in {item.lower() for item in choices}:
            choices.append(choice)
    allowed = {item.lower() for item in choices} | {item.lower() for item in (aliases or [])}
    unknown = [item for item in requested if item.lower() not in allowed]
    if unknown:
        raise AIToolClarificationRequired(
            field=field_name,
            message=(
                f"'{unknown[0]}' is not a valid {field_name.replace('_', ' ')}. "
                "Please choose one of the available values."
            ),
            choices=choices,
        )


def _uses_only_aliases(raw_value, aliases):
    value = str(raw_value or "").strip().lstrip("-")
    requested = {item.strip().lower() for item in value.split(",") if item.strip()}
    return bool(requested) and requested <= {str(alias).lower() for alias in aliases}


def request_params(payload: dict[str, Any], *, project_key="project", company_key="company_profile"):
    params = {}
    scope = payload.get("scope") or "all_accessible"
    if payload.get("project_id"):
        params[project_key] = payload["project_id"]
    elif scope == "company" and payload.get("company_profile_id"):
        params[company_key] = payload["company_profile_id"]
    if payload.get("filters", {}).get("solution"):
        params["solution"] = payload["filters"]["solution"]
    return params


def resolve_company_profile_id(user, payload):
    filters = payload.get("filters") or {}
    company_profile_id = payload.get("company_profile_id") or filters.get("company_profile_id")
    if company_profile_id:
        try:
            company_profile_id = int(company_profile_id)
        except (TypeError, ValueError):
            raise ValidationError("Company profile ID must be a valid integer.")
    elif payload.get("scope") == "company":
        company_profile_id = getattr(user, "company_id", None)

    user_company_id = getattr(user, "company_id", None)
    if company_profile_id and user_company_id and int(company_profile_id) != int(user_company_id):
        raise PermissionDenied("Company profile not found or not accessible.")
    return company_profile_id


def scoped_request_params(user, payload: dict[str, Any], *, project_key="project", company_key="company_profile"):
    company_profile_id = resolve_company_profile_id(user, payload)
    scoped_payload = {**payload}
    if company_profile_id:
        scoped_payload["company_profile_id"] = company_profile_id
    return request_params(scoped_payload, project_key=project_key, company_key=company_key)


def viewset_queryset(viewset_class, user, params):
    viewset = viewset_class()
    viewset.request = ToolRequest(user=user, query_params=params, data={})
    viewset.action = "list"
    viewset.kwargs = {}
    return viewset.get_queryset()


def ensure_purchase_order_view_permission(user):
    if PurchaseOrderPermission(action="view", scope="company").get_view_scope(user) == "none":
        raise PermissionDenied("Purchase Order access is required.")


def ensure_invoice_view_permission(user):
    permission = InvoicePermission(action="view", scope="company")
    request = ToolRequest(user=user, query_params={}, method="GET", data={})
    view = type("InvoiceListView", (), {"action": "list"})()
    if not permission.has_permission(request, view):
        raise PermissionDenied("Invoice access is required.")


def ensure_project_visible(user, payload):
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    project_name = (payload.get("project_name") or "").strip()
    if project_id is None and project_name:
        visible_projects = viewset_queryset(ProjectViewSet, user, {})
        matches = visible_projects.filter(
            project_name__iexact=project_name
        ).order_by("id")
        match_rows = list(matches.values("id", "project_name")[:10])
        if not match_rows:
            partial_matches = list(
                visible_projects.filter(project_name__icontains=project_name)
                .order_by("project_name", "id")
                .values("id", "project_name")[:10]
            )
            if partial_matches:
                raise AIToolClarificationRequired(
                    field="project",
                    message="More than one accessible project may match that name. Which project should I use?",
                    choices=partial_matches,
                )
            raise PermissionDenied("Project not found or not accessible.")
        if len(match_rows) > 1:
            raise AIToolClarificationRequired(
                field="project",
                message="More than one accessible project has that name. Which project should I use?",
                choices=match_rows,
            )
        project_id = match_rows[0]["id"]
        payload["project_id"] = project_id
    if project_id is None:
        return None

    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        raise ValidationError("Project ID must be a valid positive integer.")
    if project_id <= 0:
        raise ValidationError("Project ID must be a valid positive integer.")

    queryset = viewset_queryset(ProjectViewSet, user, {"project": project_id})
    if not queryset.filter(id=project_id).exists():
        raise PermissionDenied("Project not found or not accessible.")
    return project_id


def visible_action_items(user, payload):
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload)
    queryset = viewset_queryset(ActionItemViewSet, user, params)
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)
    requester_company_profile_id = (
        company_profile_id or getattr(user, "company_id", None)
    )

    scope = payload.get("scope") or "all_accessible"

    if project_id:
        queryset = queryset.filter(
            Q(milestone__project_id=project_id)
            | Q(parent_action_item__milestone__project_id=project_id)
        )
    elif scope in {"accessible_projects", "all_accessible", "current_user"}:
        accessible_project_ids = visible_projects(
            user,
            {"scope": "all_accessible", "filters": {}},
        ).values_list("id", flat=True)
        accessible_project_filter = (
            Q(milestone__project_id__in=accessible_project_ids)
            | Q(parent_action_item__milestone__project_id__in=accessible_project_ids)
        )
        if scope == "accessible_projects":
            queryset = queryset.filter(accessible_project_filter)
        else:
            company_item_filter = Q(
                milestone__isnull=True,
                parent_action_item__milestone__isnull=True,
            )
            if user.is_freelancer:
                company_item_filter &= (
                    Q(freelancer=user)
                    | Q(parent_action_item__freelancer=user)
                )
            elif requester_company_profile_id:
                company_item_filter &= (
                    Q(company_profile_id=requester_company_profile_id)
                    | Q(
                        parent_action_item__company_profile_id=(
                            requester_company_profile_id
                        )
                    )
                )
            else:
                company_item_filter &= Q(pk__in=[])
            queryset = queryset.filter(accessible_project_filter | company_item_filter)
    elif scope == "company" and company_profile_id:
        queryset = queryset.filter(
            Q(company_profile_id=company_profile_id)
            | Q(parent_action_item__company_profile_id=company_profile_id)
        )

    if filters.get("include_sub_actions") is False:
        queryset = queryset.filter(parent_action_item__isnull=True)

    current_user_assignee_values = {"current_user", "me", "myself"}
    assignee_filter = filters.get("assignee")
    if payload.get("scope") == "current_user" or assignee_filter in current_user_assignee_values:
        if ACTION_ITEM_HAS_ASSIGNEES:
            queryset = queryset.filter(assignees=user)
        elif ACTION_ITEM_HAS_ASSIGNEE:
            queryset = queryset.filter(assignee=user)
        else:
            queryset = queryset.none()
    elif filters.get("related_to_me"):
        user_filter = Q(created_by=user)
        if ACTION_ITEM_HAS_ASSIGNEES:
            user_filter |= Q(assignees=user)
        elif ACTION_ITEM_HAS_ASSIGNEE:
            user_filter |= Q(assignee=user)
        queryset = queryset.filter(user_filter).distinct()
    if assignee_filter and assignee_filter not in current_user_assignee_values:
        if ACTION_ITEM_HAS_ASSIGNEES:
            queryset = queryset.filter(assignees__id=assignee_filter)
        elif ACTION_ITEM_HAS_ASSIGNEE:
            queryset = queryset.filter(assignee_id=assignee_filter)
    if filters.get("created_by") == "current_user":
        queryset = queryset.filter(created_by=user)
    elif filters.get("created_by"):
        queryset = queryset.filter(created_by_id=filters["created_by"])
    if filters.get("search"):
        search = filters["search"]
        queryset = queryset.filter(
            Q(name__icontains=search)
            | Q(notes__icontains=search)
            | Q(status__icontains=search)
        )
    if filters.get("priority"):
        _validate_choice_filter(
            queryset,
            filters["priority"],
            field_name="priority_level",
            configured_choices=_configured_project_or_company_choices(
                user,
                payload,
                "priority_levels",
            ),
        )
        queryset = queryset.filter(priority_level__iexact=filters["priority"])
    if filters.get("status"):
        status_aliases = ACTIVE_STATUS_VALUES | COMPLETED_STATUS_VALUES | OPEN_STATUS_VALUES
        if not _uses_only_aliases(filters["status"], status_aliases):
            _validate_choice_filter(
                queryset,
                filters["status"],
                field_name="status",
                configured_choices=_configured_project_or_company_choices(
                    user,
                    payload,
                    "status_options",
                ),
                aliases=status_aliases,
            )
        queryset = apply_status_filter(queryset, filters["status"])
    if filters.get("excluded_statuses"):
        excluded_statuses = filters["excluded_statuses"]
        configured_statuses = _configured_project_or_company_choices(
            user,
            payload,
            "status_options",
        )
        for excluded_status in excluded_statuses:
            _validate_choice_filter(
                queryset,
                excluded_status,
                field_name="status",
                configured_choices=configured_statuses,
                aliases=COMPLETED_STATUS_VALUES,
            )
        excluded_filter = Q()
        for excluded_status in excluded_statuses:
            excluded_filter |= Q(status__iexact=excluded_status)
        queryset = queryset.exclude(excluded_filter)
    if filters.get("target_date_from"):
        queryset = queryset.filter(target_date__gte=parse_date_value(filters["target_date_from"]))
    if filters.get("target_date_to"):
        queryset = queryset.filter(target_date__lte=parse_date_value(filters["target_date_to"]))
    if ACTION_ITEM_HAS_FORECAST_DATE and filters.get("forecast_date_from"):
        queryset = queryset.filter(forecast_date__gte=parse_date_value(filters["forecast_date_from"]))
    if ACTION_ITEM_HAS_FORECAST_DATE and filters.get("forecast_date_to"):
        queryset = queryset.filter(forecast_date__lte=parse_date_value(filters["forecast_date_to"]))
    if filters.get("closed_at_from"):
        queryset = queryset.filter(closed_at__gte=parse_date_value(filters["closed_at_from"]))
    if filters.get("closed_at_to"):
        queryset = queryset.filter(closed_at__lte=parse_date_value(filters["closed_at_to"]))
    if filters.get("late_only"):
        if not has_explicit_status_filter(filters):
            queryset = not_closed_status_queryset(queryset)
        queryset = queryset.filter(target_date__lt=timezone.localdate())

    return queryset.distinct()


def decimal_value(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def float_value(value) -> float:
    return float(decimal_value(value))


def action_items_with_booked_annotations(queryset):
    approved_time_card_hours = (
        TimeCard.objects.filter(action_item=OuterRef("pk"), status=StatusType.APPROVED.value)
        .values("action_item")
        .annotate(total=Sum("hours"))
        .values("total")[:1]
    )
    approved_expense_card_total = (
        ExpenseCard.objects.filter(action_item=OuterRef("pk"), status=StatusType.APPROVED.value)
        .values("action_item")
        .annotate(total=Sum("converted_amount"))
        .values("total")[:1]
    )
    return queryset.annotate(
        booked_hour=Subquery(approved_time_card_hours, output_field=FloatField()),
        booked_cost=Subquery(approved_expense_card_total, output_field=FloatField()),
    )


def action_item_project(item: ActionItem):
    if item.milestone_id:
        return item.milestone.project
    parent = getattr(item, "parent_action_item", None)
    return parent.milestone.project if parent and parent.milestone_id else None


def action_item_row(item: ActionItem):
    project = action_item_project(item)
    if ACTION_ITEM_HAS_ASSIGNEES:
        assignees = [compact_user(user) for user in item.assignees.all()]
    elif ACTION_ITEM_HAS_ASSIGNEE and getattr(item, "assignee_id", None):
        assignees = [compact_user(item.assignee)]
    else:
        assignees = []
    return {
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "project": {"id": project.id, "name": project.project_name} if project else None,
        "company_profile_id": item.company_profile_id,
        "target_date": item.target_date.isoformat() if item.target_date else None,
        "forecast_date": item.forecast_date.isoformat() if getattr(item, "forecast_date", None) else None,
        "closed_at": item.closed_at.isoformat() if item.closed_at else None,
        "allocated_budget": item.allocated_budget,
        "allocated_hours": item.allocated_time,
        "booked_cost": getattr(item, "booked_cost", None),
        "booked_hours": getattr(item, "booked_hour", None),
        "assignees": assignees,
        "delay_days": max((timezone.localdate() - item.target_date).days, 0) if item.target_date else None,
    }


def action_item_source(item: ActionItem):
    project = action_item_project(item)
    url = f"/project-action-items?project_id={project.id}&id={item.id}" if project else f"/company-action-items?id={item.id}"
    return source_ref("action_item", item.id, item.name, url, project_id=project.id if project else None)


def action_item_ordering(raw_ordering):
    ordering = str(raw_ordering or "target_date").strip()
    allowed_fields = {"id", "name", "status", "target_date", "closed_at"}
    if ACTION_ITEM_HAS_FORECAST_DATE:
        allowed_fields.add("forecast_date")
    if ordering.lstrip("-") not in allowed_fields:
        raise ValidationError(
            {
                "ordering": (
                    "Unsupported Action Item ordering. Choose one of: "
                    f"{', '.join(sorted(allowed_fields))}."
                )
            }
        )
    return (ordering,) if ordering.lstrip("-") == "id" else (ordering, "id")


def action_item_scope_breakdown(queryset):
    grouped = list(
        queryset.order_by().annotate(
            ai_project_id=Coalesce(
                "milestone__project_id",
                "parent_action_item__milestone__project_id",
            ),
            ai_project_name=Coalesce(
                "milestone__project__project_name",
                "parent_action_item__milestone__project__project_name",
            ),
        )
        .values("ai_project_id", "ai_project_name")
        .annotate(count=Count("id", distinct=True))
    )
    rows = [
        {
            "scope": "project" if group["ai_project_id"] else "company",
            "project_id": group["ai_project_id"],
            "project_name": group["ai_project_name"],
            "count": group["count"],
        }
        for group in grouped
    ]
    return sorted(
        rows,
        key=lambda row: (-row["count"], row["project_name"] or ""),
    )


def count_action_items(user, payload):
    queryset = visible_action_items(user, payload)
    return response_payload(
        "count",
        rows=action_item_scope_breakdown(queryset),
        summary={"count": queryset.count()},
    )


def search_action_items(user, payload):
    limit = normalize_limit(payload.get("limit"))
    queryset = visible_action_items(user, payload).select_related(
        "milestone",
        "milestone__project",
        "parent_action_item__milestone",
        "parent_action_item__milestone__project",
        "company_profile",
    )
    if ACTION_ITEM_HAS_ASSIGNEES:
        queryset = queryset.prefetch_related("assignees")
    elif ACTION_ITEM_HAS_ASSIGNEE:
        queryset = queryset.select_related("assignee")
    queryset = queryset.order_by(
        *action_item_ordering((payload.get("filters") or {}).get("ordering"))
    )
    count = queryset.count()
    items = list(queryset[:limit])
    rows = [action_item_row(item) for item in items]
    sources = [action_item_source(item) for item in items]
    return response_payload("list", rows=rows, sources=sources, summary={"count": count}, has_more=count > limit)


def biggest_action_item_delay(user, payload):
    filters = payload.get("filters") or {}
    queryset = visible_action_items(user, payload)
    if not has_explicit_status_filter(filters):
        queryset = not_closed_status_queryset(queryset)
    queryset = queryset.filter(target_date__lt=timezone.localdate())
    queryset = queryset.select_related(
        "milestone",
        "milestone__project",
        "parent_action_item__milestone",
        "parent_action_item__milestone__project",
    )
    if ACTION_ITEM_HAS_ASSIGNEES:
        queryset = queryset.prefetch_related("assignees")
    elif ACTION_ITEM_HAS_ASSIGNEE:
        queryset = queryset.select_related("assignee")
    item = queryset.order_by("target_date", "id").first()
    if not item:
        return response_payload("list", rows=[], summary={"count": 0})
    row = action_item_row(item)
    return response_payload("list", rows=[row], sources=[action_item_source(item)], summary={"count": 1, "delay_days": row["delay_days"]})


def action_items_group_by_project(user, payload):
    queryset = visible_action_items(user, payload)
    rows = [
        {
            "project_id": row["project_id"],
            "project_name": row["project_name"],
            "count": row["count"],
        }
        for row in action_item_scope_breakdown(queryset)
        if row["scope"] == "project"
    ]
    return response_payload(
        "chart",
        rows=rows,
        summary={"count": sum(row["count"] for row in rows)},
        chart={"type": "pie", "labels": [row["project_name"] for row in rows], "values": [row["count"] for row in rows]},
    )


def action_items_group_by_milestone(user, payload):
    if payload.get("scope") != "project" or not (
        payload.get("project_id") or payload.get("project_name")
    ):
        return response_payload(
            "chart",
            rows=[],
            summary={"count": 0},
            warnings=["A project ID or exact project name is required for milestone grouping."],
        )

    queryset = visible_action_items(user, payload)
    grouped = list(
        queryset.order_by()
        .annotate(
            ai_milestone_id=Coalesce(
                "milestone_id",
                "parent_action_item__milestone_id",
            ),
            ai_milestone_name=Coalesce(
                "milestone__name",
                "parent_action_item__milestone__name",
            ),
            ai_phase_title=Coalesce(
                "milestone__phase_title",
                "parent_action_item__milestone__phase_title",
            ),
        )
        .exclude(ai_milestone_id__isnull=True)
        .values("ai_milestone_id", "ai_milestone_name", "ai_phase_title")
        .annotate(count=Count("id", distinct=True))
        .order_by("ai_milestone_id")
    )
    rows = [
        {
            "milestone_id": row["ai_milestone_id"],
            "milestone_name": row["ai_milestone_name"],
            "phase_title": row["ai_phase_title"],
            "count": row["count"],
        }
        for row in grouped
    ]
    return response_payload(
        "chart",
        rows=rows,
        summary={"count": sum(row["count"] for row in rows)},
        chart={
            "type": "bar",
            "labels": [
                row["phase_title"] or row["milestone_name"] or str(row["milestone_id"])
                for row in rows
            ],
            "values": [row["count"] for row in rows],
        },
    )


def action_items_financial_summary(user, payload):
    queryset = action_items_with_booked_annotations(visible_action_items(user, payload))
    summary = queryset.aggregate(
        count=Count("id", distinct=True),
        allocated_budget=Sum("allocated_budget"),
        allocated_hours=Sum("allocated_time"),
        booked_costs=Sum("booked_cost"),
        booked_hours=Sum("booked_hour"),
    )
    return response_payload(
        "summary",
        summary={
            "count": summary["count"] or 0,
            "allocated_budget": float_value(summary["allocated_budget"]),
            "allocated_hours": float_value(summary["allocated_hours"]),
            "booked_costs": float_value(summary["booked_costs"]),
            "booked_hours": float_value(summary["booked_hours"]),
            "status": (payload.get("filters") or {}).get("status"),
        },
    )


def action_items_closure_list(user, payload):
    limit = normalize_limit(payload.get("limit"))
    filters = payload.get("filters") or {}
    queryset = visible_action_items(user, payload).filter(closed_at__isnull=False)
    queryset = action_items_with_booked_annotations(queryset).select_related(
        "milestone",
        "milestone__project",
        "parent_action_item__milestone",
        "parent_action_item__milestone__project",
        "company_profile",
    )
    if ACTION_ITEM_HAS_ASSIGNEES:
        queryset = queryset.prefetch_related("assignees")
    elif ACTION_ITEM_HAS_ASSIGNEE:
        queryset = queryset.select_related("assignee")
    ordering = filters.get("ordering") or "closed_at"
    if ordering not in {"closed_at", "-closed_at"}:
        ordering = "closed_at"
    queryset = queryset.order_by(ordering, "id")
    count = queryset.count()
    items = list(queryset[:limit])
    return response_payload(
        "list",
        rows=[action_item_row(item) for item in items],
        sources=[action_item_source(item) for item in items],
        summary={"count": count},
        has_more=count > limit,
    )


def action_items_progress_summary(user, payload):
    queryset = visible_action_items(user, payload)
    rows = list(queryset.values("status").annotate(count=Count("id", distinct=True)).order_by("status"))
    total = sum(row["count"] for row in rows)
    open_count = not_closed_status_queryset(queryset).count()
    late_count = not_closed_status_queryset(
        queryset.filter(target_date__lt=timezone.localdate())
    ).count()
    closed_count = queryset.filter(status__iexact="closed").count()
    return response_payload(
        "chart",
        rows=rows,
        summary={
            "count": total,
            "open_action_items": open_count,
            "late_action_items": late_count,
            "closed_action_items": closed_count,
        },
        chart={
            "type": "pie",
            "labels": [row["status"] or "No status" for row in rows],
            "values": [row["count"] for row in rows],
        },
    )


def action_items_invoice_candidates(user, payload):
    return response_payload(
        "list",
        rows=[],
        summary={"count": 0},
        warnings=[
            "Action-item invoice-next logic is not available as a safe read-only query yet. "
            "Invoicing is currently driven by approved timesheets, expense cards, purchase orders, and invoice records, not a direct action-item invoice queue."
        ],
    )


def phase_row(phase: ProjectPhase):
    return {
        "id": phase.id,
        "type": "phase",
        "name": phase.name,
        "project_id": phase.project_id,
        "status": phase.status,
        "amount": float_value(phase.amount),
        "date": phase.date.isoformat() if phase.date else None,
        "purchase_order_id": phase.purchase_order_id,
        "url": f"/project-milestone-management?project_id={phase.project_id}&phase_id={phase.id}",
    }


def milestone_row(milestone: Milestone):
    return {
        "id": milestone.id,
        "type": "milestone",
        "name": milestone.name,
        "project_id": milestone.project_id,
        "status": milestone.status,
        "allocated_budget": float_value(milestone.allocated_budget),
        "allocated_hours": float_value(milestone.allocated_time),
        "start_date": milestone.start_date.isoformat() if milestone.start_date else None,
        "due_date": milestone.due_date.isoformat() if milestone.due_date else None,
        "closed_at": milestone.closed_at.isoformat() if milestone.closed_at else None,
        "url": f"/project-milestone-management?project_id={milestone.project_id}&milestone_id={milestone.id}",
    }


def phase_source(row):
    return source_ref(row["type"], row["id"], row["name"], row["url"], project_id=row["project_id"])


def phase_milestone_financial_summary(user, payload):
    project_id = ensure_project_visible(user, payload)
    if not project_id:
        return response_payload(
            "summary",
            summary={"count": 0},
            warnings=["Project ID is required for phase and milestone budget questions."],
        )

    limit = normalize_limit(payload.get("limit"))
    filters = payload.get("filters") or {}
    phases = ProjectPhase.objects.filter(project_id=project_id)
    milestones = Milestone.objects.filter(project_id=project_id)
    if filters.get("status"):
        phases = apply_status_filter(phases, filters["status"])
        milestones = apply_status_filter(milestones, filters["status"])

    phase_summary = phases.aggregate(count=Count("id"), total_amount=Sum("amount"))
    milestone_summary = milestones.aggregate(
        count=Count("id"),
        total_allocated_budget=Sum("allocated_budget"),
        total_allocated_hours=Sum("allocated_time"),
    )
    phase_rows = [phase_row(phase) for phase in phases.order_by("date", "id")[:limit]]
    remaining_limit = max(limit - len(phase_rows), 0)
    milestone_rows = [milestone_row(milestone) for milestone in milestones.order_by("due_date", "id")[:remaining_limit]]
    rows = phase_rows + milestone_rows

    return response_payload(
        "summary",
        rows=rows,
        sources=[phase_source(row) for row in rows],
        summary={
            "project_id": int(project_id),
            "phase_count": phase_summary["count"] or 0,
            "phase_total_amount": float_value(phase_summary["total_amount"]),
            "milestone_count": milestone_summary["count"] or 0,
            "milestone_total_allocated_budget": float_value(milestone_summary["total_allocated_budget"]),
            "milestone_total_allocated_hours": float_value(milestone_summary["total_allocated_hours"]),
            "count": (phase_summary["count"] or 0) + (milestone_summary["count"] or 0),
        },
        chart={
            "type": "bar",
            "labels": [row["name"] for row in phase_rows],
            "values": [row["amount"] for row in phase_rows],
        },
        has_more=(phase_summary["count"] or 0) + (milestone_summary["count"] or 0) > limit,
    )


def visible_purchase_orders(user, payload):
    ensure_purchase_order_view_permission(user)
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload)
    queryset = viewset_queryset(PurchaseOrderViewSet, user, params)
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)

    if project_id:
        queryset = queryset.filter(project_id=project_id)
    elif payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(Q(company_profile_id=company_profile_id) | Q(project__company_profile_id=company_profile_id))
    if filters.get("status"):
        _validate_choice_filter(
            queryset,
            filters["status"],
            field_name="status",
            configured_choices=_configured_project_or_company_choices(
                user,
                payload,
                "purchase_order_status",
            ),
        )
        queryset = queryset.filter(status__iexact=filters["status"])
    if filters.get("po_type"):
        queryset = queryset.filter(po_type=filters["po_type"])
    if filters.get("document_type"):
        queryset = queryset.filter(document_type=filters["document_type"])
    if filters.get("reference"):
        queryset = queryset.filter(Q(reference__icontains=filters["reference"]) | Q(customer_reference__icontains=filters["reference"]))
    if filters.get("po_date_from"):
        queryset = queryset.filter(po_date__gte=parse_date_value(filters["po_date_from"]))
    if filters.get("po_date_to"):
        queryset = queryset.filter(po_date__lte=parse_date_value(filters["po_date_to"]))
    return queryset.distinct()


def visible_invoices(user, payload):
    ensure_invoice_view_permission(user)
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload)
    queryset = viewset_queryset(InvoiceViewSet, user, params)
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)

    if project_id:
        queryset = queryset.filter(project_id=project_id)
    elif payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(Q(company_profile_id=company_profile_id) | Q(project__company_profile_id=company_profile_id))
    if filters.get("purchase_order_id"):
        queryset = queryset.filter(purchase_order_id=filters["purchase_order_id"])
    if filters.get("status"):
        _validate_choice_filter(
            queryset,
            filters["status"],
            field_name="status",
            configured_choices=_configured_project_or_company_choices(
                user,
                payload,
                "invoice_status",
            ),
            aliases={"paid"},
        )
        queryset = queryset.filter(status__iexact=filters["status"])
    if filters.get("document_type"):
        queryset = queryset.filter(document_type=filters["document_type"])
    if filters.get("invoice_date_from"):
        queryset = queryset.filter(invoice_date__gte=parse_date_value(filters["invoice_date_from"]))
    if filters.get("invoice_date_to"):
        queryset = queryset.filter(invoice_date__lte=parse_date_value(filters["invoice_date_to"]))
    if filters.get("search"):
        search = filters["search"]
        queryset = queryset.filter(Q(reference_number__icontains=search) | Q(status__icontains=search))
    return queryset.distinct()


def purchase_order_source(order: PurchaseOrder):
    return source_ref(
        "purchase_order",
        order.id,
        order.reference,
        f"/purchase-orders?id={order.id}",
        project_id=order.project_id,
    )


def invoice_source(invoice: Invoice):
    label = invoice.reference_number or f"Invoice {invoice.id}"
    return source_ref(
        "invoice",
        invoice.id,
        label,
        f"/billing-management?id={invoice.id}",
        project_id=invoice.project_id,
        purchase_order_id=invoice.purchase_order_id,
    )


def invoice_amount(invoice: Invoice) -> Decimal:
    return decimal_value(invoice.grand_total)


def active_invoice_queryset(queryset):
    """Invoices that currently represent a billable amount, excluding superseded rows."""
    return queryset.filter(
        document_type=Invoice.DocumentType.INVOICE,
        replacement_invoice__isnull=True,
    )


def effective_invoice_rows(queryset):
    invoices = list(active_invoice_queryset(queryset).select_related("project", "purchase_order"))
    invoice_ids = [invoice.id for invoice in invoices]
    credit_notes = list(
        queryset.filter(
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice_id__in=invoice_ids,
        ).select_related("project", "purchase_order", "source_invoice")
    )
    invoice_total = sum((invoice_amount(invoice) for invoice in invoices), Decimal("0"))
    credit_note_total = sum((invoice_amount(note) for note in credit_notes), Decimal("0"))
    return invoices, credit_notes, invoice_total, credit_note_total


def purchase_order_invoice_queryset(order: PurchaseOrder):
    return order.invoices.all()


def purchase_order_invoiced_amount(order: PurchaseOrder) -> Decimal:
    _, _, invoice_total, credit_note_total = effective_invoice_rows(purchase_order_invoice_queryset(order))
    return invoice_total + credit_note_total


def visible_invoice_queryset_for_purchase_orders(user, payload, purchase_orders):
    queryset = visible_invoices(user, payload)
    return queryset.filter(purchase_order_id__in=purchase_orders.values("id"))


def purchase_order_revised_amounts(order: PurchaseOrder) -> tuple[Decimal, Decimal]:
    if order.po_type != PurchaseOrder.POType.OUTGOING:
        return decimal_value(order.amount_excl_vat), decimal_value(order.amount_incl_vat)
    revised_incl = decimal_value(get_po_revised_amount_incl_vat(order))
    vat_multiplier = Decimal("1") + (decimal_value(order.vat_rate) / Decimal("100"))
    revised_excl = revised_incl / vat_multiplier if vat_multiplier else revised_incl
    return revised_excl, revised_incl


def purchase_order_row(
    order: PurchaseOrder,
    *,
    invoice_total: Decimal | None = None,
    credit_note_total: Decimal | None = None,
    invoice_count: int | None = None,
    credit_note_count: int | None = None,
):
    project = order.project
    if invoice_total is None or credit_note_total is None:
        invoices, credit_notes, calculated_invoice_total, calculated_credit_total = effective_invoice_rows(
            purchase_order_invoice_queryset(order)
        )
        invoice_total = calculated_invoice_total
        credit_note_total = calculated_credit_total
        invoice_count = len(invoices)
        credit_note_count = len(credit_notes)
    invoice_count = invoice_count or 0
    credit_note_count = credit_note_count or 0
    revised_excl_vat, revised_incl_vat = purchase_order_revised_amounts(order)
    net_invoiced_amount = invoice_total + credit_note_total
    return {
        "id": order.id,
        "reference": order.reference,
        "customer_reference": order.customer_reference,
        "project": {"id": project.id, "name": project.project_name} if project else None,
        "company_profile_id": order.company_profile_id,
        "status": order.status,
        "po_type": order.po_type,
        "document_type": order.document_type,
        "po_date": order.po_date.isoformat() if order.po_date else None,
        "currency": order.currency,
        "amount_excl_vat": float_value(revised_excl_vat),
        "amount_incl_vat": float_value(revised_incl_vat),
        "invoice_amount": float_value(invoice_total),
        "credit_note_amount": float_value(credit_note_total),
        "invoiced_amount": float_value(net_invoiced_amount),
        "remaining_balance": float_value(revised_incl_vat - net_invoiced_amount),
        "invoice_count": invoice_count,
        "credit_note_count": credit_note_count,
        "url": f"/purchase-orders?id={order.id}",
    }


def invoice_row(invoice: Invoice):
    project = invoice.project
    return {
        "id": invoice.id,
        "reference": invoice.reference_number,
        "reference_number": invoice.reference_number,
        "project": {"id": project.id, "name": project.project_name} if project else None,
        "company_profile_id": invoice.company_profile_id,
        "purchase_order_id": invoice.purchase_order_id,
        "purchase_order_reference": invoice.purchase_order.reference if invoice.purchase_order_id else None,
        "status": invoice.status,
        "document_type": invoice.document_type,
        "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "amount": float_value(invoice_amount(invoice)),
        "company_currency_amount": float_value(invoice.company_currency_amount)
        if invoice.company_currency_amount is not None
        else None,
        "url": f"/billing-management?id={invoice.id}",
    }


def purchase_orders_summary(user, payload):
    queryset = (
        visible_purchase_orders(user, payload)
        .filter(document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER, source_po__isnull=True)
        .prefetch_related("invoices", "change_notices")
        .select_related("project", "company_profile")
    )
    count = queryset.count()
    all_orders = list(queryset.order_by("-po_date", "-id"))
    invoice_queryset = visible_invoice_queryset_for_purchase_orders(user, payload, queryset)
    invoices, credit_notes, invoice_total, credit_note_total = effective_invoice_rows(invoice_queryset)
    currencies = sorted(queryset.exclude(currency__isnull=True).exclude(currency="").values_list("currency", flat=True).distinct())
    limit = normalize_limit(payload.get("limit"))
    orders = all_orders[:limit]
    invoice_totals_by_po = {}
    credit_totals_by_po = {}
    invoice_counts_by_po = {}
    credit_counts_by_po = {}
    for invoice in invoices:
        invoice_totals_by_po[invoice.purchase_order_id] = invoice_totals_by_po.get(invoice.purchase_order_id, Decimal("0")) + invoice_amount(invoice)
        invoice_counts_by_po[invoice.purchase_order_id] = invoice_counts_by_po.get(invoice.purchase_order_id, 0) + 1
    for note in credit_notes:
        credit_totals_by_po[note.purchase_order_id] = credit_totals_by_po.get(note.purchase_order_id, Decimal("0")) + invoice_amount(note)
        credit_counts_by_po[note.purchase_order_id] = credit_counts_by_po.get(note.purchase_order_id, 0) + 1
    rows = [
        purchase_order_row(
            order,
            invoice_total=invoice_totals_by_po.get(order.id, Decimal("0")),
            credit_note_total=credit_totals_by_po.get(order.id, Decimal("0")),
            invoice_count=invoice_counts_by_po.get(order.id, 0),
            credit_note_count=credit_counts_by_po.get(order.id, 0),
        )
        for order in orders
    ]
    currency_totals = {}
    for order in all_orders:
        revised_excl, revised_incl = purchase_order_revised_amounts(order)
        invoice_amount_for_order = invoice_totals_by_po.get(order.id, Decimal("0"))
        credit_amount_for_order = credit_totals_by_po.get(order.id, Decimal("0"))
        net_invoiced_for_order = invoice_amount_for_order + credit_amount_for_order
        bucket = currency_totals.setdefault(
            order.currency or "Unknown",
            {
                "amount_excl_vat": Decimal("0"),
                "amount_incl_vat": Decimal("0"),
                "invoice_amount": Decimal("0"),
                "credit_note_amount": Decimal("0"),
                "invoiced_amount": Decimal("0"),
                "remaining_balance": Decimal("0"),
            },
        )
        bucket["amount_excl_vat"] += revised_excl
        bucket["amount_incl_vat"] += revised_incl
        bucket["invoice_amount"] += invoice_amount_for_order
        bucket["credit_note_amount"] += credit_amount_for_order
        bucket["invoiced_amount"] += net_invoiced_for_order
        bucket["remaining_balance"] += revised_incl - net_invoiced_for_order
    total_excl_vat = sum((value["amount_excl_vat"] for value in currency_totals.values()), Decimal("0"))
    total_incl_vat = sum((value["amount_incl_vat"] for value in currency_totals.values()), Decimal("0"))
    net_invoiced_amount = invoice_total + credit_note_total
    warnings = []
    if len(currency_totals) > 1:
        warnings.append("Purchase orders use multiple currencies; authoritative financial totals are grouped by currency and are not added together.")
    has_single_currency = len(currency_totals) <= 1
    return response_payload(
        "summary",
        rows=rows,
        sources=[purchase_order_source(order) for order in orders],
        summary={
            "count": count,
            "total_amount_excl_vat": float_value(total_excl_vat) if has_single_currency else None,
            "total_amount_incl_vat": float_value(total_incl_vat) if has_single_currency else None,
            "invoice_amount": float_value(invoice_total) if has_single_currency else None,
            "credit_note_amount": float_value(credit_note_total) if has_single_currency else None,
            "invoiced_amount": float_value(net_invoiced_amount) if has_single_currency else None,
            "remaining_balance": float_value(total_incl_vat - net_invoiced_amount) if has_single_currency else None,
            "currencies": currencies,
            "currency_totals": {
                currency: {key: float_value(value) for key, value in totals.items()}
                for currency, totals in currency_totals.items()
            },
        },
        warnings=warnings,
        has_more=count > limit,
    )


def purchase_order_balance(user, payload):
    filters = payload.get("filters") or {}
    purchase_order_id = filters.get("purchase_order_id") or payload.get("purchase_order_id")
    purchase_order_reference = filters.get("reference")
    queryset = (
        visible_purchase_orders(user, payload)
        .filter(document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER, source_po__isnull=True)
        .select_related("project", "company_profile")
        .prefetch_related("change_notices")
    )
    if purchase_order_id:
        queryset = queryset.filter(id=purchase_order_id)
    elif purchase_order_reference:
        queryset = queryset.filter(
            Q(reference__iexact=purchase_order_reference)
            | Q(customer_reference__iexact=purchase_order_reference)
        )
    orders = list(queryset[:normalize_limit(payload.get("limit"))])
    invoice_queryset = visible_invoice_queryset_for_purchase_orders(user, payload, queryset)
    invoices, credit_notes, _, _ = effective_invoice_rows(invoice_queryset)
    invoice_totals_by_po = {}
    credit_totals_by_po = {}
    invoice_counts_by_po = {}
    credit_counts_by_po = {}
    for invoice in invoices:
        invoice_totals_by_po[invoice.purchase_order_id] = invoice_totals_by_po.get(invoice.purchase_order_id, Decimal("0")) + invoice_amount(invoice)
        invoice_counts_by_po[invoice.purchase_order_id] = invoice_counts_by_po.get(invoice.purchase_order_id, 0) + 1
    for note in credit_notes:
        credit_totals_by_po[note.purchase_order_id] = credit_totals_by_po.get(note.purchase_order_id, Decimal("0")) + invoice_amount(note)
        credit_counts_by_po[note.purchase_order_id] = credit_counts_by_po.get(note.purchase_order_id, 0) + 1
    rows = [
        purchase_order_row(
            order,
            invoice_total=invoice_totals_by_po.get(order.id, Decimal("0")),
            credit_note_total=credit_totals_by_po.get(order.id, Decimal("0")),
            invoice_count=invoice_counts_by_po.get(order.id, 0),
            credit_note_count=credit_counts_by_po.get(order.id, 0),
        )
        for order in orders
    ]
    invoice_details = [invoice_row(invoice) for invoice in invoices]
    credit_note_details = [invoice_row(note) for note in credit_notes]
    for row in rows:
        row["invoices"] = [item for item in invoice_details if item["purchase_order_id"] == row["id"]]
        row["credit_notes"] = [item for item in credit_note_details if item["purchase_order_id"] == row["id"]]
    return response_payload(
        "list",
        rows=rows,
        sources=[purchase_order_source(order) for order in orders],
        summary={"count": queryset.count()},
        warnings=[
            "Remaining balance uses the revised purchase order amount including VAT, active linked invoices, and signed credit-note deductions."
        ],
        has_more=queryset.count() > len(rows),
    )


def search_invoices(user, payload):
    limit = normalize_limit(payload.get("limit"))
    filters = payload.get("filters") or {}
    queryset = visible_invoices(user, payload).select_related("project", "company_profile", "purchase_order")
    if filters.get("paid_only"):
        queryset = queryset.filter(status__iexact="paid")
        if not filters.get("document_type"):
            queryset = queryset.filter(
                document_type=Invoice.DocumentType.INVOICE
            )
    queryset = queryset.order_by("-invoice_date", "-id")
    count = queryset.count()
    invoices = list(queryset[:limit])
    warnings = []
    if filters.get("download_all"):
        warnings.append("Bulk invoice download is not enabled in the AI assistant. I can list invoice metadata and source links only.")
    return response_payload(
        "list",
        rows=[invoice_row(invoice) for invoice in invoices],
        sources=[invoice_source(invoice) for invoice in invoices],
        summary={
            "count": count,
            "returned_amount": float_value(sum((invoice_amount(invoice) for invoice in invoices), Decimal("0"))),
        },
        warnings=warnings,
        has_more=count > limit,
    )


def visible_documents(user, payload):
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload, project_key="folder__project", company_key="folder__company_profile")
    if payload.get("folder_id"):
        params["folder"] = payload["folder_id"]
    queryset = viewset_queryset(DocumentViewSet, user, params)
    filters = payload.get("filters") or {}
    warnings = []
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)

    if payload.get("folder_id"):
        queryset = queryset.filter(folder_id=payload["folder_id"])
    if project_id:
        queryset = queryset.filter(folder__project_id=project_id)
    elif payload.get("scope") == "accessible_projects":
        queryset = queryset.filter(folder__project__isnull=False)
    elif payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(
            Q(folder__company_profile_id=company_profile_id)
            | Q(folder__project__company_profile_id=company_profile_id)
            | Q(folder__project__user__company_id=company_profile_id)
        )

    if filters.get("content_query"):
        warnings.append("Document content search is not enabled. Only document metadata is searchable.")
    if filters.get("due_within_days") or filters.get("due_date_from") or filters.get("due_date_to"):
        warnings.append("Document due-date metadata is not available in this version. Documents can be searched by metadata such as status, type, author, folder, uploaded date, and updated date.")
    if filters.get("status"):
        _validate_choice_filter(
            queryset,
            filters["status"],
            field_name="status",
            configured_choices=_configured_project_or_company_choices(
                user,
                payload,
                "document_status",
            ),
        )
        queryset = queryset.filter(status__iexact=filters["status"])
    if filters.get("document_type"):
        queryset = queryset.filter(document_type__iexact=filters["document_type"])
    if filters.get("author") == "current_user":
        queryset = queryset.filter(author=user)
    elif filters.get("author"):
        queryset = queryset.filter(author_id=filters["author"])
    if filters.get("assignee") == "current_user":
        queryset = queryset.filter(assignee=user)
    elif filters.get("assignee"):
        queryset = queryset.filter(assignee_id=filters["assignee"])
    if filters.get("created_by") == "current_user":
        queryset = queryset.filter(created_by=user)
    elif filters.get("created_by"):
        queryset = queryset.filter(created_by_id=filters["created_by"])
    if filters.get("uploaded_at_from"):
        queryset = queryset.filter(uploaded_at__gte=parse_datetime_value(filters["uploaded_at_from"]))
    if filters.get("uploaded_at_to"):
        queryset = queryset.filter(uploaded_at__lte=parse_datetime_value(filters["uploaded_at_to"]))
    if filters.get("updated_at_from"):
        queryset = queryset.filter(updated_at__gte=parse_datetime_value(filters["updated_at_from"]))
    if filters.get("updated_at_to"):
        queryset = queryset.filter(updated_at__lte=parse_datetime_value(filters["updated_at_to"]))
    if filters.get("search"):
        search = filters["search"]
        queryset = queryset.filter(
            Q(file_name__icontains=search)
            | Q(document_type__icontains=search)
            | Q(status__icontains=search)
            | Q(full_document_number__icontains=search)
        )
    return queryset.distinct(), warnings


def visible_object_assessment_documents(user, payload, *, validate_filters=True):
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload)
    queryset = viewset_queryset(ObjectAssessmentViewSet, user, params)
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)

    if project_id:
        queryset = queryset.filter(project_id=project_id)
    elif payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(
            Q(project__company_profile_id=company_profile_id)
            | Q(project__user__company_id=company_profile_id)
        )

    if filters.get("status"):
        # Document queries report Object Assessment totals only as secondary
        # diagnostics. A valid Document status does not need to exist in the
        # Object Assessment module, so validate choices only when Object
        # Assessments are the requested entity.
        if validate_filters:
            _validate_choice_filter(
                queryset,
                filters["status"],
                field_name="status",
            )
        queryset = apply_status_filter(queryset, filters["status"])
    if filters.get("document_type"):
        queryset = apply_status_filter(queryset, filters["document_type"], field_name="document_type")
    if filters.get("author") == "current_user":
        queryset = queryset.filter(author=user)
    elif filters.get("author"):
        queryset = queryset.filter(author_id=filters["author"])
    if filters.get("assignee") == "current_user":
        queryset = queryset.filter(assignee=user)
    elif filters.get("assignee"):
        queryset = queryset.filter(assignee_id=filters["assignee"])
    if filters.get("created_by") == "current_user":
        queryset = queryset.filter(created_by=user)
    elif filters.get("created_by"):
        queryset = queryset.filter(created_by_id=filters["created_by"])
    if filters.get("uploaded_at_from"):
        queryset = queryset.filter(created_at__gte=parse_datetime_value(filters["uploaded_at_from"]))
    if filters.get("uploaded_at_to"):
        queryset = queryset.filter(created_at__lte=parse_datetime_value(filters["uploaded_at_to"]))
    if filters.get("updated_at_from"):
        queryset = queryset.filter(updated_at__gte=parse_datetime_value(filters["updated_at_from"]))
    if filters.get("updated_at_to"):
        queryset = queryset.filter(updated_at__lte=parse_datetime_value(filters["updated_at_to"]))
    if filters.get("search"):
        search = filters["search"]
        queryset = queryset.filter(
            Q(record_title__icontains=search)
            | Q(record_type__icontains=search)
            | Q(document_type__icontains=search)
            | Q(status__icontains=search)
            | Q(file_name__icontains=search)
            | Q(identification_number__icontains=search)
        )
    return queryset.distinct()


def visible_document_folders(user, payload):
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload, project_key="project", company_key="company_profile")
    queryset = viewset_queryset(FolderViewSet, user, params)
    filters = payload.get("filters") or {}
    project_id = payload.get("project_id") or filters.get("project_id")
    company_profile_id = resolve_company_profile_id(user, payload)

    if payload.get("folder_id"):
        queryset = queryset.filter(id=payload["folder_id"])
    if project_id:
        queryset = queryset.filter(project_id=project_id)
    elif payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(
            Q(company_profile_id=company_profile_id)
            | Q(project__company_profile_id=company_profile_id)
            | Q(project__user__company_id=company_profile_id)
        )
    if filters.get("search"):
        queryset = queryset.filter(name__icontains=filters["search"])
    return queryset.distinct()


def document_summary_counts(document_count, assessment_count, folder_count):
    return {
        "count": document_count,
        "document_metadata_count": document_count,
        "folder_document_count": document_count,
        "object_assessment_record_count": assessment_count,
        "folder_count": folder_count,
        "library_item_count": folder_count + document_count,
    }


def document_row(document: Document):
    folder = document.folder
    project = folder.project if folder else None
    return {
        "id": document.id,
        "file_name": document.file_name or document.file.name,
        "status": document.status,
        "document_type": document.document_type,
        "full_document_number": document.full_document_number,
        "project": {"id": project.id, "name": project.project_name} if project else None,
        "folder_id": folder.id if folder else None,
        "author": compact_user(document.author),
        "assignee": compact_user(document.assignee),
        "uploaded_at": document.uploaded_at.isoformat() if document.uploaded_at else None,
        "updated_at": document.updated_at.isoformat() if document.updated_at else None,
    }


def object_assessment_document_row(assessment: ObjectAssessment):
    return {
        "id": assessment.id,
        "file_name": assessment.file_name or assessment.record_title or assessment.file.name,
        "status": assessment.status,
        "document_type": assessment.document_type or assessment.record_type,
        "full_document_number": assessment.identification_number,
        "project": {"id": assessment.project_id, "name": assessment.project.project_name} if assessment.project_id else None,
        "folder_id": None,
        "author": compact_user(assessment.author),
        "assignee": compact_user(assessment.assignee),
        "uploaded_at": assessment.created_at.isoformat() if assessment.created_at else None,
        "updated_at": assessment.updated_at.isoformat() if assessment.updated_at else None,
        "source_kind": "object_assessment",
    }


def document_source(document: Document):
    project = document.folder.project if document.folder else None
    url = f"/project-document-management?project_id={project.id}&id={document.id}" if project else f"/company-documents?id={document.id}"
    return source_ref("document", document.id, document.file_name or str(document.file), url, project_id=project.id if project else None)


def object_assessment_document_source(assessment: ObjectAssessment):
    label = assessment.record_title or assessment.file_name or f"Object Assessment {assessment.id}"
    return source_ref(
        "object_assessment",
        assessment.id,
        label,
        f"/object-assessment?project_id={assessment.project_id}&id={assessment.id}",
        project_id=assessment.project_id,
    )


def count_documents(user, payload):
    queryset, warnings = visible_documents(user, payload)
    # Object Assessment records are a separate module. They are reported as a
    # diagnostic count only and never included in the authoritative Document count.
    assessment_queryset = visible_object_assessment_documents(
        user, payload, validate_filters=False
    )
    folders = visible_document_folders(user, payload)
    return response_payload(
        "count",
        summary=document_summary_counts(queryset.count(), assessment_queryset.count(), folders.count()),
        warnings=warnings,
    )


def search_documents(user, payload):
    limit = normalize_limit(payload.get("limit"))
    queryset, warnings = visible_documents(user, payload)
    queryset = queryset.select_related("folder", "folder__project", "author", "assignee").order_by("-updated_at", "-id")
    assessment_queryset = visible_object_assessment_documents(
        user, payload, validate_filters=False
    )
    folder_count = visible_document_folders(user, payload).count()
    document_count = queryset.count()
    assessment_count = assessment_queryset.count()
    count = document_count
    items = list(queryset[:limit])
    return response_payload(
        "list",
        rows=[document_row(item) for item in items],
        sources=[document_source(item) for item in items],
        summary=document_summary_counts(document_count, assessment_count, folder_count),
        warnings=warnings,
        has_more=count > limit,
    )


def count_object_assessments(user, payload):
    queryset = visible_object_assessment_documents(user, payload)
    return response_payload(
        "count",
        summary={"count": queryset.count(), "object_assessment_record_count": queryset.count()},
    )


def search_object_assessments(user, payload):
    limit = normalize_limit(payload.get("limit"))
    queryset = visible_object_assessment_documents(user, payload).select_related(
        "project", "author", "assignee"
    ).order_by("-updated_at", "-id")
    count = queryset.count()
    items = list(queryset[:limit])
    return response_payload(
        "list",
        rows=[object_assessment_document_row(item) for item in items],
        sources=[object_assessment_document_source(item) for item in items],
        summary={"count": count, "object_assessment_record_count": count},
        has_more=count > limit,
    )


def documents_group_by_status(user, payload):
    queryset, warnings = visible_documents(user, payload)
    assessment_queryset = visible_object_assessment_documents(
        user, payload, validate_filters=False
    )
    grouped = {
        (row["status"] or "No status"): row["count"]
        for row in queryset.values("status").annotate(count=Count("id", distinct=True))
    }
    rows = [{"status": key, "count": grouped[key]} for key in sorted(grouped)]
    return response_payload(
        "chart",
        rows=rows,
        summary={
            "count": sum(row["count"] for row in rows),
            "document_metadata_count": queryset.count(),
            "object_assessment_record_count": assessment_queryset.count(),
        },
        chart={
            "type": "pie",
            "labels": [row["status"] or "No status" for row in rows],
            "values": [row["count"] for row in rows],
        },
        warnings=warnings,
    )


def visible_calendar_events(user, payload):
    filters = payload.get("filters") or {}
    scope = payload.get("scope") or "current_user"
    project_id = payload.get("project_id") or filters.get("project_id")

    if project_id is not None:
        ensure_project_visible(user, payload)
        project = Project.objects.filter(id=project_id).first()
        if not project:
            return CalendarEvent.objects.none()
        return CalendarEventPermission("view", "project").filter_queryset(
            CalendarEvent.objects.all(),
            user,
            {"project": project},
        )

    company_queryset = CalendarEventPermission("view", "company").filter_queryset(
        CalendarEvent.objects.all(), user, {}
    )
    if scope == "company":
        queryset = company_queryset
    else:
        project_queryset = viewset_queryset(ProjectViewSet, user, {})
        visible_event_ids = (
            set()
            if scope == "accessible_projects"
            else set(company_queryset.values_list("id", flat=True))
        )
        for project in project_queryset.only("id"):
            visible_event_ids.update(
                CalendarEventPermission("view", "project")
                .filter_queryset(CalendarEvent.objects.all(), user, {"project": project})
                .values_list("id", flat=True)
            )
        queryset = CalendarEvent.objects.filter(id__in=visible_event_ids)

    if scope == "current_user" or filters.get("personal"):
        if user.is_freelancer:
            queryset = queryset.filter(Q(freelancer=user) | Q(user=user) | Q(assignees=user)).distinct()
        else:
            queryset = queryset.filter(Q(created_by=user) | Q(assignees=user) | Q(user=user)).distinct()
    return queryset


def calendar_row(event: CalendarEvent):
    private = bool(event.is_private)
    return {
        "id": event.id,
        "name": "Private event" if private else event.name,
        "description": None if private else event.description,
        "is_private": private,
        "start_date": event.start_date.isoformat() if event.start_date else None,
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "project_id": getattr(event, "project_id", None),
        "project_name": event.project.project_name if event.project_id else None,
        "company_profile_id": getattr(event, "company_profile_id", None),
        "scope_type": "project" if event.project_id else "company",
        "meeting_link": None if private else event.meeting_link,
    }


def filtered_calendar_events(user, payload):
    filters = payload.get("filters") or {}
    if payload.get("scope") == "project" and not (payload.get("project_id") or filters.get("project_id")):
        return None, ["Project ID is required for project calendar questions."]
    queryset = visible_calendar_events(user, payload)

    start = parse_datetime_value(filters.get("start_datetime"))
    end = parse_datetime_value(filters.get("end_datetime"))
    if filters.get("date") and not start and not end:
        start, end = date_bounds_for_day(filters["date"], request_timezone(filters))
    if start and end:
        queryset = queryset.filter(start_date__lte=end, end_date__gte=start)

    return queryset, []


def count_calendar(user, payload):
    queryset, warnings = filtered_calendar_events(user, payload)
    return response_payload(
        "count",
        summary={"count": queryset.count() if queryset is not None else 0},
        warnings=warnings,
    )


def search_calendar(user, payload):
    limit = normalize_limit(payload.get("limit"))
    queryset, warnings = filtered_calendar_events(user, payload)
    if queryset is None:
        return response_payload("list", summary={"count": 0}, warnings=warnings)

    queryset = queryset.select_related("project", "company_profile")
    if (payload.get("filters") or {}).get("ordering") == "project":
        queryset = queryset.order_by("project__project_name", "start_date", "id")
    else:
        queryset = queryset.order_by("start_date", "id")
    count = queryset.count()
    items = list(queryset[:limit])
    sources = [
        source_ref(
            "calendar_event",
            item.id,
            "Private event" if item.is_private else item.name,
            f"/calendar-management?id={item.id}",
            project_id=getattr(item, "project_id", None),
        )
        for item in items
    ]
    return response_payload(
        "list",
        rows=[calendar_row(item) for item in items],
        sources=sources,
        summary={"count": count},
        warnings=warnings,
        has_more=count > limit,
    )


def find_free_calendar_slot(user, payload):
    filters = payload.get("filters") or {}
    duration = int(filters.get("duration_minutes") or payload.get("duration_minutes") or 30)
    start = parse_datetime_value(filters.get("start_datetime"))
    end = parse_datetime_value(filters.get("end_datetime"))
    if filters.get("date") and not start and not end:
        day = parse_date_value(filters["date"])
        if not day:
            return response_payload("availability", summary={"available": False}, warnings=["A valid date is required."])
        tz = request_timezone(filters)
        start = timezone.make_aware(datetime.combine(day, time(hour=6)), tz)
        end = timezone.make_aware(datetime.combine(day, time(hour=20)), tz)
    if not start or not end or end <= start:
        return response_payload("availability", summary={"available": False}, warnings=["A valid date or start/end datetime range is required."])

    events = visible_calendar_events(user, {**payload, "scope": "current_user"}).filter(start_date__lt=end, end_date__gt=start).order_by("start_date")
    cursor = start
    for event in events:
        if event.start_date - cursor >= timedelta(minutes=duration):
            break
        if event.end_date > cursor:
            cursor = event.end_date
    available = cursor + timedelta(minutes=duration) <= end
    response = response_payload(
        "availability",
        summary={
            "available": available,
            "slot_start": cursor.isoformat() if available else None,
            "slot_end": (cursor + timedelta(minutes=duration)).isoformat() if available else None,
            "duration_minutes": duration,
        },
        warnings=[] if available else ["No free slot was found in the requested range."],
    )
    if not is_outlook_token_valid(user):
        response["warnings"].append(
            "This availability was checked against Links(R) calendar events only. "
            "Outlook is not connected or its authorization has expired, so Outlook availability was not verified."
        )
        response["ui_actions"] = [_outlook_connect_action(user, filters)]
    return response


def _normalized_contact_text(value):
    return " ".join(str(value or "").casefold().split())


def _contact_candidate(*, source, candidate_id, name, email):
    return {
        "source": source,
        "id": candidate_id,
        "name": str(name or email or "").strip(),
        "email": str(email or "").strip(),
    }


def resolve_calendar_attendees(user, payload):
    """Resolve attendee names without exposing contacts outside the user's scope."""
    queries = (payload.get("filters") or {}).get("attendee_queries") or []
    candidates = []

    company = getattr(user, "company", None)
    internal_users = User.objects.filter(id=user.id)
    if company:
        internal_users = User.objects.filter(company=company)
    for candidate in internal_users.exclude(email__isnull=True).exclude(email=""):
        candidates.append(
            _contact_candidate(
                source="links_user",
                candidate_id=candidate.id,
                name=getattr(candidate, "full_name", None) or candidate.email,
                email=candidate.email,
            )
        )

    can_view_contacts = ContactPermission("view", "company")._check_view_permission(
        user,
        None,
    )
    if can_view_contacts:
        contact_queryset = ContactUser.objects.filter(is_deleted=False)
        if getattr(user, "is_freelancer", False):
            contact_queryset = contact_queryset.filter(user=user)
        elif company:
            contact_queryset = contact_queryset.filter(company_profile=company)
        else:
            contact_queryset = ContactUser.objects.none()
        for contact in contact_queryset.exclude(email=""):
            candidates.append(
                _contact_candidate(
                    source="links_contact",
                    candidate_id=contact.id,
                    name=f"{contact.name or ''} {contact.surname or ''}".strip(),
                    email=contact.email,
                )
            )

    outlook_warning = None
    if is_outlook_token_valid(user):
        try:
            response = get_outlook_contacts(
                user.outlook_credentials.outlook_access_token
            )
            if response.status_code == 200:
                for contact in response.json().get("value", []):
                    display_name = contact.get("displayName") or ""
                    for address in contact.get("emailAddresses") or []:
                        email = (address or {}).get("address")
                        if email:
                            candidates.append(
                                _contact_candidate(
                                    source="outlook_contact",
                                    candidate_id=contact.get("id"),
                                    name=display_name or email,
                                    email=email,
                                )
                            )
            else:
                outlook_warning = "Outlook contacts could not be checked."
        except Exception:
            outlook_warning = "Outlook contacts could not be checked."

    deduped = {}
    for candidate in candidates:
        email_key = _normalized_contact_text(candidate["email"])
        if email_key:
            deduped.setdefault(email_key, candidate)
    candidates = list(deduped.values())

    resolutions = []
    for raw_query in queries:
        query = str(raw_query).strip()
        normalized_query = _normalized_contact_text(query)
        exact_email = [
            item
            for item in candidates
            if _normalized_contact_text(item["email"]) == normalized_query
        ]
        exact_name = [
            item
            for item in candidates
            if _normalized_contact_text(item["name"]) == normalized_query
        ]
        partial = [
            item
            for item in candidates
            if normalized_query
            and (
                normalized_query in _normalized_contact_text(item["name"])
                or normalized_query in _normalized_contact_text(item["email"])
            )
        ]
        matches = exact_email or exact_name or partial
        if len(matches) == 1:
            status_value = "resolved"
        elif len(matches) > 1:
            status_value = "ambiguous"
        else:
            status_value = "not_found"
        resolutions.append(
            {
                "query": query,
                "status": status_value,
                "matches": matches[:10],
            }
        )

    warnings = [outlook_warning] if outlook_warning else []
    return response_payload(
        "attendee_resolution",
        rows=resolutions,
        summary={
            "count": len(resolutions),
            "resolved_count": sum(
                item["status"] == "resolved" for item in resolutions
            ),
        },
        warnings=warnings,
    )


def _calendar_event_create_permission(user):
    return CalendarEventPermission("create", "company")._check_create_permission(user, {})


def _calendar_event_datetime_filters(filters):
    start = parse_datetime_value(filters.get("start_datetime") or filters.get("start_date"))
    end = parse_datetime_value(filters.get("end_datetime") or filters.get("end_date"))
    duration_minutes = filters.get("duration_minutes")
    if start and not end and duration_minutes:
        end = start + timedelta(minutes=int(duration_minutes))
    return start, end


def _outlook_connect_action(user, filters):
    solution_name = filters.get("solution") or get_user_active_solution(user.id)
    return {
        "name": "open_outlook_calendar_connection",
        "label": "Connect Outlook calendar",
        "tooltip": "Connect or reconnect Outlook calendar access.",
        "icon": "calendar-plus",
        "payload": {
            "url": build_outlook_oauth_url(user, solution_name, redirect_to="ai_assistant"),
            "target": "_blank",
        },
    }


def _calendar_confirmation_payload(user, title, description, start, end, assignee_ids, attendee_emails, create_outlook, online_meeting):
    return {
        "user_id": user.id,
        "title": title,
        "description": description,
        "start_datetime": start.isoformat(),
        "end_datetime": end.isoformat(),
        "assignee_ids": [int(value) for value in assignee_ids],
        "attendee_emails": sorted(str(email).strip().lower() for email in attendee_emails if str(email).strip()),
        "create_outlook_event": bool(create_outlook),
        "online_meeting": bool(online_meeting),
    }


def _calendar_confirmation_token(payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return signing.TimestampSigner(salt="ai-tools-calendar-confirmation").sign(serialized)


def _calendar_confirmation_is_valid(token, expected_payload):
    if not token:
        return False
    try:
        serialized = signing.TimestampSigner(salt="ai-tools-calendar-confirmation").unsign(
            token,
            max_age=CALENDAR_CONFIRMATION_MAX_AGE_SECONDS,
        )
    except signing.BadSignature:
        return False
    try:
        payload = json.loads(serialized)
    except json.JSONDecodeError:
        return False
    return payload == expected_payload


def _calendar_confirmation_response(user, filters, title, description, start, end, assignee_ids, attendee_emails, create_outlook, online_meeting, warnings=None):
    confirmation_payload = _calendar_confirmation_payload(
        user,
        title,
        description,
        start,
        end,
        assignee_ids,
        attendee_emails,
        create_outlook,
        online_meeting,
    )
    response = response_payload(
        "confirmation",
        summary={
            "ready": True,
            "requires_confirmation": True,
            "title": title,
            "description": description,
            "start_datetime": start.isoformat(),
            "end_datetime": end.isoformat(),
            "assignee_ids": assignee_ids,
            "attendee_emails": attendee_emails,
            "create_outlook_event": create_outlook,
            "online_meeting": online_meeting,
            "confirmation_token": _calendar_confirmation_token(confirmation_payload),
        },
        warnings=warnings or ["Do not create the calendar event until the user explicitly confirms these details."],
    )
    if create_outlook and not is_outlook_token_valid(user):
        response["warnings"].append("Outlook calendar access must be connected before an Outlook event can be created.")
        response["ui_actions"] = [_outlook_connect_action(user, filters)]
    return response


def create_calendar_event(user, payload):
    filters = payload.get("filters") or {}
    confirmed = bool(filters.get("confirmed") or payload.get("confirmed"))
    title = filters.get("title") or filters.get("name")
    description = filters.get("description") or ""
    start, end = _calendar_event_datetime_filters(filters)
    assignee_ids = filters.get("assignee_ids") or []
    attendee_emails = filters.get("attendee_emails") or []
    create_outlook = bool(filters.get("create_outlook_event", True))
    online_meeting = bool(filters.get("online_meeting") or filters.get("teams_meeting"))

    if not title or not start or not end:
        return response_payload(
            "confirmation",
            summary={"ready": False},
            warnings=["Calendar event title, start_datetime, and end_datetime or duration_minutes are required."],
        )
    if end <= start:
        return response_payload(
            "confirmation",
            summary={"ready": False},
            warnings=["Calendar event end time must be after the start time."],
        )
    expected_confirmation = _calendar_confirmation_payload(
        user,
        title,
        description,
        start,
        end,
        assignee_ids,
        attendee_emails,
        create_outlook,
        online_meeting,
    )
    if not confirmed:
        return _calendar_confirmation_response(
            user,
            filters,
            title,
            description,
            start,
            end,
            assignee_ids,
            attendee_emails,
            create_outlook,
            online_meeting,
        )
    if not _calendar_confirmation_is_valid(filters.get("confirmation_token"), expected_confirmation):
        return _calendar_confirmation_response(
            user,
            filters,
            title,
            description,
            start,
            end,
            assignee_ids,
            attendee_emails,
            create_outlook,
            online_meeting,
            warnings=[
                "No calendar event has been created yet. Stop and ask the user to explicitly confirm these exact details first; only then call this tool again with the returned confirmation_token.",
            ],
        )
    if create_outlook and not is_outlook_token_valid(user):
        return _calendar_confirmation_response(
            user,
            filters,
            title,
            description,
            start,
            end,
            assignee_ids,
            attendee_emails,
            create_outlook,
            online_meeting,
            warnings=[
                "No calendar event has been created. Connect Outlook with calendar and contacts access, then confirm these details again.",
            ],
        )
    if not _calendar_event_create_permission(user):
        raise PermissionDenied("You do not have permission to create calendar events.")

    serializer_data = {
        "name": title,
        "description": description,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "assignees": assignee_ids,
    }

    request_context = ToolRequest(user=user, query_params={}, method="POST", data=serializer_data)
    with transaction.atomic():
        serializer = CalendarEventSerializer(
            data=serializer_data,
            context={"request": request_context},
        )
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as exc:
            return response_payload("confirmation", summary={"ready": False}, warnings=[str(exc.detail)])
        event = serializer.save()

    outlook_result = None
    warnings = []
    if create_outlook:
        outlook_result = create_outlook_calendar_event(
            user,
            event,
            attendee_emails=attendee_emails,
            online_meeting=online_meeting,
        )
        if not outlook_result.get("success"):
            warnings.append(
                "The Sharemundo calendar event was created, but Outlook event creation failed. "
                "Reconnect Outlook with calendar write permission and try again if Outlook sync is required."
            )
            if outlook_result.get("requires_reconnect"):
                warnings.append("Outlook calendar connection is missing or expired.")

    row = calendar_row(event)
    row.update(
        {
            "outlook_synced": bool(outlook_result and outlook_result.get("success")),
            "outlook_result": outlook_result,
        }
    )
    response = response_payload(
        "created",
        rows=[row],
        sources=[source_ref("calendar_event", event.id, event.name, f"/calendar-management?id={event.id}")],
        summary={
            "created": True,
            "calendar_event_id": event.id,
            "outlook_synced": bool(outlook_result and outlook_result.get("success")),
        },
        warnings=warnings,
    )
    if outlook_result and outlook_result.get("requires_reconnect"):
        response["ui_actions"] = [_outlook_connect_action(user, filters)]
    return response


def visible_projects(user, payload):
    ensure_project_visible(user, payload)
    params = scoped_request_params(user, payload)
    queryset = viewset_queryset(ProjectViewSet, user, params)
    filters = payload.get("filters") or {}
    company_profile_id = resolve_company_profile_id(user, payload)
    if payload.get("scope") == "company" and company_profile_id:
        queryset = queryset.filter(Q(company_profile_id=company_profile_id) | Q(user__company_id=company_profile_id))
    if filters.get("status"):
        _validate_choice_filter(
            queryset,
            filters["status"],
            field_name="project_status",
            configured_choices=[
                str(value)
                for value, _label in Project._meta.get_field("project_status").choices
            ],
        )
        queryset = queryset.filter(project_status__iexact=filters["status"])
    if filters.get("search"):
        search = filters["search"]
        queryset = queryset.filter(Q(project_name__icontains=search) | Q(project_description__icontains=search))
    return queryset.distinct()


def projects_summary(user, payload):
    limit = normalize_limit(payload.get("limit"))
    projects = visible_projects(user, payload).order_by("project_name", "id")
    rows = []
    for project in projects[:limit]:
        action_items = visible_action_items(user, {"scope": "project", "project_id": project.id, "filters": payload.get("filters") or {}})
        rows.append({
            "project_id": project.id,
            "project_name": project.project_name,
            "project_type": project.project_type,
            "project_status": project.project_status,
            "open_action_items": not_closed_status_queryset(action_items).count(),
            "late_action_items": not_closed_status_queryset(
                action_items.filter(target_date__lt=timezone.localdate())
            ).count(),
            "closed_action_items": action_items.filter(status__iexact="closed").count(),
        })
    return response_payload("chart", rows=rows, summary={"count": projects.count()}, chart={"type": "bar", "labels": [row["project_name"] for row in rows], "values": [row["open_action_items"] for row in rows]}, has_more=projects.count() > limit)


def project_email_context(user, payload):
    project_id = ensure_project_visible(user, payload)
    if not project_id:
        return response_payload(
            "clarification",
            summary={"ready": False},
            warnings=["Project ID is required to prepare an email for project members."],
        )
    project = visible_projects(user, payload).filter(id=project_id).first()
    if not project:
        raise PermissionDenied("Project not found or not accessible.")

    members = list(
        project.team_members.select_related("team_member")
        .order_by("team_member__first_name", "team_member__last_name", "id")
    )
    milestones = list(project.milestones.order_by("-updated_at", "-created_at", "-id")[:10])
    rows = [
        {
            "id": member.team_member_id,
            "name": member.team_member.full_name,
            "email": member.team_member.email,
            "role": member.role,
        }
        for member in members
    ]
    latest_milestone = milestones[0] if milestones else None
    return response_payload(
        "email_context",
        rows=rows,
        sources=[
            source_ref(
                "project",
                project.id,
                project.project_name,
                f"/project-milestone-management?project_id={project.id}",
                project_id=project.id,
            )
        ],
        summary={
            "count": len(rows),
            "project_id": project.id,
            "project_name": project.project_name,
            "latest_milestone": {
                "id": latest_milestone.id,
                "name": latest_milestone.name,
                "due_date": latest_milestone.due_date.isoformat(),
            } if latest_milestone else None,
        },
    )


def project_team_members(user, payload):
    """Return the same permission-filtered members without email-draft context."""
    result = project_email_context(user, payload)
    result["answer_type"] = "list"
    summary = dict(result.get("summary") or {})
    summary.pop("latest_milestone", None)
    result["summary"] = summary
    return result


def company_team_members(user, payload):
    """Return active internal users visible through the company-user APIs."""
    company_id = resolve_company_profile_id(user, payload)
    if getattr(user, "is_freelancer", False) or not company_id:
        raise PermissionDenied("Company users are available only within your company.")

    queryset = (
        User.objects.filter(
            company_id=company_id,
            is_active=True,
            is_deleted=False,
            is_permanently_deleted=False,
        )
        .select_related("company")
        .order_by("first_name", "last_name", "id")
    )
    search = str((payload.get("filters") or {}).get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(first_name__icontains=search)
            | Q(last_name__icontains=search)
            | Q(email__icontains=search)
            | Q(job_title__icontains=search)
        )

    count = queryset.count()
    limit = normalize_limit(payload.get("limit"))
    members = list(queryset[:limit])
    rows = [
        {
            "id": member.id,
            "name": member.full_name or member.email,
            "email": member.email,
            "job_title": member.job_title,
            "sharemundo_links_id": member.sharemundo_links_id,
            "company_id": member.company_id,
            "company_name": member.company.company_name if member.company else None,
        }
        for member in members
    ]
    return response_payload(
        "list",
        rows=rows,
        summary={
            "count": count,
            "company_id": company_id,
            "company_name": user.company.company_name,
        },
        has_more=count > limit,
    )


def visible_demands(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return None
    ensure_project_visible(user, payload)
    params = request_params(payload)
    queryset = viewset_queryset(SolutionDemandViewSet, user, params)
    if payload.get("project_id"):
        queryset = queryset.filter(project_id=payload["project_id"])
    return queryset


def visible_requirements(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return None
    ensure_project_visible(user, payload)
    params = request_params(payload)
    queryset = viewset_queryset(RequirementViewSet, user, params)
    if payload.get("project_id"):
        queryset = queryset.filter(project_id=payload["project_id"])
    return queryset


def visible_bom_parts(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return None
    ensure_project_visible(user, payload)
    params = request_params(payload)
    queryset = viewset_queryset(BillOfMaterialPartViewSet, user, params)
    if payload.get("project_id"):
        queryset = queryset.filter(project_id=payload["project_id"])
    return queryset


def visible_available_parts(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return None
    try:
        company_profile_id = resolve_company_profile_id(user, payload)
    except PermissionDenied:
        return AvailableProductPart.objects.none()
    if company_profile_id and not getattr(user, "is_freelancer", False) and int(company_profile_id) != getattr(user, "company_id", None):
        return AvailableProductPart.objects.none()
    params = scoped_request_params(user, payload)
    if company_profile_id:
        params["company_profile"] = company_profile_id
    queryset = viewset_queryset(AvailableProductPartViewSet, user, params)
    if company_profile_id:
        queryset = queryset.filter(company_profile_id=company_profile_id)
    return queryset


def demand_row(demand: SolutionDemand):
    return {
        "id": demand.id,
        "sequence": demand.get_sequence(),
        "name": demand.subject,
        "demand_number": demand.demand_number,
        "project_id": demand.project_id,
        "linked_requirements": demand.requirements.count(),
        "url": f"/project-system-requirements/customer-demands?project_id={demand.project_id}&id={demand.id}",
    }


def requirement_row(requirement: Requirement):
    return {
        "id": requirement.id,
        "sequence": requirement.get_sequence(),
        "name": requirement.name,
        "object_number": requirement.object_number,
        "skill": requirement.skill,
        "project_id": requirement.project_id,
        "linked_demands": requirement.solution_demands.count(),
        "linked_bom_parts": requirement.bom_parts.count(),
        "url": f"/project-system-requirements/technical-requirements?project_id={requirement.project_id}&id={requirement.id}",
    }


def part_row(part):
    project_id = getattr(part, "project_id", None)
    if project_id:
        url = f"/project-product-services/bill-of-material?project_id={project_id}&id={part.id}"
    else:
        url = f"/company-product-services/available-products?id={part.id}"
    return {
        "id": part.id,
        "sequence": part.get_sequence(),
        "name": part.name,
        "part_number": part.part_number,
        "project_id": project_id,
        "company_profile_id": getattr(part, "company_profile_id", None),
        "linked_requirements": part.requirements.count() if hasattr(part, "requirements") else None,
        "url": url,
    }


def matrix_gap_analysis(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return response_payload("list", summary={"count": 0}, warnings=[MATRIX_AND_PARTS_UNAVAILABLE_WARNING])

    filters = payload.get("filters") or {}
    gap_type = filters.get("gap_type")
    limit = normalize_limit(payload.get("limit"))
    if gap_type == "requirements_without_parts":
        queryset = visible_requirements(user, payload).filter(bom_parts__isnull=True).distinct().prefetch_related("solution_demands")
        items = list(queryset[:limit])
        rows = [requirement_row(item) for item in items]
        sources = [source_ref("requirement", item.id, item.name, f"/project-system-requirements/technical-requirements?project_id={item.project_id}&id={item.id}", project_id=item.project_id) for item in items]
    else:
        queryset = visible_demands(user, payload).filter(requirements__isnull=True).distinct()
        items = list(queryset[:limit])
        rows = [demand_row(item) for item in items]
        sources = [source_ref("solution_demand", item.id, item.subject, f"/project-system-requirements/customer-demands?project_id={item.project_id}&id={item.id}", project_id=item.project_id) for item in items]
    return response_payload("list", rows=rows, sources=sources, summary={"count": queryset.count()}, has_more=queryset.count() > limit)


def search_matrix_or_parts(user, payload):
    if not MATRIX_AND_PARTS_AVAILABLE:
        return response_payload("list", summary={"count": 0}, warnings=[MATRIX_AND_PARTS_UNAVAILABLE_WARNING])

    filters = payload.get("filters") or {}
    entity = filters.get("entity") or "requirements"
    valid_entities = ["demands", "requirements", "bom_parts", "available_parts"]
    if entity not in valid_entities:
        raise AIToolClarificationRequired(
            field="entity",
            message="Which matrix or parts list should I search?",
            choices=valid_entities,
        )
    search = filters.get("search")
    limit = normalize_limit(payload.get("limit"))

    if entity == "demands":
        queryset = visible_demands(user, payload)
        if search:
            queryset = queryset.filter(Q(subject__icontains=search) | Q(demand_number__icontains=search))
        items = list(queryset[:limit])
        rows = [demand_row(item) for item in items]
        source_type = "solution_demand"
    elif entity == "bom_parts":
        queryset = visible_bom_parts(user, payload)
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(part_number__icontains=search))
        items = list(queryset[:limit])
        rows = [part_row(item) for item in items]
        source_type = "bill_of_material_part"
    elif entity == "available_parts":
        queryset = visible_available_parts(user, payload)
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(part_number__icontains=search))
        items = list(queryset[:limit])
        rows = [part_row(item) for item in items]
        source_type = "available_product_part"
    else:
        queryset = visible_requirements(user, payload)
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(object_number__icontains=search) | Q(skill__icontains=search))
        items = list(queryset[:limit])
        rows = [requirement_row(item) for item in items]
        source_type = "requirement"

    count = queryset.count()
    return response_payload(
        "list",
        rows=rows,
        sources=[source_ref(source_type, row["id"], row["name"], row["url"]) for row in rows],
        summary={"count": count},
        has_more=count > limit,
    )


PROFILE_SEARCH_STOP_WORDS = {
    "a", "all", "an", "and", "are", "area", "availability", "companies", "company",
    "competence", "contact", "contacts", "expert", "experts", "external", "for",
    "equipment", "equipments", "explain", "find", "in", "links", "looking", "match", "matches", "me", "member",
    "need", "new", "of", "open", "other", "particularity", "please", "profile",
    "or", "part", "parts", "profiles", "public", "qualified", "rank", "show", "some", "supplier", "suppliers",
    "team", "the", "these", "this", "to", "type", "user", "users", "who", "with",
    "work",
}


def search_tokens(search: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[\wÀ-ÿ-]+", (search or "").lower())
        if len(token) > 1 and token not in PROFILE_SEARCH_STOP_WORDS
    ]


def rank_profile_rows(items, search: str, tokens: list[str], weighted_fields: dict[str, int]):
    phrase = (search or "").strip().lower()
    ranked = []
    for item in items:
        score = 0
        reasons = []
        for field_name, weight in weighted_fields.items():
            text = str(getattr(item, field_name, None) or "").lower()
            matched_tokens = [token for token in tokens if token in text]
            if matched_tokens:
                score += weight * len(set(matched_tokens))
                reasons.append(field_name)
            if phrase and phrase in text:
                score += weight * 2
        if score > 0:
            ranked.append((item, score, reasons))
    return sorted(ranked, key=lambda row: (-row[1], str(row[0])))


def public_user_profile_search(user, payload):
    filters = payload.get("filters") or {}
    search = " ".join(
        str(filters.get(name) or "").strip()
        for name in (
            "search", "competence", "position", "qualification", "keywords", "availability"
        )
        if filters.get(name)
    ).strip()
    queryset = User.objects.select_related("company").filter(user_searchable=True, is_active=True, is_deleted=False)
    if getattr(user, "company_id", None):
        queryset = queryset.exclude(company_id=user.company_id)
    if getattr(user, "is_freelancer", False):
        queryset = queryset.exclude(id=user.id)
    location = str(filters.get("location") or "").strip()
    if location:
        queryset = queryset.filter(
            Q(country__icontains=location)
            | Q(city__icontains=location)
            | Q(availability_location__icontains=location)
        )
    availability = str(filters.get("availability") or "").strip()
    if availability:
        queryset = queryset.filter(
            Q(availability_type__icontains=availability)
            | Q(availability_location__icontains=availability)
        )
    availability_from = filters.get("availability_from")
    availability_to = filters.get("availability_to")
    if availability_from:
        queryset = queryset.filter(
            Q(availability_until__isnull=True) | Q(availability_until__gte=availability_from)
        )
    if availability_to:
        queryset = queryset.filter(
            Q(availability_from__isnull=True) | Q(availability_from__lte=availability_to)
        )
    if filters.get("open_to_new_challenges") is not None:
        queryset = queryset.filter(
            open_to_new_challenges=filters["open_to_new_challenges"]
        )
    tokens = search_tokens(search)
    if location:
        location_tokens = set(search_tokens(location))
        tokens = [token for token in tokens if token not in location_tokens]
    has_structured_criteria = any(
        value not in (None, "")
        for value in (location, availability, availability_from, availability_to, filters.get("open_to_new_challenges"))
    )
    if not tokens and not has_structured_criteria:
        queryset = queryset.none()
    elif tokens:
        token_filter = Q()
        for token in tokens:
            token_filter |= (
                Q(first_name__icontains=token)
                | Q(last_name__icontains=token)
                | Q(title__icontains=token)
                | Q(job_title__icontains=token)
                | Q(professional_headline__icontains=token)
                | Q(industry__icontains=token)
                | Q(current_position__icontains=token)
                | Q(qualifications__icontains=token)
                | Q(keywords__icontains=token)
                | Q(resume__icontains=token)
                | Q(availability_location__icontains=token)
                | Q(availability_type__icontains=token)
            )
        queryset = queryset.filter(token_filter).distinct()
    limit = normalize_limit(payload.get("limit"))
    items = list(queryset[:500])
    ranked = (
        rank_profile_rows(
            items,
            search,
            tokens,
            {
                "current_position": 7,
                "job_title": 7,
                "professional_headline": 6,
                "keywords": 5,
                "qualifications": 4,
                "resume": 4,
                "industry": 3,
                "availability_type": 3,
                "availability_location": 2,
                "first_name": 1,
                "last_name": 1,
            },
        )
        if tokens
        else [(item, 1, ["profile_declared_availability"]) for item in items]
    )
    rows = [
        {
            "id": item.id,
            "name": item.full_name,
            "profile_image": item.user_image,
            "professional_headline": item.professional_headline,
            "job_title": item.job_title,
            "industry": item.industry,
            "current_position": item.current_position,
            "qualifications": item.qualifications,
            "keywords": item.keywords,
            "availability_from": item.availability_from.isoformat() if item.availability_from else None,
            "availability_until": item.availability_until.isoformat() if item.availability_until else None,
            "availability_type": item.availability_type,
            "availability_location": item.availability_location,
            "availability_hours_per_week": item.availability_hours_per_week,
            "open_to_new_challenges": item.open_to_new_challenges,
            "spoken_languages": item.spoken_languages,
            "country": item.country,
            "city": item.city,
            "sharemundo_links_id": item.sharemundo_links_id,
            "match_score": score,
            "match_reasons": reasons,
            "company": {
                "id": item.company.id,
                "company_name": item.company.company_name,
                "sharemundo_links_id": item.company.sharemundo_links_id,
                "country": item.company.company_country,
                "town": item.company.company_town,
                "website": item.company.company_web_address,
            } if item.company and item.company.company_searchable else None,
            "url": f"/company-network?public_user_id={item.id}",
        }
        for item, score, reasons in ranked[:limit]
    ]
    count = len(ranked)
    return response_payload(
        "list",
        rows=rows,
        sources=[source_ref("public_user_profile", row["id"], row["name"], row["url"]) for row in rows],
        summary={"count": count},
        has_more=count > limit,
    )


def public_company_profile_search(user, payload):
    filters = payload.get("filters") or {}
    search = " ".join(
        str(filters.get(name) or "").strip()
        for name in ("search", "competence", "keywords", "products", "services")
        if filters.get(name)
    ).strip()
    location = str(filters.get("location") or "").strip()
    queryset = CompanyProfile.objects.filter(company_searchable=True)
    if getattr(user, "company_id", None):
        queryset = queryset.exclude(id=user.company_id)
    if location:
        queryset = queryset.filter(
            Q(company_country__icontains=location)
            | Q(company_town__icontains=location)
        )
    tokens = search_tokens(search)
    if not tokens and not location:
        queryset = queryset.none()
    elif tokens:
        token_filter = Q()
        for token in tokens:
            token_filter |= (
                Q(company_name__icontains=token)
                | Q(products__icontains=token)
                | Q(services__icontains=token)
                | Q(expertise__icontains=token)
            )
        queryset = queryset.filter(token_filter).distinct()
    limit = normalize_limit(payload.get("limit"))
    items = list(queryset[:500])
    ranked = rank_profile_rows(
        items,
        search or location,
        tokens or search_tokens(location),
        {
            "products": 7,
            "services": 6,
            "expertise": 5,
            "company_name": 3,
            "company_country": 1,
            "company_town": 1,
        },
    )
    rows = [
        {
            "id": item.id,
            "company_name": item.company_name,
            "logo": item.company_profile_image,
            "country": item.company_country,
            "town": item.company_town,
            "website": item.company_web_address,
            "products": item.products,
            "services": item.services,
            "expertise": item.expertise,
            "sharemundo_links_id": item.sharemundo_links_id,
            "match_score": score,
            "match_reasons": reasons,
            "url": f"/company-network?public_company_id={item.id}",
        }
        for item, score, reasons in ranked[:limit]
    ]
    count = len(ranked)
    return response_payload(
        "list",
        rows=rows,
        sources=[source_ref("public_company_profile", row["id"], row["company_name"], row["url"]) for row in rows],
        summary={"count": count},
        has_more=count > limit,
    )


def public_profile_search(user, payload):
    limit = normalize_limit(payload.get("limit"))
    child_payload = {**payload, "limit": limit}
    user_result = public_user_profile_search(user, child_payload)
    company_filter_names = {
        "search",
        "competence",
        "keywords",
        "products",
        "services",
        "location",
    }
    company_payload = {
        **child_payload,
        "filters": {
            key: value
            for key, value in (child_payload.get("filters") or {}).items()
            if key in company_filter_names
        },
    }
    company_result = public_company_profile_search(user, company_payload)

    rows = [
        {**row, "profile_type": "user"}
        for row in user_result.get("rows", [])
    ] + [
        {**row, "profile_type": "company"}
        for row in company_result.get("rows", [])
    ]
    rows.sort(
        key=lambda row: (
            -(row.get("match_score") or 0),
            str(row.get("name") or row.get("company_name") or "").lower(),
            row.get("id") or 0,
        )
    )
    rows = rows[:limit]
    sources_by_url = {
        source.get("url"): source
        for source in [
            *(user_result.get("sources") or []),
            *(company_result.get("sources") or []),
        ]
        if source.get("url")
    }
    sources = [sources_by_url[row["url"]] for row in rows if row.get("url") in sources_by_url]
    count = int(user_result.get("summary", {}).get("count") or 0) + int(
        company_result.get("summary", {}).get("count") or 0
    )
    return response_payload(
        "list",
        rows=rows,
        sources=sources,
        summary={
            "count": count,
            "user_count": int(user_result.get("summary", {}).get("count") or 0),
            "company_count": int(company_result.get("summary", {}).get("count") or 0),
        },
        has_more=count > len(rows),
    )


def search_network(user, payload):
    if not getattr(user, "company_id", None) and not getattr(user, "is_freelancer", False):
        return response_payload("list", rows=[], summary={"count": 0})
    filters = payload.get("filters") or {}
    links_id = user.sharemundo_links_id if user.is_freelancer else user.company.sharemundo_links_id
    queryset = Connection.objects.filter(Q(initiator_id=links_id) | Q(target_id=links_id), status="accepted")
    requested_type = str(filters.get("entity_type") or "").strip().lower()
    requested_connection_types = (
        {"user", "freelancer"}
        if requested_type in {"user", "freelancer"}
        else ({requested_type} if requested_type else set())
    )

    connections = list(queryset)
    other_entities = []
    user_links_ids = set()
    company_links_ids = set()
    for item in connections:
        other_type = item.target_type if item.initiator_id == links_id else item.initiator_type
        other_id = item.target_id if item.initiator_id == links_id else item.initiator_id
        if requested_connection_types and other_type not in requested_connection_types:
            continue
        other_entities.append((item, other_type, other_id))
        if other_type in {"user", "freelancer"}:
            user_links_ids.add(other_id)
        elif other_type == "company":
            company_links_ids.add(other_id)

    users_by_links_id = {
        item.sharemundo_links_id: item
        for item in User.objects.select_related("company").filter(
            sharemundo_links_id__in=user_links_ids
        )
    }
    companies_by_links_id = {
        item.sharemundo_links_id: item
        for item in CompanyProfile.objects.filter(
            sharemundo_links_id__in=company_links_ids
        )
    }

    search = str(filters.get("search") or "").strip()
    phrase = search.casefold()
    tokens = search_tokens(search)
    candidates = []
    for item, other_type, other_id in other_entities:
        entity_type = other_type
        label = other_id
        url = None
        searchable_fields = {"sharemundo_links_id": str(other_id or "")}
        row_details = {}

        if other_type in {"user", "freelancer"}:
            entity_type = "user"
            entity = users_by_links_id.get(other_id)
            if entity:
                label = entity.full_name
                url = f"/contact-management?user_id={entity.id}"
                searchable_fields.update(
                    {
                        "name": entity.full_name,
                        "title": entity.title,
                        "job_title": entity.job_title,
                        "professional_headline": entity.professional_headline,
                        "current_position": entity.current_position,
                        "qualifications": entity.qualifications,
                        "keywords": entity.keywords,
                        "resume": entity.resume,
                    }
                )
                row_details = {
                    "job_title": entity.job_title,
                    "professional_headline": entity.professional_headline,
                    "current_position": entity.current_position,
                    "qualifications": entity.qualifications,
                    "keywords": entity.keywords,
                }
        elif other_type == "company":
            entity = companies_by_links_id.get(other_id)
            if entity:
                label = entity.company_name
                url = f"/contact-management?company_id={entity.id}"
                searchable_fields.update(
                    {
                        "company_name": entity.company_name,
                        "products": entity.products,
                        "services": entity.services,
                        "expertise": entity.expertise,
                    }
                )
                row_details = {
                    "products": entity.products,
                    "services": entity.services,
                    "expertise": entity.expertise,
                }

        match_score = 0
        match_reasons = []
        for field_name, raw_value in searchable_fields.items():
            field_value = str(raw_value or "").casefold()
            matched_tokens = {token for token in tokens if token in field_value}
            if matched_tokens:
                match_score += len(matched_tokens)
                match_reasons.append(field_name)
            if phrase and phrase in field_value:
                match_score += 2
        if search and match_score == 0:
            continue
        candidates.append(
            {
                "id": item.id,
                "entity_type": entity_type,
                "name": label,
                "sharemundo_links_id": other_id,
                "status": item.status,
                "url": url,
                "match_score": match_score,
                "match_reasons": match_reasons,
                **row_details,
            }
        )
    candidates.sort(
        key=lambda row: (
            -(row.get("match_score") or 0),
            str(row.get("name") or "").casefold(),
            row.get("id") or 0,
        )
    )
    limit = normalize_limit(payload.get("limit"))
    rows = candidates[:limit]
    sources = [
        source_ref("network_connection", row["id"], row["name"], row["url"])
        for row in rows
        if row.get("url")
    ]
    return response_payload(
        "list",
        rows=rows,
        sources=sources,
        summary={"count": len(candidates)},
        has_more=len(candidates) > len(rows),
    )
