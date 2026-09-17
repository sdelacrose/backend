from django.db.models import (
    BooleanField,
    Case,
    Count,
    Exists,
    FloatField,
    IntegerField,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from ecpmpapp.constants import StatusType
from project_management.models import ActionItem, ExpenseCard, Folder, Milestone, TimeCard


class MilestoneQueryService:
    """Build bounded Phase list/detail projections without Action Item trees."""

    @staticmethod
    def with_read_projection(queryset, *, detail=False):
        direct_actions = ActionItem.objects.filter(
            milestone_id=OuterRef("pk"),
            parent_action_item__isnull=True,
        )
        child_actions = ActionItem.objects.filter(
            parent_action_item__milestone_id=OuterRef("pk"),
        )
        approved_time = (
            TimeCard.objects.filter(
                milestone_id=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("milestone_id")
            .annotate(total=Sum("hours"))
            .values("total")[:1]
        )
        approved_cost = (
            ExpenseCard.objects.filter(
                milestone_id=OuterRef("pk"),
                status=StatusType.APPROVED.value,
            )
            .values("milestone_id")
            .annotate(total=Sum("total"))
            .values("total")[:1]
        )
        direct_count = (
            direct_actions.values("milestone_id")
            .annotate(total=Count("id"))
            .values("total")[:1]
        )
        child_count = (
            child_actions.values("parent_action_item__milestone_id")
            .annotate(total=Count("id"))
            .values("total")[:1]
        )
        open_direct = direct_actions.exclude(status="closed")
        open_children = child_actions.exclude(status="closed")

        queryset = queryset.select_related(
            "project",
            "project__configurations",
            "closed_by",
        ).annotate(
            booked_hour=Coalesce(
                Subquery(approved_time, output_field=FloatField()),
                Value(0.0),
            ),
            booked_cost=Coalesce(
                Subquery(approved_cost, output_field=FloatField()),
                Value(0.0),
            ),
            num_documents=Count("documents", distinct=True),
            num_action_items=Coalesce(
                Subquery(direct_count, output_field=IntegerField()),
                Value(0),
            ),
            num_sub_action_items=Coalesce(
                Subquery(child_count, output_field=IntegerField()),
                Value(0),
            ),
            has_open_action_items=Exists(open_direct) | Exists(open_children),
            has_any_action_items=Exists(direct_actions) | Exists(child_actions),
        ).annotate(
            all_closed=Case(
                When(
                    Q(has_any_action_items=True) & Q(has_open_action_items=False),
                    then=Value(True),
                ),
                default=Value(False),
                output_field=BooleanField(),
            )
        )

        document_queryset = Milestone.documents.rel.model.objects.select_related(
            "author"
        )
        queryset = queryset.prefetch_related(
            Prefetch("documents", queryset=document_queryset, to_attr="phase_documents")
        )
        if detail:
            queryset = queryset.prefetch_related(
                Prefetch(
                    "linked_folders",
                    queryset=Folder.objects.select_related(
                        "parent", "project", "company_profile", "freelancer"
                    ),
                    to_attr="phase_linked_folders",
                )
            )
        return queryset
