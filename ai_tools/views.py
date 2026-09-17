import json
import logging
import os
from hashlib import sha256
from time import perf_counter

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone

from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services
from .exceptions import AIToolClarificationRequired
from .models import AIToolAuditLog
from .permissions import AIToolServicePermission
from .serializers import AIToolRequestSerializer
from .business_schema import BUSINESS_QUERY_CONTRACT_VERSION, public_business_schema
from .query_gateway import (
    BusinessQuerySpecSerializer,
    execute_query,
    load_confirmed_query,
    normalized_query_spec,
    query_preview,
)


CONTRACT_VERSION = "6"
logger = logging.getLogger(__name__)

TOOL_QUERY_SHAPES = {
    "action_items_count": ("action_items", "count"),
    "action_items_search": ("action_items", "list"),
    "action_items_biggest_delay": ("action_items", "find_extreme"),
    "action_items_group_by_project": ("action_items", "group"),
    "action_items_group_by_milestone": ("action_items", "group"),
    "action_items_financial_summary": ("action_items", "aggregate"),
    "action_items_closure_list": ("action_items", "list"),
    "action_items_progress": ("action_items", "aggregate"),
    "action_items_invoice_candidates": ("action_items", "search"),
    "phase_milestone_financial_summary": ("projects", "aggregate"),
    "purchase_orders_summary": ("purchase_orders", "aggregate"),
    "purchase_order_balance": ("purchase_orders", "aggregate"),
    "invoices_search": ("invoices", "list"),
    "documents_count": ("documents", "count"),
    "documents_search": ("documents", "list"),
    "documents_group_by_status": ("documents", "group"),
    "calendar_search": ("calendar", "list"),
    "calendar_free_slot": ("calendar", "find_free_slot"),
    "calendar_create_event": ("workflow", "create"),
    "projects_summary": ("projects", "list"),
    "project_email_context": ("workflow", "prepare"),
    "project_team_members": ("projects", "list_members"),
    "company_team_members": ("company", "list_members"),
    "matrix_gaps": ("matrices", "find_gaps"),
    "matrix_search": ("matrices", "search"),
    "public_user_profile_search": ("profiles", "search"),
    "public_company_profile_search": ("profiles", "search"),
    "public_profile_search": ("profiles", "search"),
    "network_search": ("network", "search"),
}


def _query_spec(tool_name, payload):
    domain, operation = TOOL_QUERY_SHAPES.get(tool_name, ("unknown", "unknown"))
    scope = {
        "type": payload.get("scope", "all_accessible"),
        "project_id": payload.get("project_id"),
        "project_name": payload.get("project_name"),
        "company_profile_id": payload.get("company_profile_id"),
    }
    return {
        "domain": domain,
        "operation": operation,
        "scope": {key: value for key, value in scope.items() if value is not None},
        "filters": payload.get("filters", {}),
        "limit": payload.get("limit", 25),
    }


def _result_hash(result):
    authoritative_fields = {
        key: result.get(key)
        for key in (
            "query_spec",
            "summary",
            "rows",
            "chart",
            "sources",
            "warnings",
            "applied_scope",
            "applied_filters",
            "applied_project_id",
            "applied_project_name",
        )
    }
    canonical = json.dumps(
        authoritative_fields,
        cls=DjangoJSONEncoder,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _error_response(*, code, message, status_code, field=None, choices=None, details=None):
    return Response(
        {
            "contract_version": os.getenv("LINKS_AI_CONTRACT_VERSION", CONTRACT_VERSION),
            "authoritative": False,
            "data_source": "links_business_data",
            "error": {
                "code": code,
                "message": message,
                "field": field,
                "choices": choices or [],
                "details": details or {},
            },
        },
        status=status_code,
    )


class AIToolAPIView(APIView):
    permission_classes = [IsAuthenticated, AIToolServicePermission]
    tool_name = ""
    service_function = None

    def post(self, request, *args, **kwargs):
        started_at = perf_counter()
        status_value = AIToolAuditLog.STATUS_SUCCESS
        error_message = ""
        result = None
        payload = request.data if isinstance(request.data, dict) else {}
        serializer = AIToolRequestSerializer(data=request.data, tool_name=self.tool_name)
        try:
            if not serializer.is_valid():
                status_value = AIToolAuditLog.STATUS_ERROR
                error_message = str(serializer.errors)
                return _error_response(
                    code="invalid_query",
                    message="The business query contains invalid or unsupported fields.",
                    status_code=status.HTTP_400_BAD_REQUEST,
                    details=serializer.errors,
                )
            payload = serializer.validated_data
            result = self.service_function(request.user, payload)
            safe_payload = json.loads(json.dumps(payload, cls=DjangoJSONEncoder))
            if isinstance(result, dict):
                result = {
                    "contract_version": os.getenv("LINKS_AI_CONTRACT_VERSION", CONTRACT_VERSION),
                    "intent": self.tool_name,
                    "applied_scope": safe_payload.get("scope", "all_accessible"),
                    "applied_filters": safe_payload.get("filters", {}),
                    "applied_project_id": safe_payload.get("project_id"),
                    "applied_project_name": safe_payload.get("project_name"),
                    "authoritative": True,
                    "generated_at": timezone.now().isoformat(),
                    "data_source": "links_business_data",
                    "query_spec": _query_spec(self.tool_name, safe_payload),
                    **result,
                }
                result["result_hash"] = _result_hash(result)
            return Response(result)
        except AIToolClarificationRequired as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = exc.message
            return _error_response(
                code=exc.default_code,
                message=exc.message,
                field=exc.field,
                choices=exc.choices,
                status_code=exc.status_code,
            )
        except PermissionDenied:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = "Permission denied."
            return _error_response(
                code="permission_denied",
                message="You do not have permission to access the requested business data.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        except Exception as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = str(exc)
            raise
        finally:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            rows = result.get("rows", []) if isinstance(result, dict) else []
            summary = result.get("summary", {}) if isinstance(result, dict) else {}
            sources = result.get("sources", []) if isinstance(result, dict) else []
            authoritative_count = summary.get("count")
            if not isinstance(authoritative_count, int) or authoritative_count < 0:
                authoritative_count = len(rows)
            AIToolAuditLog.objects.create(
                user=request.user if request.user.is_authenticated else None,
                company_profile=getattr(request.user, "company", None),
                tool_name=self.tool_name,
                request_payload=json.loads(json.dumps(payload, cls=DjangoJSONEncoder)),
                result_count=authoritative_count,
                source_object_refs=sources,
                status=status_value,
                error_message=error_message[:2000],
                latency_ms=elapsed_ms,
            )


class AIContractHealthView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, *args, **kwargs):
        return Response(
            {
                "status": "ok",
                "service": "links-ai-backend",
                "contract_version": os.getenv("LINKS_AI_CONTRACT_VERSION", CONTRACT_VERSION),
                "business_query_contract_version": BUSINESS_QUERY_CONTRACT_VERSION,
                "git_sha": os.getenv("GIT_SHA", "unknown"),
                "image_version": os.getenv("IMAGE_VERSION", "unknown"),
            }
        )


class BusinessQuerySchemaView(APIView):
    permission_classes = [IsAuthenticated, AIToolServicePermission]

    def get(self, request, *args, **kwargs):
        schema = public_business_schema()
        visible_projects = services.visible_projects(
            request.user,
            {"scope": "all_accessible", "filters": {}},
        ).order_by("project_name", "id")
        project_catalog_limit = 250
        schema["visible_projects"] = [
            {"id": project["id"], "name": project["project_name"]}
            for project in visible_projects.values("id", "project_name")[
                :project_catalog_limit
            ]
        ]
        schema["visible_projects_truncated"] = (
            visible_projects.count() > project_catalog_limit
        )
        schema["configured_values"] = {
            "action_item_statuses": services.visible_action_item_statuses(
                request.user,
                project_queryset=visible_projects,
            )
        }
        return Response(schema)


class BusinessQueryPreviewView(APIView):
    permission_classes = [IsAuthenticated, AIToolServicePermission]

    def post(self, request, *args, **kwargs):
        serializer = BusinessQuerySpecSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(
                "Business Query preview validation failed: %s",
                serializer.errors,
            )
            return _error_response(
                code="invalid_query",
                message="The business query contains invalid or unsupported fields.",
                details=serializer.errors,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            preview = query_preview(serializer.validated_data, user=request.user)
        except AIToolClarificationRequired as exc:
            return _error_response(
                code=exc.default_code,
                message=exc.message,
                field=exc.field,
                choices=exc.choices,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except PermissionDenied:
            return _error_response(
                code="permission_denied",
                message="The requested scope is not accessible.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        return Response(preview)


class BusinessQueryExecuteView(APIView):
    permission_classes = [IsAuthenticated, AIToolServicePermission]

    def post(self, request, *args, **kwargs):
        started_at = perf_counter()
        result = None
        query_spec = {}
        status_value = AIToolAuditLog.STATUS_SUCCESS
        error_message = ""
        token = request.data.get("confirmation_token") if isinstance(request.data, dict) else None
        if not token:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = "Confirmation required."
            self._audit(request, started_at, status_value, error_message, query_spec, result)
            return _error_response(
                code="confirmation_required",
                message="Confirm the business query before it is executed.",
                field="confirmation_token",
                status_code=status.HTTP_409_CONFLICT,
            )

        try:
            validated_data = load_confirmed_query(token, user_id=request.user.id)
            query_spec = normalized_query_spec(validated_data)
            result = execute_query(request.user, validated_data)
        except ValidationError as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = "Invalid or expired confirmation token."
            return _error_response(
                code="invalid_confirmation",
                message="The query confirmation is invalid or has expired.",
                details=exc.detail,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        except AIToolClarificationRequired as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = exc.message
            return _error_response(
                code=exc.default_code,
                message=exc.message,
                field=exc.field,
                choices=exc.choices,
                status_code=exc.status_code,
            )
        except PermissionDenied:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = "Permission denied."
            return _error_response(
                code="permission_denied",
                message="You do not have permission to access the requested business data.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
        except Exception as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = str(exc)
            raise
        finally:
            self._audit(request, started_at, status_value, error_message, query_spec, result)

        result["result_hash"] = _result_hash(result)
        return Response(result)

    @staticmethod
    def _audit(request, started_at, status_value, error_message, query_spec, result):
        elapsed_ms = int((perf_counter() - started_at) * 1000)
        result = result if isinstance(result, dict) else {}
        rows = result.get("rows") or []
        summary = result.get("summary") or {}
        authoritative_count = summary.get("count")
        if not isinstance(authoritative_count, int) or authoritative_count < 0:
            authoritative_count = len(rows)
        AIToolAuditLog.objects.create(
            user=request.user,
            company_profile=getattr(request.user, "company", None),
            tool_name="business_query_execute",
            request_payload=result.get("query_spec") or query_spec or {},
            result_count=authoritative_count,
            source_object_refs=result.get("sources") or [],
            status=status_value,
            error_message=error_message[:2000],
            latency_ms=elapsed_ms,
        )


class BusinessQueryRunView(APIView):
    """Validate and execute one permission-filtered read query atomically."""

    permission_classes = [IsAuthenticated, AIToolServicePermission]

    def post(self, request, *args, **kwargs):
        started_at = perf_counter()
        result = None
        query_spec = request.data if isinstance(request.data, dict) else {}
        status_value = AIToolAuditLog.STATUS_SUCCESS
        error_message = ""
        response = None

        serializer = BusinessQuerySpecSerializer(data=query_spec)
        try:
            if not serializer.is_valid():
                status_value = AIToolAuditLog.STATUS_ERROR
                error_message = str(serializer.errors)
                response = _error_response(
                    code="invalid_query",
                    message="The business query contains invalid or unsupported fields.",
                    details=serializer.errors,
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
                return response

            query_spec = normalized_query_spec(serializer.validated_data)
            result = execute_query(request.user, serializer.validated_data)
            result["result_hash"] = _result_hash(result)
            response = Response(result)
            return response
        except ValidationError as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = str(exc.detail)
            response = _error_response(
                code="invalid_query",
                message="The business query contains invalid or unsupported fields.",
                details=exc.detail,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
            return response
        except AIToolClarificationRequired as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = exc.message
            response = _error_response(
                code=exc.default_code,
                message=exc.message,
                field=exc.field,
                choices=exc.choices,
                status_code=exc.status_code,
            )
            return response
        except PermissionDenied:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = "Permission denied."
            response = _error_response(
                code="permission_denied",
                message="You do not have permission to access the requested business data.",
                status_code=status.HTTP_403_FORBIDDEN,
            )
            return response
        except Exception as exc:
            status_value = AIToolAuditLog.STATUS_ERROR
            error_message = str(exc)
            raise
        finally:
            elapsed_ms = int((perf_counter() - started_at) * 1000)
            result_payload = result if isinstance(result, dict) else {}
            rows = result_payload.get("rows") or []
            summary = result_payload.get("summary") or {}
            authoritative_count = summary.get("count")
            if not isinstance(authoritative_count, int) or authoritative_count < 0:
                authoritative_count = len(rows)
            audit = AIToolAuditLog.objects.create(
                user=request.user,
                company_profile=getattr(request.user, "company", None),
                tool_name="business_query_run",
                request_payload=json.loads(json.dumps(query_spec, cls=DjangoJSONEncoder)),
                result_count=authoritative_count,
                source_object_refs=result_payload.get("sources") or [],
                status=status_value,
                error_message=error_message[:2000],
                latency_ms=elapsed_ms,
            )
            if isinstance(result, dict):
                result["audit_id"] = audit.pk


def tool_view(name, function):
    return type(
        f"{name.title().replace('_', '')}View",
        (AIToolAPIView,),
        {"tool_name": name, "service_function": staticmethod(function)},
    ).as_view()
