from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import (
    Count,
    Exists,
    F,
    FloatField,
    Func,
    IntegerField,
    OrderBy,
    OuterRef,
    Prefetch,
    Q,
    QuerySet,
    Subquery,
    Sum,
    TextField,
    Value,
)
from django.db.models.functions import Coalesce, Lower, NullIf, Trim
from django.utils.dateparse import parse_date
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from ecpmpapp.constants import StatusType
from ecpmpapp.ordering import NaturalSortKey
from companyprofileapis.models import Opportunity
from models.models import CompanyConfig, User
from project_management.models import (
    ActionItem,
    ExpenseCard,
    Milestone,
    Project,
    TimeCard,
)
from project_management.models.constants import COLLABORATIVE_SOLUTION, SPECIAL_SOLUTION
from project_management.utils import get_product_name_from_request


def _assignee_or_creator(user) -> Q:
    return Q(assignees=user) | Q(created_by=user)


@dataclass(frozen=True)
class ActionItemVisibility:
    queryset: QuerySet
    directly_visible_ids: frozenset[int]
    limit_sub_action_items_to_visible: bool


class ActionItemVisibilityService:
    """Resolve the permission-visible Action Item scope for one request."""

    def __init__(self, request, *, permission_checker):
        self.request = request
        self.user = request.user
        self.params = request.query_params
        self.permission_checker = permission_checker

    def resolve(self) -> ActionItemVisibility:
        permissions = self._permissions(include_assigned_created=True)
        filters = self._visibility_filter(permissions)
        directly_visible_ids: set[int] = set()
        limit_children = False

        has_explicit_scope = any(
            self.params.get(key)
            for key in ("project", "company_profile", "freelancer")
        )
        if permissions["assigned_created"] and (
            not permissions["broad_scope"] or not has_explicit_scope
        ):
            directly_visible_ids = self.directly_visible_ids()
            visible_parent_ids = ActionItem.objects.filter(
                id__in=directly_visible_ids,
                parent_action_item__isnull=False,
            ).values_list("parent_action_item_id", flat=True)
            filters |= Q(id__in=directly_visible_ids) | Q(id__in=visible_parent_ids)
            limit_children = not permissions["broad_scope"]

        queryset = (
            ActionItem.objects.filter(filters).distinct()
            if filters
            else ActionItem.objects.none()
        )
        scope_filter = self._request_scope_filter()
        if scope_filter is not None:
            queryset = queryset.filter(scope_filter)
        if self._truthy(self.params.get("related_to_me")):
            queryset = queryset.filter(_assignee_or_creator(self.user)).distinct()

        return ActionItemVisibility(
            queryset=queryset,
            directly_visible_ids=frozenset(directly_visible_ids),
            limit_sub_action_items_to_visible=limit_children,
        )

    def directly_visible_ids(self) -> set[int]:
        return set(
            self.directly_visible_queryset().values_list("id", flat=True)
        )

    def directly_visible_queryset(
        self,
        queryset: QuerySet | None = None,
    ) -> QuerySet:
        queryset = queryset if queryset is not None else ActionItem.objects.all()
        queryset = queryset.filter(_assignee_or_creator(self.user))
        scope_filter = self._request_scope_filter()
        if scope_filter is not None:
            queryset = queryset.filter(scope_filter)
        return queryset.distinct()

    def should_limit_sub_action_items(self) -> bool:
        return not self._permissions(include_assigned_created=False)["broad_scope"]

    def _permissions(self, *, include_assigned_created: bool) -> dict[str, bool]:
        request_scope = "project" if self.params.get("project") else "company"
        action_item_company = self.permission_checker(
            self.user,
            {"action_item": ["view", "full_access"]},
            "company",
            self.params,
        )
        projects_company = self.permission_checker(
            self.user,
            {"projects": ["full_access", "view_all_access"]},
            "company",
            self.params,
        )
        action_item_project = self.permission_checker(
            self.user,
            {"action_item": ["view", "full_access"]},
            "project",
            self.params,
        )
        assigned_created = False
        if include_assigned_created:
            assigned_created = self.permission_checker(
                self.user,
                {"action_item": ["view_assigned_created"]},
                request_scope,
                self.params,
            )
        broad_scope = (
            request_scope == "company" and action_item_company
        ) or (
            request_scope == "project"
            and (action_item_project or projects_company)
        )
        return {
            "action_item_company": action_item_company,
            "projects_company": projects_company,
            "action_item_project": action_item_project,
            "assigned_created": assigned_created,
            "broad_scope": broad_scope,
        }

    def _visibility_filter(self, permissions: dict[str, bool]) -> Q:
        products = self.allowed_products()
        filters = Q()

        if not self.user.is_freelancer and permissions["action_item_company"]:
            filters |= Q(company_profile=self.user.company) | Q(
                parent_action_item__company_profile=self.user.company
            )

        if permissions["action_item_project"]:
            user_projects = Project.objects.filter(
                team_members__team_member=self.user,
                solution__in=products,
            )
            filters |= Q(milestone__project__in=user_projects) | Q(
                parent_action_item__milestone__project__in=user_projects
            )

        if not self.user.is_freelancer and permissions["projects_company"]:
            filters |= (
                Q(milestone__project__solution__in=products)
                & Q(milestone__project__user__company=self.user.company)
            ) | (
                Q(parent_action_item__milestone__project__solution__in=products)
                & Q(
                    parent_action_item__milestone__project__user__company=(
                        self.user.company
                    )
                )
            )

        if self.user.is_freelancer:
            filters |= Q(freelancer=self.user) | Q(
                parent_action_item__freelancer=self.user
            )
            if permissions["action_item_project"]:
                freelancer_projects = Project.objects.filter(
                    team_members__team_member=self.user,
                    solution__in=products,
                )
                filters |= Q(milestone__project__in=freelancer_projects) | Q(
                    parent_action_item__milestone__project__in=freelancer_projects
                )
        return filters

    def _request_scope_filter(self) -> Q | None:
        project_id = self.params.get("project")
        company_profile_id = self.params.get("company_profile")
        freelancer_id = self.params.get("freelancer")
        if project_id:
            return Q(milestone__project=project_id) | Q(
                parent_action_item__milestone__project=project_id
            )
        if company_profile_id:
            return Q(company_profile=company_profile_id) | Q(
                parent_action_item__company_profile=company_profile_id
            )
        if freelancer_id:
            return Q(freelancer=freelancer_id) | Q(
                parent_action_item__freelancer=freelancer_id
            )
        return None

    def allowed_products(self) -> list[str]:
        product_name = get_product_name_from_request(self.request)
        combined_solutions = COLLABORATIVE_SOLUTION + SPECIAL_SOLUTION
        if product_name in combined_solutions:
            return combined_solutions
        return [product_name]

    @staticmethod
    def _truthy(value) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}


class ActionItemFilterService:
    """Build Action Item predicates for hierarchical and flat read models."""

    def __init__(self, params: Mapping):
        self.params = params
        self.search = str(params.get("search") or "").strip()

    def filter_tree(self, parents: QuerySet) -> QuerySet:
        parent_predicate = self._predicate(is_child=False)
        child_predicate = self._predicate(is_child=True)
        if not parent_predicate.children and not child_predicate.children:
            return parents

        matching_children = ActionItem.objects.filter(
            parent_action_item_id=OuterRef("pk")
        ).filter(child_predicate)
        return (
            parents.annotate(
                _action_item_has_matching_child=Exists(matching_children)
            )
            .filter(parent_predicate | Q(_action_item_has_matching_child=True))
            .distinct()
        )

    def filter_children(self, children: QuerySet) -> QuerySet:
        return children.filter(self._predicate(is_child=True)).distinct()

    def filter_flat(self, action_items: QuerySet) -> QuerySet:
        return action_items.filter(self._predicate(is_child=None)).distinct()

    def filter_time_bucket(self, action_items: QuerySet) -> QuerySet:
        bucket = str(self.params.get("bucket") or "").strip().lower()
        bucket = {"thisweek": "this_week", "nextweek": "next_week"}.get(
            bucket, bucket
        )
        # CRM-1B ("Mes actions"): "upcoming" = after this week, "done" = closed (closed_at set).
        if bucket not in {"late", "today", "this_week", "next_week", "later", "upcoming", "done"}:
            raise ValidationError({"bucket": "Unknown time bucket."})
        if bucket == "done":
            return action_items.filter(closed_at__isnull=False)
        if self._truthy_param("open_only"):
            action_items = action_items.filter(closed_at__isnull=True)

        today = timezone.localdate()
        this_week_end = today + timedelta(days=6 - today.weekday())
        next_week_start = this_week_end + timedelta(days=1)
        next_week_end = next_week_start + timedelta(days=6)
        if bucket == "late":
            return action_items.filter(target_date__lt=today)
        if bucket == "today":
            return action_items.filter(target_date=today)
        if bucket == "this_week":
            return action_items.filter(
                target_date__gt=today,
                target_date__lte=this_week_end,
            )
        if bucket == "next_week":
            return action_items.filter(
                target_date__gte=next_week_start,
                target_date__lte=next_week_end,
            )
        if bucket == "upcoming":
            return action_items.filter(target_date__gt=this_week_end)
        return action_items.filter(target_date__gt=next_week_end)

    def _truthy_param(self, name: str) -> bool:
        return str(self.params.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}

    def filter_calendar_range(self, action_items: QuerySet) -> QuerySet:
        range_start = self._date("range_start")
        range_end = self._date("range_end")
        if range_start is None or range_end is None:
            raise ValidationError(
                {"range": "range_start and range_end are required."}
            )
        if range_end <= range_start:
            raise ValidationError({"range_end": "Must be after range_start."})
        if (range_end - range_start).days > 370:
            raise ValidationError({"range_end": "Calendar range is too large."})
        return action_items.filter(
            start_date__lt=range_end,
            target_date__gte=range_start,
        )

    def _predicate(self, *, is_child: bool | None) -> Q:
        predicate = Q()
        if self.search:
            predicate &= (
                Q(id__icontains=self.search)
                | Q(name__icontains=self.search)
                | Q(status__icontains=self.search)
            )
        predicate &= self._status_predicate()
        predicate &= self._date_predicate()
        predicate &= self._relation_predicate(is_child=is_child)
        return predicate

    def _date_predicate(self) -> Q:
        predicate = Q()
        if self._truthy("dashboard_window"):
            today = timezone.localdate()
            window_end = today + timedelta(days=7)
            predicate &= (
                Q(target_date__lt=today)
                | Q(start_date__range=(today, window_end))
                | Q(target_date__range=(today, window_end))
            )
        start_date = self._date("start_date")
        target_date = self._date("target_date")
        target_date_after = self._date("target_date_after")
        target_date_before = self._date("target_date_before")
        date_filter_mode = str(self.params.get("date_filter_mode") or "date")
        if date_filter_mode not in {"date", "period"}:
            raise ValidationError(
                {"date_filter_mode": "Expected 'date' or 'period'."}
            )
        if date_filter_mode == "period":
            if start_date:
                predicate &= Q(start_date__gte=start_date)
            if target_date:
                predicate &= Q(target_date__lte=target_date)
        else:
            if start_date:
                predicate &= Q(start_date=start_date)
            if target_date:
                predicate &= Q(target_date=target_date)

        if target_date_after:
            predicate &= Q(target_date__gte=target_date_after)
        if target_date_before:
            predicate &= Q(target_date__lte=target_date_before)

        forecast_from = self._date_any(
            "forecast_date_after",
            "forecast_date_from",
        )
        forecast_to = self._date_any(
            "forecast_date_before",
            "forecast_date_to",
        )
        if forecast_from and forecast_to:
            predicate &= Q(forecast_date__range=(forecast_from, forecast_to))
        elif forecast_from or forecast_to:
            predicate &= Q(forecast_date=forecast_from or forecast_to)
        return predicate

    def _relation_predicate(self, *, is_child: bool | None) -> Q:
        predicate = Q()
        for param, lookup in (
            ("reporter", "created_by_id"),
            ("assignee", "assignees__id"),
            ("opportunity", "opportunities__id"),
            ("contact_user", "contact_users__id"),
            ("contact_company", "contact_companies__id"),
        ):
            values = self._integer_values(param)
            if values:
                predicate &= Q(**{f"{lookup}__in": values})

        # CRM-1B: "type" of follow-up = which Network record it is linked to.
        crm_link = str(self.params.get("crm_link") or "").strip().lower()
        if crm_link == "contact":
            predicate &= Q(contact_users__isnull=False)
        elif crm_link == "company":
            predicate &= Q(contact_companies__isnull=False)
        elif crm_link == "opportunity":
            predicate &= Q(opportunities__isnull=False)
        elif crm_link == "any":
            predicate &= (
                Q(contact_users__isnull=False)
                | Q(contact_companies__isnull=False)
                | Q(opportunities__isnull=False)
            )

        priorities = self._values("priority")
        if priorities:
            predicate &= self._case_insensitive_any("priority_level", priorities)

        labels = self._values("labels") or self._values("label")
        if labels:
            label_predicate = Q()
            for label in labels:
                label_predicate |= Q(labels__contains=[label])
            predicate &= label_predicate

        phase_ids = self._integer_values("phase") or self._integer_values(
            "milestone"
        )
        phase_names = self._values("section")
        if phase_ids or phase_names:
            if is_child is True:
                phase_prefixes = ("parent_action_item__milestone",)
            elif is_child is False:
                phase_prefixes = ("milestone",)
            else:
                phase_prefixes = ("milestone", "parent_action_item__milestone")

            phase_predicate = Q()
            for prefix in phase_prefixes:
                if phase_ids:
                    phase_predicate |= Q(**{f"{prefix}_id__in": phase_ids})
                if phase_names:
                    phase_predicate |= self._case_insensitive_any(
                        f"{prefix}__name",
                        phase_names,
                    )
            predicate &= phase_predicate
        return predicate

    @staticmethod
    def _case_insensitive_any(field: str, values: list[str]) -> Q:
        predicate = Q()
        for value in values:
            predicate |= Q(**{f"{field}__iexact": value})
        return predicate

    def _status_predicate(self) -> Q:
        closed_from = self._date_any("closure_date_after", "closed_at_from")
        closed_to = self._date_any("closure_date_before", "closed_at_to")
        if closed_from or closed_to:
            predicate = Q(status__iexact="closed", closed_at__isnull=False)
            if closed_from and closed_to:
                return predicate & Q(closed_at__range=(closed_from, closed_to))
            return predicate & Q(closed_at=closed_from or closed_to)

        statuses = self._values("status")
        normalized_statuses = {
            self._normalize_status(value) for value in statuses if value
        }
        if normalized_statuses:
            predicate = Q()
            for status in normalized_statuses:
                predicate |= Q(status__iexact=status)
            if self._truthy("show_closed") and "closed" not in normalized_statuses:
                predicate |= Q(status__iexact="closed")
            return predicate

        if self._truthy("show_closed"):
            return Q()
        return ~Q(status__iexact="closed")

    def _values(self, key: str) -> list[str]:
        getlist = getattr(self.params, "getlist", None)
        raw_values = getlist(key) if getlist else [self.params.get(key)]
        values = []
        for raw_value in raw_values:
            if raw_value in (None, ""):
                continue
            if isinstance(raw_value, (list, tuple, set)):
                candidates = raw_value
            else:
                candidates = str(raw_value).split(",")
            values.extend(
                str(candidate).strip()
                for candidate in candidates
                if str(candidate).strip()
            )
        return values

    def _date(self, key: str) -> date | None:
        value = self.params.get(key)
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            parsed = parse_date(str(value))
        except ValueError as error:
            raise ValidationError({key: "Enter a valid ISO date."}) from error
        if parsed is None:
            raise ValidationError({key: "Enter a valid ISO date."})
        return parsed

    def _integer_values(self, key: str) -> list[int]:
        values = self._values(key)
        try:
            return [int(value) for value in values]
        except (TypeError, ValueError) as error:
            raise ValidationError({key: "Expected comma-separated IDs."}) from error

    def _date_any(self, *keys: str) -> date | None:
        for key in keys:
            parsed = self._date(key)
            if parsed is not None:
                return parsed
        return None

    def _truthy(self, key: str) -> bool:
        return str(self.params.get(key) or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _normalize_status(value: str) -> str:
        normalized = value.split(":", 1)[0].strip().lower()
        return "closed" if normalized == "close" else normalized


class ActionItemOrderingService:
    """Apply only supported parent ordering with a stable ID tie-breaker."""

    FIELD_MAP = {
        "id": "id",
        "name": "name",
        "notes": "notes",
        "status": "status",
        "phase": "milestone__name",
        "milestone": "milestone__name",
        "allocated_time": "allocated_time",
        "allocated_budget": "allocated_budget",
        "start_date": "start_date",
        "target_date": "target_date",
        "forecast_date": "forecast_date",
        "closed_at": "closed_at",
        "priority_level": "priority_level",
        "created_at": "created_at",
        "updated_at": "updated_at",
        "num_documents": "num_documents",
        "num_sub_action_items": "num_sub_action_items",
        "labels": "num_labels",
        "documents": "num_documents",
        "opportunities": "num_opportunities",
        "assignee": "assignees_count",
        "assignees_count": "assignees_count",
        "booked_hour": "booked_hour",
        "booked_cost": "booked_cost",
    }
    TEXT_FIELDS = {
        "name",
        "notes",
        "status",
        "phase",
        "milestone",
        "priority_level",
    }

    @classmethod
    def apply(
        cls,
        queryset: QuerySet,
        *,
        scope: str,
        ordering: str | None = None,
    ) -> QuerySet:
        requested_fields = [
            value.strip()
            for value in str(ordering or "").split(",")
            if value.strip()
        ]
        valid_fields = [
            value
            for value in requested_fields
            if value.lstrip("-") in cls.FIELD_MAP
        ]
        if not valid_fields:
            if scope == "project":
                return queryset.order_by(
                    "milestone__sort_order",
                    "milestone_id",
                    "id",
                )
            return queryset.order_by("-id")

        order_by = []
        explicitly_orders_id = False
        for index, requested in enumerate(valid_fields):
            descending = requested.startswith("-")
            public_name = requested.lstrip("-")
            field_name = cls.FIELD_MAP[public_name]
            explicitly_orders_id |= public_name == "id"

            if public_name in {"allocated_time", "allocated_budget"}:
                allocation_alias = f"_action_item_ordering_allocation_{index}"
                allocation_output_field = (
                    FloatField()
                    if public_name == "allocated_budget"
                    else IntegerField()
                )
                queryset = queryset.alias(
                    **{
                        allocation_alias: NullIf(
                            F(field_name),
                            Value(0, output_field=allocation_output_field),
                            output_field=allocation_output_field,
                        )
                    }
                )
                field_name = allocation_alias

            if public_name in cls.TEXT_FIELDS:
                text_alias = f"_action_item_ordering_text_{index}"
                queryset = queryset.alias(
                    **{
                        text_alias: Lower(
                            NullIf(
                                NullIf(
                                    Trim(F(field_name)),
                                    Value(""),
                                    output_field=TextField(),
                                ),
                                Value("-"),
                                output_field=TextField(),
                            )
                        )
                    }
                )
                if public_name == "name":
                    natural_alias = f"_action_item_ordering_natural_{index}"
                    queryset = queryset.alias(
                        **{natural_alias: NaturalSortKey(F(text_alias))}
                    )
                    order_by.append(
                        OrderBy(
                            F(natural_alias),
                            descending=descending,
                            nulls_last=True,
                        )
                    )
                else:
                    order_by.append(
                        OrderBy(
                            F(text_alias),
                            descending=descending,
                            nulls_last=True,
                        )
                    )
            else:
                order_by.append(
                    OrderBy(
                        F(field_name),
                        descending=descending,
                        nulls_last=True,
                    )
                )

        if not explicitly_orders_id:
            order_by.append("id")
        return queryset.order_by(*order_by)


class ActionItemQueryService:
    """Provide explicit projections for Action Item read models."""

    @classmethod
    def with_list_projection(
        cls,
        parents: QuerySet,
        *,
        children: QuerySet | None = None,
    ) -> QuerySet:
        child_queryset = cls._with_list_annotations(
            children if children is not None else ActionItem.objects.all()
        ).select_related(
            "milestone",
            "milestone__project",
            "parent_action_item",
            "parent_action_item__milestone",
            "parent_action_item__milestone__project",
            "created_by",
            "closed_by",
        ).prefetch_related("assignees", "opportunities")

        return (
            cls._with_list_annotations(parents)
            .select_related("milestone", "created_by", "closed_by")
            .select_related("milestone__project")
            .prefetch_related(
                "assignees",
                "opportunities",
                Prefetch(
                    "sub_action_items",
                    queryset=child_queryset.order_by("id"),
                    to_attr="prefetched_list_children",
                ),
            )
        )

    @classmethod
    def with_board_projection(cls, action_items: QuerySet) -> QuerySet:
        child_count = (
            ActionItem.objects.filter(parent_action_item_id=OuterRef("pk"))
            .values("parent_action_item_id")
            .annotate(total=Count("id"))
            .values("total")[:1]
        )
        return (
            action_items.annotate(
                num_sub_action_items=Coalesce(
                    Subquery(child_count, output_field=IntegerField()),
                    Value(0),
                )
            )
            .select_related(
                "milestone",
                "milestone__project",
                "parent_action_item",
                "parent_action_item__milestone",
                "parent_action_item__milestone__project",
                "created_by",
                "closed_by",
            )
            .prefetch_related(
                "assignees",
                "documents",
                "contact_users",
                "contact_companies",
                "opportunities",
            )
        )

    @staticmethod
    def with_calendar_projection(action_items: QuerySet) -> QuerySet:
        return action_items.select_related(
            "milestone",
            "parent_action_item",
            "parent_action_item__milestone",
        )

    @staticmethod
    def _with_list_annotations(queryset: QuerySet) -> QuerySet:
        approved_time_card_hours = (
            TimeCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("hours"))
            .values("total")[:1]
        )
        approved_expense_card_total = (
            ExpenseCard.objects.filter(
                action_item=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("action_item")
            .annotate(total=Sum("total"))
            .values("total")[:1]
        )
        return queryset.annotate(
            num_documents=Count("documents", distinct=True),
            num_sub_action_items=Count("sub_action_items", distinct=True),
            assignees_count=Count("assignees", distinct=True),
            num_opportunities=Count("opportunities", distinct=True),
            num_labels=Coalesce(
                Func(F("labels"), function="CARDINALITY", output_field=IntegerField()),
                Value(0),
            ),
            booked_hour=Coalesce(
                Subquery(approved_time_card_hours, output_field=FloatField()),
                Value(0.0),
                output_field=FloatField(),
            ),
            booked_cost=Coalesce(
                Subquery(approved_expense_card_total, output_field=FloatField()),
                Value(0.0),
                output_field=FloatField(),
            ),
        )


class ActionItemFilterOptionsService:
    """Return stable options from the same permission-visible scope as List."""

    @classmethod
    def build(cls, queryset: QuerySet, request) -> dict:
        parent_queryset = queryset.filter(parent_action_item__isnull=True)
        reporter_ids = parent_queryset.exclude(created_by_id=None).values(
            "created_by_id"
        )
        assignee_ids = queryset.exclude(assignees=None).values("assignees__id")
        opportunity_ids = queryset.exclude(opportunities=None).values(
            "opportunities__id"
        )

        reporters = cls._users(reporter_ids)
        assignees = cls._users(assignee_ids)
        opportunities = list(
            Opportunity.objects.filter(id__in=Subquery(opportunity_ids))
            .order_by(Lower("title"), "id")
            .values("id", "title")
        )

        project_id = request.query_params.get("project")
        phases = []
        configuration = None
        if project_id:
            project = (
                Project.objects.filter(pk=project_id)
                .select_related("configurations")
                .first()
            )
            configuration = getattr(project, "configurations", None) if project else None
            phase_ids = parent_queryset.exclude(milestone_id=None).values(
                "milestone_id"
            )
            phases = list(
                Milestone.objects.filter(id__in=Subquery(phase_ids))
                .order_by("sort_order", "id")
                .values("id", "name", "colour", "sort_order")
            )
        else:
            product_name = get_product_name_from_request(request)
            config_filters = {"solution": product_name}
            if request.user.is_freelancer:
                config_filters["user"] = request.user
            else:
                config_filters["company_profile"] = request.user.company
            configuration = CompanyConfig.objects.filter(**config_filters).first()

        statuses = cls._configured_options(
            getattr(configuration, "status_options", None),
            fallback=parent_queryset.values_list("status", flat=True).distinct(),
        )
        priorities = cls._configured_options(
            getattr(configuration, "priority_levels", None),
            fallback=parent_queryset.exclude(priority_level__in=(None, "")).values_list(
                "priority_level", flat=True
            ).distinct(),
        )
        configured_labels = getattr(configuration, "labels", None) or []
        labels = sorted({str(label) for label in configured_labels if label})
        if not labels:
            for row_labels in parent_queryset.values_list("labels", flat=True):
                labels.extend(str(label) for label in (row_labels or []) if label)
            labels = sorted(set(labels), key=str.casefold)

        return {
            "reporters": reporters,
            "assignees": assignees,
            "statuses": statuses,
            "labels": [{"value": label, "label": label} for label in labels],
            "opportunities": [
                {"id": item["id"], "name": item["title"]}
                for item in opportunities
            ],
            "phases": phases,
            "priorities": priorities,
        }

    @staticmethod
    def _users(ids_queryset) -> list[dict]:
        users = User.objects.filter(id__in=Subquery(ids_queryset)).order_by(
            Lower("first_name"), Lower("last_name"), "id"
        )
        return [{"id": user.id, "name": user.full_name} for user in users]

    @staticmethod
    def _configured_options(values, *, fallback) -> list[dict]:
        raw_values = list(values or fallback)
        options = []
        seen = set()
        for raw_value in raw_values:
            if not raw_value:
                continue
            value, separator, color = str(raw_value).partition(":")
            normalized = value.strip()
            if not normalized or normalized.casefold() in seen:
                continue
            seen.add(normalized.casefold())
            option = {"value": normalized, "label": normalized}
            if separator and color:
                option["color"] = color
            options.append(option)
        return options
