from rest_framework import serializers

from models.models import User
from project_management.models import ActionItem, Milestone


class ActionItemListUserSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="full_name", read_only=True)

    class Meta:
        model = User
        fields = ("id", "name")


class ActionItemListMilestoneSerializer(serializers.ModelSerializer):
    project = serializers.SerializerMethodField()

    class Meta:
        model = Milestone
        fields = (
            "id",
            "name",
            "colour",
            "sort_order",
            "start_date",
            "due_date",
            "project",
        )

    def get_project(self, milestone):
        return {
            "id": milestone.project_id,
            "name": milestone.project.project_name,
        }


class ActionItemListChildSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    closed_by = ActionItemListUserSerializer(read_only=True)
    opportunities = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    num_documents = serializers.IntegerField(read_only=True)
    num_opportunities = serializers.IntegerField(read_only=True)
    num_labels = serializers.IntegerField(read_only=True)
    assignees_count = serializers.IntegerField(read_only=True)
    booked_hour = serializers.FloatField(read_only=True)
    booked_cost = serializers.FloatField(read_only=True)
    # PROSEVIEW MOD-02b: BPU of the mother (inherited by its sub-actions) and "<mother id>-<rank>".
    # The UI shows BPU on the MOTHER only; legacy per-line flags (MOD-02) are not read here, so the list
    # stays free of any extra query.
    effective_bpu = serializers.SerializerMethodField()
    sub_reference = serializers.CharField(read_only=True, allow_null=True)

    # List columns "External ref. / LINKS ref. / Received on / Expected end": data of the customer request
    # the MOTHER action was created from (InterventionRequest). Nothing is derived or invented: null for
    # sub-actions and for independent actions. `expected_end_date` is the CUSTOMER deadline; the planned
    # end of the action is its own `forecast_date`, the two are never merged.
    external_reference = serializers.SerializerMethodField()
    links_reference = serializers.SerializerMethodField()
    received_date = serializers.SerializerMethodField()
    expected_end_date = serializers.SerializerMethodField()
    imported_on = serializers.SerializerMethodField()
    # "Margin (days)" column: customer deadline of the intervention (InterventionRequest.expected_end_date) minus the
    # contractor's planned end (the action's OWN forecast_date), in calendar days (date arithmetic, no time zone).
    # `target_date` (the action's end date, initialised from the deadline at import) is NOT the forecast and is never
    # used. A missing forecast gives no margin: it is never replaced by the deadline. The margin belongs to the
    # INTERVENTION (its mother, or an older flat action): sub-actions carry none (null). `client_deadline` is exposed
    # on the intervention row for the tooltip.
    client_deadline = serializers.SerializerMethodField()
    margin_days = serializers.SerializerMethodField()
    # "Progress" column: same calculation as the request list (MOD-01), null when no calculation exists.
    progress_percent = serializers.SerializerMethodField()
    progress_state = serializers.SerializerMethodField()

    def get_effective_bpu(self, obj):
        parent = obj.parent_action_item if obj.parent_action_item_id else None
        # BPU belongs to interventions only: never shown for a classic action (stored data untouched)
        in_intervention = bool(
            obj.intervention_request_id or (parent is not None and parent.intervention_request_id)
        )
        return bool(in_intervention and (obj.is_bpu or (parent is not None and parent.is_bpu)))

    @staticmethod
    def _request_of_mother(obj):
        if obj.parent_action_item_id or not obj.intervention_request_id:
            return None
        return obj.intervention_request

    def get_external_reference(self, obj):
        request = self._request_of_mother(obj)
        return (request.external_reference or None) if request else None

    def get_links_reference(self, obj):
        request = self._request_of_mother(obj)
        return request.reference if request else None

    def get_received_date(self, obj):
        request = self._request_of_mother(obj)
        return request.received_date if request else None

    def get_expected_end_date(self, obj):
        request = self._request_of_mother(obj)
        return request.expected_end_date if request else None

    @staticmethod
    def _deadline_request(obj):
        """Intervention request of an INTERVENTION row (mother or older flat action); None for a sub-action and
        for a classic action: the margin is an intervention-level figure."""
        if obj.parent_action_item_id or not obj.intervention_request_id:
            return None
        return obj.intervention_request

    def get_client_deadline(self, obj):
        request = self._deadline_request(obj)
        return request.expected_end_date if request else None

    def get_margin_days(self, obj):
        request = self._deadline_request(obj)
        if request is None or request.expected_end_date is None or obj.forecast_date is None:
            return None
        return (request.expected_end_date - obj.forecast_date).days

    def get_imported_on(self, obj):
        """Calendar date (application time zone) of the first successful import of the request; None when
        unknown (older data) and for sub-actions. Same rule and zone as the "Import date" filter."""
        from project_management.services.intervention_import_date import import_date_of

        found = import_date_of(self._request_of_mother(obj))
        return found.isoformat() if found else None

    def _progress(self, obj):
        cached = getattr(obj, "_list_progress_cache", None)
        if cached is not None:
            return cached
        from project_management.services.action_progress import (
            CLOSED, IN_PROGRESS, compute_progress, status_state,
        )

        percent = None
        if obj.parent_action_item_id:
            # sub-action: its own contribution to the mother (points copied from the template)
            # only when the list projection already loaded it: the serializer never queries by itself
            loaded = "progress_setting" in obj._state.fields_cache
            setting = obj._state.fields_cache.get("progress_setting") if loaded else None
            state = status_state(obj.status)
            if setting is not None:
                percent = (
                    setting.closed_points if state == CLOSED
                    else setting.in_progress_points if state == IN_PROGRESS else 0
                )
            state_name = state
        else:
            # all the sub-actions (loaded once for the page), never only the visible ones; without that
            # projection the serializer stays query-free and shows no progress
            subs = getattr(obj, "progress_children", None)
            percent = compute_progress(obj, subs=subs)["progress"] if subs is not None else None
            if percent is None and subs == [] and obj.intervention_request_id:
                # older request with FLAT actions (no mother, no sub-action): its action follows its own
                # status with the same 0 / 10 / 100 mapping as a mother without sub-action (no data change)
                from project_management.services.action_progress import MOTHER_ONLY_POINTS

                percent = MOTHER_ONLY_POINTS.get(status_state(obj.status), 0)
            state_name = CLOSED if status_state(obj.status) == CLOSED else (
                IN_PROGRESS if status_state(obj.status) == IN_PROGRESS or (percent or 0) > 0 else status_state(obj.status)
            )
        obj._list_progress_cache = (percent, state_name)
        return obj._list_progress_cache

    def get_progress_percent(self, obj):
        return self._progress(obj)[0]

    def get_progress_state(self, obj):
        return self._progress(obj)[1]

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "opportunities",
            "created_by",
            "closed_by",
            "num_documents",
            "num_opportunities",
            "num_labels",
            "assignees_count",
            "booked_hour",
            "booked_cost",
            "is_bpu",
            "effective_bpu",
            "sub_reference",
            "external_reference",
            "links_reference",
            "received_date",
            "expected_end_date",
            "imported_on",
            "client_deadline",
            "margin_days",
            "progress_percent",
            "progress_state",
        )

    def get_milestone(self, obj):
        milestone = obj.milestone
        if milestone is None and obj.parent_action_item is not None:
            milestone = obj.parent_action_item.milestone
        if milestone is None:
            return None
        return ActionItemListMilestoneSerializer(milestone).data


class ActionItemListSerializer(ActionItemListChildSerializer):
    milestone = ActionItemListMilestoneSerializer(read_only=True)
    sub_action_items = serializers.SerializerMethodField()
    num_sub_action_items = serializers.IntegerField(read_only=True)

    class Meta(ActionItemListChildSerializer.Meta):
        fields = ActionItemListChildSerializer.Meta.fields + (
            "sub_action_items",
            "num_sub_action_items",
        )

    def get_sub_action_items(self, obj):
        children = getattr(obj, "prefetched_list_children", None)
        if children is None:
            children = obj.sub_action_items.all()
            if self.context.get("limit_sub_action_items_to_visible"):
                children = children.filter(
                    id__in=self.context.get("visible_action_item_ids", set())
                )
        return ActionItemListChildSerializer(
            children,
            many=True,
            context=self.context,
        ).data


class ActionItemBoardDocumentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    status = serializers.CharField(allow_blank=True, allow_null=True)


class ActionItemSubActionSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    closed_by = ActionItemListUserSerializer(read_only=True)
    documents = ActionItemBoardDocumentSerializer(many=True, read_only=True)
    # MOD-03: "<mother id>-<rank>" of a numbered sub-action (None for older sub-actions: never renumbered).
    sub_reference = serializers.CharField(read_only=True, allow_null=True)

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "sub_reference",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "created_by",
            "closed_by",
            "documents",
        )

    def get_milestone(self, obj):
        parent = obj.parent_action_item
        milestone = obj.milestone or (parent.milestone if parent else None)
        return (
            ActionItemListMilestoneSerializer(milestone).data
            if milestone is not None
            else None
        )


class ActionItemBoardSerializer(serializers.ModelSerializer):
    milestone = serializers.SerializerMethodField()
    assignees = ActionItemListUserSerializer(many=True, read_only=True)
    created_by = ActionItemListUserSerializer(read_only=True)
    documents = ActionItemBoardDocumentSerializer(many=True, read_only=True)
    num_documents = serializers.SerializerMethodField()
    is_sub_action_item = serializers.SerializerMethodField()
    parent_action_item_id = serializers.IntegerField(
        read_only=True, allow_null=True
    )
    parent_action_item_name = serializers.CharField(
        source="parent_action_item.name", read_only=True, allow_null=True
    )
    parent_action_item_status = serializers.CharField(
        source="parent_action_item.status", read_only=True, allow_null=True
    )
    num_sub_action_items = serializers.IntegerField(read_only=True)
    # PROSEVIEW MOD-02b: BPU of the mother (inherited by its sub-actions) and "<mother id>-<rank>"
    # reference of numbered sub-actions. Legacy per-line BPU flags are not read here (no query per row).
    is_bpu = serializers.BooleanField(read_only=True)
    effective_bpu = serializers.SerializerMethodField()
    sub_reference = serializers.CharField(read_only=True, allow_null=True)
    # CRM-1B: Network records the follow-up is linked to (navigation from "Mes actions").
    crm_links = serializers.SerializerMethodField()

    class Meta:
        model = ActionItem
        fields = (
            "id",
            "name",
            "notes",
            "allocated_time",
            "start_date",
            "target_date",
            "forecast_date",
            "reminder",
            "status",
            "allocated_budget",
            "priority_level",
            "labels",
            "closed_at",
            "created_at",
            "updated_at",
            "milestone",
            "parent_action_item",
            "assignees",
            "created_by",
            "crm_links",
            "documents",
            "num_documents",
            "is_sub_action_item",
            "is_bpu",
            "effective_bpu",
            "sub_reference",
            "parent_action_item_id",
            "parent_action_item_name",
            "parent_action_item_status",
            "num_sub_action_items",
        )

    def get_milestone(self, obj):
        milestone = obj.milestone
        if milestone is None and obj.parent_action_item is not None:
            milestone = obj.parent_action_item.milestone
        return (
            ActionItemListMilestoneSerializer(milestone).data
            if milestone is not None
            else None
        )

    def get_num_documents(self, obj):
        return len(obj.documents.all())

    def get_is_sub_action_item(self, obj):
        return obj.parent_action_item_id is not None

    def get_effective_bpu(self, obj):
        parent = obj.parent_action_item if obj.parent_action_item_id else None
        # BPU belongs to interventions only: never shown for a classic action (stored data untouched)
        in_intervention = bool(
            obj.intervention_request_id or (parent is not None and parent.intervention_request_id)
        )
        return bool(in_intervention and (obj.is_bpu or (parent is not None and parent.is_bpu)))

    def get_crm_links(self, obj):
        return {
            "contact_users": [
                {"id": c.id, "name": f"{c.name} {c.surname}".strip()}
                for c in obj.contact_users.all()
            ],
            "contact_companies": [
                {"id": c.id, "name": c.company_name} for c in obj.contact_companies.all()
            ],
            "opportunities": [
                {"id": o.id, "title": o.title, "reference": o.reference}
                for o in obj.opportunities.all()
            ],
        }


class ActionItemCalendarSerializer(serializers.BaseSerializer):
    def to_representation(self, obj):
        parent = obj.parent_action_item
        milestone = obj.milestone or (parent.milestone if parent else None)
        return {
            "id": obj.id,
            "name": obj.name,
            "start_date": obj.start_date,
            "target_date": obj.target_date,
            "status": obj.status,
            "colour": milestone.colour if milestone else "#0057B7",
            "is_sub_action_item": parent is not None,
            "parent_action_item_id": parent.id if parent else None,
            "parent_action_item_name": parent.name if parent else None,
            "parent_action_item_status": parent.status if parent else None,
        }
