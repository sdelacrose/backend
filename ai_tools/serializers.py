from rest_framework import serializers


class CurrentUserOrPositiveIntegerField(serializers.Field):
    default_error_messages = {
        "invalid": "Use current_user or a positive user ID.",
    }

    def to_internal_value(self, data):
        if isinstance(data, str) and data.strip().lower() in {"current_user", "me", "myself"}:
            return "current_user"
        try:
            value = int(data)
        except (TypeError, ValueError):
            self.fail("invalid")
        if value < 1:
            self.fail("invalid")
        return value

    def to_representation(self, value):
        return value


FILTER_FIELD_FACTORIES = {
    "search": lambda: serializers.CharField(allow_blank=False),
    "status": lambda: serializers.CharField(allow_blank=False),
    "excluded_statuses": lambda: serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=255),
        allow_empty=False,
        max_length=50,
    ),
    "priority": lambda: serializers.CharField(allow_blank=False),
    "assignee": CurrentUserOrPositiveIntegerField,
    "created_by": CurrentUserOrPositiveIntegerField,
    "author": CurrentUserOrPositiveIntegerField,
    "document_type": lambda: serializers.CharField(allow_blank=False),
    "po_type": lambda: serializers.CharField(allow_blank=False),
    "reference": lambda: serializers.CharField(allow_blank=False),
    "entity": lambda: serializers.CharField(allow_blank=False),
    "entity_type": lambda: serializers.CharField(allow_blank=False),
    "competence": lambda: serializers.CharField(allow_blank=False),
    "position": lambda: serializers.CharField(allow_blank=False),
    "qualification": lambda: serializers.CharField(allow_blank=False),
    "keywords": lambda: serializers.CharField(allow_blank=False),
    "products": lambda: serializers.CharField(allow_blank=False),
    "services": lambda: serializers.CharField(allow_blank=False),
    "location": lambda: serializers.CharField(allow_blank=False),
    "availability": lambda: serializers.CharField(allow_blank=False),
    "availability_from": lambda: serializers.DateField(),
    "availability_to": lambda: serializers.DateField(),
    "open_to_new_challenges": lambda: serializers.BooleanField(),
    "gap_type": lambda: serializers.CharField(allow_blank=False),
    "ordering": lambda: serializers.CharField(allow_blank=False),
    "content_query": lambda: serializers.CharField(allow_blank=False),
    "title": lambda: serializers.CharField(allow_blank=False),
    "name": lambda: serializers.CharField(allow_blank=False),
    "description": lambda: serializers.CharField(required=False, allow_blank=True),
    "confirmation_token": lambda: serializers.CharField(allow_blank=False),
    "solution": lambda: serializers.CharField(allow_blank=False),
    "timezone": lambda: serializers.CharField(allow_blank=False),
    "target_date_from": lambda: serializers.DateField(),
    "target_date_to": lambda: serializers.DateField(),
    "forecast_date_from": lambda: serializers.DateField(),
    "forecast_date_to": lambda: serializers.DateField(),
    "closed_at_from": lambda: serializers.DateField(),
    "closed_at_to": lambda: serializers.DateField(),
    "po_date_from": lambda: serializers.DateField(),
    "po_date_to": lambda: serializers.DateField(),
    "invoice_date_from": lambda: serializers.DateField(),
    "invoice_date_to": lambda: serializers.DateField(),
    "due_date_from": lambda: serializers.DateField(),
    "due_date_to": lambda: serializers.DateField(),
    "date": lambda: serializers.DateField(),
    "start_datetime": lambda: serializers.DateTimeField(),
    "end_datetime": lambda: serializers.DateTimeField(),
    "start_date": lambda: serializers.DateTimeField(),
    "end_date": lambda: serializers.DateTimeField(),
    "uploaded_at_from": lambda: serializers.DateTimeField(),
    "uploaded_at_to": lambda: serializers.DateTimeField(),
    "updated_at_from": lambda: serializers.DateTimeField(),
    "updated_at_to": lambda: serializers.DateTimeField(),
    "purchase_order_id": lambda: serializers.IntegerField(min_value=1),
    "duration_minutes": lambda: serializers.IntegerField(min_value=1, max_value=24 * 60),
    "due_within_days": lambda: serializers.IntegerField(min_value=1, max_value=366),
    "related_to_me": lambda: serializers.BooleanField(),
    "late_only": lambda: serializers.BooleanField(),
    "include_sub_actions": lambda: serializers.BooleanField(),
    "personal": lambda: serializers.BooleanField(),
    "paid_only": lambda: serializers.BooleanField(),
    "download_all": lambda: serializers.BooleanField(),
    "confirmed": lambda: serializers.BooleanField(),
    "create_outlook_event": lambda: serializers.BooleanField(),
    "online_meeting": lambda: serializers.BooleanField(),
    "teams_meeting": lambda: serializers.BooleanField(),
    "attendee_emails": lambda: serializers.ListField(
        child=serializers.EmailField(), allow_empty=True, max_length=100
    ),
    "attendee_queries": lambda: serializers.ListField(
        child=serializers.CharField(allow_blank=False, max_length=320),
        allow_empty=False,
        max_length=25,
    ),
    "assignee_ids": lambda: serializers.ListField(
        child=serializers.IntegerField(min_value=1), allow_empty=True, max_length=100
    ),
}


COMMON_ACTION_ITEM_FILTERS = {
    "search",
    "status",
    "excluded_statuses",
    "assignee",
    "created_by",
    "target_date_from",
    "target_date_to",
    "forecast_date_from",
    "forecast_date_to",
    "closed_at_from",
    "closed_at_to",
    "late_only",
    "include_sub_actions",
    "related_to_me",
    "priority",
    "ordering",
}

TOOL_FILTERS = {
    "action_items_count": COMMON_ACTION_ITEM_FILTERS,
    "action_items_search": COMMON_ACTION_ITEM_FILTERS,
    "action_items_biggest_delay": COMMON_ACTION_ITEM_FILTERS,
    "action_items_group_by_project": COMMON_ACTION_ITEM_FILTERS,
    "action_items_group_by_milestone": COMMON_ACTION_ITEM_FILTERS,
    "action_items_financial_summary": COMMON_ACTION_ITEM_FILTERS,
    "action_items_closure_list": COMMON_ACTION_ITEM_FILTERS,
    "action_items_progress": COMMON_ACTION_ITEM_FILTERS,
    "action_items_invoice_candidates": COMMON_ACTION_ITEM_FILTERS,
    "phase_milestone_financial_summary": {"status"},
    "purchase_orders_summary": {
        "status", "po_type", "document_type", "reference", "po_date_from", "po_date_to"
    },
    "purchase_order_balance": {
        "purchase_order_id", "status", "po_type", "document_type", "reference", "po_date_from", "po_date_to"
    },
    "invoices_search": {
        "purchase_order_id", "status", "paid_only", "document_type", "invoice_date_from",
        "invoice_date_to", "search", "download_all"
    },
    "documents_count": {
        "status", "document_type", "author", "assignee", "created_by", "search",
        "uploaded_at_from", "uploaded_at_to", "updated_at_from", "updated_at_to",
        "content_query", "due_within_days", "due_date_from", "due_date_to"
    },
    "documents_search": {
        "status", "document_type", "author", "assignee", "created_by", "search",
        "uploaded_at_from", "uploaded_at_to", "updated_at_from", "updated_at_to",
        "content_query", "due_within_days", "due_date_from", "due_date_to"
    },
    "documents_group_by_status": set(),
    "object_assessments_count": {
        "status", "document_type", "author", "assignee", "created_by", "search",
        "uploaded_at_from", "uploaded_at_to", "updated_at_from", "updated_at_to"
    },
    "object_assessments_search": {
        "status", "document_type", "author", "assignee", "created_by", "search",
        "uploaded_at_from", "uploaded_at_to", "updated_at_from", "updated_at_to"
    },
    "calendar_count": {"date", "start_datetime", "end_datetime", "personal", "timezone"},
    "calendar_search": {"date", "start_datetime", "end_datetime", "personal", "timezone", "ordering"},
    "calendar_free_slot": {
        "date", "start_datetime", "end_datetime", "duration_minutes", "personal", "timezone"
    },
    "calendar_create_event": {
        "title", "name", "description", "start_datetime", "end_datetime", "start_date",
        "end_date", "duration_minutes", "confirmed", "confirmation_token", "assignee_ids",
        "attendee_emails", "create_outlook_event", "online_meeting", "teams_meeting", "solution", "timezone"
    },
    "calendar_attendee_resolver": {"attendee_queries"},
    "projects_summary": {"status", "search"},
    "project_email_context": set(),
    "project_team_members": set(),
    "company_team_members": {"search"},
    "matrix_gaps": {"gap_type"},
    "matrix_search": {"entity", "search"},
    "public_user_profile_search": {
        "search", "competence", "position", "qualification", "keywords", "location", "availability",
        "availability_from", "availability_to", "open_to_new_challenges"
    },
    "public_company_profile_search": {
        "search", "competence", "keywords", "products", "services", "location"
    },
    "public_profile_search": {
        "search", "competence", "position", "qualification", "keywords", "products", "services",
        "location", "availability", "availability_from", "availability_to", "open_to_new_challenges"
    },
    "network_search": {"entity", "entity_type", "search"},
}


class StrictFiltersField(serializers.Field):
    default_error_messages = {
        "invalid": "Expected an object of tool-specific filters.",
        "unknown": "Unsupported filter(s) for this tool: {names}.",
    }

    def __init__(self, *, tool_name, **kwargs):
        self.tool_name = tool_name
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if data in (None, ""):
            return {}
        if not isinstance(data, dict):
            self.fail("invalid")

        allowed = TOOL_FILTERS.get(self.tool_name)
        if allowed is None:
            allowed = set(FILTER_FIELD_FACTORIES)
        unknown = sorted(set(data) - set(allowed))
        if unknown:
            self.fail("unknown", names=", ".join(unknown))

        validated = {}
        errors = {}
        for name, value in data.items():
            field = FILTER_FIELD_FACTORIES[name]()
            field.bind(field_name=name, parent=self.parent)
            try:
                validated[name] = field.run_validation(value)
            except serializers.ValidationError as exc:
                errors[name] = exc.detail
        if errors:
            raise serializers.ValidationError(errors)
        return validated

    def to_representation(self, value):
        return value or {}


class AIToolRequestSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(
        choices=("company", "project", "accessible_projects", "all_accessible", "company_and_accessible_projects", "current_user"),
        required=False,
        default="all_accessible",
    )
    project_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    project_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    company_profile_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    folder_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=25)
    include_sources = serializers.BooleanField(required=False, default=True)
    confirmed = serializers.BooleanField(required=False, default=False)

    def __init__(self, *args, **kwargs):
        tool_name = kwargs.pop("tool_name", "")
        super().__init__(*args, **kwargs)
        self.fields["filters"] = StrictFiltersField(tool_name=tool_name, required=False, default=dict)

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError({name: ["Unsupported request field."] for name in unknown})
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs.get("scope") == "company_and_accessible_projects":
            attrs["scope"] = "all_accessible"
        if attrs.get("scope") == "project" and not (attrs.get("project_id") or attrs.get("project_name")):
            raise serializers.ValidationError(
                {"project_id": "A positive project ID or an exact visible project name is required for project scope."}
            )
        filters = attrs.get("filters") or {}
        for start_name, end_name in (
            ("target_date_from", "target_date_to"),
            ("forecast_date_from", "forecast_date_to"),
            ("closed_at_from", "closed_at_to"),
            ("po_date_from", "po_date_to"),
            ("invoice_date_from", "invoice_date_to"),
            ("uploaded_at_from", "uploaded_at_to"),
            ("updated_at_from", "updated_at_to"),
            ("availability_from", "availability_to"),
            ("start_datetime", "end_datetime"),
        ):
            start_value = filters.get(start_name)
            end_value = filters.get(end_name)
            if start_value and end_value and start_value > end_value:
                raise serializers.ValidationError(
                    {end_name: f"{end_name} must be on or after {start_name}."}
                )
        return attrs
