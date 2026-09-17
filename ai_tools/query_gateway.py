import json

from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from rest_framework import serializers

from . import services
from .business_schema import (
    BUSINESS_QUERY_CONTRACT_VERSION,
    BUSINESS_QUERY_OPERATIONS,
    ENTITY_DEFAULT_FILTERS,
    OPERATION_DEFAULT_FILTERS,
)
from .localization import (
    localize_action_item_names_in_result,
    localize_project_types_in_result,
)
from .serializers import AIToolRequestSerializer, TOOL_FILTERS


BUSINESS_QUERY_SIGNING_SALT = "ai-tools.business-query.v4"
BUSINESS_QUERY_TOKEN_MAX_AGE_SECONDS = 10 * 60


class BusinessQueryScopeSerializer(serializers.Serializer):
    type = serializers.ChoiceField(
        choices=("company", "project", "accessible_projects", "all_accessible", "current_user"),
        default="all_accessible",
    )
    project_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    project_name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    company_profile_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    def validate(self, attrs):
        if attrs.get("type") == "project" and not (attrs.get("project_id") or attrs.get("project_name")):
            raise serializers.ValidationError(
                {"project_id": "A project ID or exact visible project name is required for project scope."}
            )
        return attrs


class BusinessQuerySpecSerializer(serializers.Serializer):
    entity = serializers.ChoiceField(choices=tuple(BUSINESS_QUERY_OPERATIONS))
    operation = serializers.CharField(max_length=64)
    scope = BusinessQueryScopeSerializer(required=False, default=dict)
    filters = serializers.DictField(required=False, default=dict)
    metrics = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list, max_length=20
    )
    group_by = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list, max_length=10
    )
    sort = serializers.ListField(
        child=serializers.CharField(max_length=64), required=False, default=list, max_length=10
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=100, default=25)
    include_sources = serializers.BooleanField(required=False, default=True)
    language = serializers.ChoiceField(choices=("en", "de", "fr"), required=False, default="en")
    timezone = serializers.CharField(required=False, allow_blank=False, max_length=100, default="UTC")

    def validate(self, attrs):
        entity = attrs["entity"]
        operation_name = attrs["operation"]
        operation = BUSINESS_QUERY_OPERATIONS[entity].get(operation_name)
        if operation is None:
            raise serializers.ValidationError(
                {"operation": f"Unsupported operation '{operation_name}' for entity '{entity}'."}
            )

        supported_filters = set(TOOL_FILTERS.get(operation.tool_name) or [])
        entity_defaults = {
            name: value
            for name, value in ENTITY_DEFAULT_FILTERS.get(entity, {}).items()
            if name in supported_filters
        }
        filters = {
            **entity_defaults,
            **OPERATION_DEFAULT_FILTERS.get((entity, operation_name), {}),
            **(attrs.get("filters") or {}),
        }
        if (
            entity == "action_items"
            and filters.get("late_only")
            and not filters.get("status")
            and not filters.get("excluded_statuses")
        ):
            filters["excluded_statuses"] = ["Closed"]
        if entity == "action_items" and operation_name == "biggest_delay" and not (
            filters.get("status") or filters.get("excluded_statuses")
        ):
            filters["excluded_statuses"] = ["Closed"]
        sort = attrs.get("sort") or []
        if sort:
            if entity == "action_items" and operation_name in {"list", "closure_list"} and len(sort) == 1:
                filters["ordering"] = sort[0]
            elif (
                entity == "calendar_events"
                and operation_name == "list"
                and sort == ["project"]
            ):
                filters["ordering"] = "project"
            else:
                raise serializers.ValidationError(
                    {"sort": "Sorting is not supported for this entity and operation."}
                )
        if attrs.get("metrics"):
            raise serializers.ValidationError(
                {"metrics": "Select an aggregate operation instead of supplying custom metrics."}
            )
        if attrs.get("group_by"):
            raise serializers.ValidationError(
                {"group_by": "Select a grouped operation instead of supplying custom grouping fields."}
            )
        if entity == "calendar_events" and attrs.get("timezone"):
            filters.setdefault("timezone", attrs["timezone"])
        scope = attrs.get("scope") or {"type": "all_accessible"}
        if entity == "action_items" and operation_name == "group_by_milestone":
            if scope.get("type") != "project" or not (
                scope.get("project_id") or scope.get("project_name")
            ):
                raise serializers.ValidationError(
                    {"scope": "A project ID or exact project name is required for milestone grouping."}
                )
        payload = {
            "scope": scope.get("type", "all_accessible"),
            "project_id": scope.get("project_id"),
            "project_name": scope.get("project_name"),
            "company_profile_id": scope.get("company_profile_id"),
            "filters": filters,
            "limit": attrs.get("limit", 25),
            "include_sources": attrs.get("include_sources", True),
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        tool_serializer = AIToolRequestSerializer(data=payload, tool_name=operation.tool_name)
        if not tool_serializer.is_valid():
            raise serializers.ValidationError({"query": tool_serializer.errors})

        attrs["filters"] = filters
        attrs["compiled_tool_name"] = operation.tool_name
        attrs["compiled_payload"] = tool_serializer.validated_data
        attrs["answer_type"] = operation.answer_type
        attrs["description"] = operation.description
        return attrs


def normalized_query_spec(validated_data):
    scope = validated_data.get("scope") or {"type": "all_accessible"}
    return {
        "entity": validated_data["entity"],
        "operation": validated_data["operation"],
        "scope": dict(scope),
        "filters": dict(validated_data.get("filters") or {}),
        "metrics": list(validated_data.get("metrics") or []),
        "group_by": list(validated_data.get("group_by") or []),
        "sort": list(validated_data.get("sort") or []),
        "limit": validated_data.get("limit", 25),
        "include_sources": validated_data.get("include_sources", True),
        "language": validated_data.get("language", "en"),
        "timezone": validated_data.get("timezone", "UTC"),
    }


def validate_query_before_confirmation(user, validated_data):
    payload = validated_data["compiled_payload"]
    entity = validated_data["entity"]

    # Resolve permission-sensitive scope before presenting a query as runnable.
    # Entity validators construct lazy querysets and validate configured choice
    # values; they do not return business records to the caller.
    services.ensure_project_visible(user, payload)
    services.resolve_company_profile_id(user, payload)

    validators = {
        "action_items": services.visible_action_items,
        "documents": services.visible_documents,
        "object_assessments": services.visible_object_assessment_documents,
        "projects": services.visible_projects,
        "purchase_orders": services.visible_purchase_orders,
        "invoices": services.visible_invoices,
    }
    validator = validators.get(entity)
    if validator:
        validator(user, payload)


def query_preview(validated_data, *, user):
    validate_query_before_confirmation(user, validated_data)
    spec = normalized_query_spec(validated_data)
    token_payload = {
        "contract_version": BUSINESS_QUERY_CONTRACT_VERSION,
        "user_id": user.id,
        "query_spec": spec,
    }
    token = signing.dumps(token_payload, salt=BUSINESS_QUERY_SIGNING_SALT, compress=True)
    return {
        "contract_version": BUSINESS_QUERY_CONTRACT_VERSION,
        "authoritative": False,
        "requires_confirmation": True,
        "confirmation_token": token,
        "confirmation_expires_in_seconds": BUSINESS_QUERY_TOKEN_MAX_AGE_SECONDS,
        "query_spec": spec,
        "preview": {
            "description": validated_data["description"],
            "entity": spec["entity"],
            "operation": spec["operation"],
            "scope": spec["scope"],
            "filters": spec["filters"],
            "limit": spec["limit"],
        },
    }


def load_confirmed_query(token, *, user_id):
    try:
        payload = signing.loads(
            token,
            salt=BUSINESS_QUERY_SIGNING_SALT,
            max_age=BUSINESS_QUERY_TOKEN_MAX_AGE_SECONDS,
        )
    except signing.SignatureExpired as exc:
        raise serializers.ValidationError({"confirmation_token": "The query confirmation has expired."}) from exc
    except signing.BadSignature as exc:
        raise serializers.ValidationError({"confirmation_token": "The query confirmation is invalid."}) from exc
    if payload.get("contract_version") != BUSINESS_QUERY_CONTRACT_VERSION:
        raise serializers.ValidationError({"confirmation_token": "The query contract version is no longer valid."})
    if payload.get("user_id") != user_id:
        raise serializers.ValidationError({"confirmation_token": "The query confirmation belongs to another user."})
    serializer = BusinessQuerySpecSerializer(data=payload.get("query_spec"))
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def execute_query(user, validated_data):
    entity = validated_data["entity"]
    operation = BUSINESS_QUERY_OPERATIONS[entity][validated_data["operation"]]
    service_function = getattr(services, operation.service_name)
    safe_spec = normalized_query_spec(validated_data)
    result = service_function(user, validated_data["compiled_payload"])
    result = localize_project_types_in_result(result, safe_spec["language"])
    if entity == "action_items":
        result = localize_action_item_names_in_result(
            result,
            safe_spec["language"],
        )
    response = {
        "contract_version": BUSINESS_QUERY_CONTRACT_VERSION,
        "authoritative": True,
        "data_source": "links_business_data",
        "intent": f"{entity}.{validated_data['operation']}",
        "answer_type": operation.answer_type,
        "query_spec": json.loads(json.dumps(safe_spec, cls=DjangoJSONEncoder)),
        "applied_scope": safe_spec["scope"],
        "applied_filters": safe_spec["filters"],
        "generated_at": timezone.now().isoformat(),
        **result,
    }
    if entity == "documents":
        assessment_count = (response.get("summary") or {}).get(
            "object_assessment_record_count"
        )
        if isinstance(assessment_count, int) and assessment_count > 0:
            related_spec = safe_spec.copy()
            related_spec.update(
                {
                    "entity": "object_assessments",
                    "operation": "list",
                    "metrics": [],
                    "group_by": [],
                    "sort": [],
                }
            )
            response["related_queries"] = {
                "object_assessments": {
                    "count": assessment_count,
                    "query_spec": related_spec,
                }
            }
    return response
