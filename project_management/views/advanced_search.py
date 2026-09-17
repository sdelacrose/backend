from django.conf import settings
from django.db import OperationalError
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import SearchIndexJob, SearchRecord
from project_management.serializers.advanced_search import (
    AdvancedSearchBackfillSerializer,
    AdvancedSearchQuerySerializer,
)
from project_management.services.advanced_search import (
    AdvancedSearchValidationError,
    advanced_search,
    authorized_source_querysets,
    index_summary,
    search_config,
)
from project_management.services.advanced_search_resilience import (
    AdvancedSearchBusyError,
    AdvancedSearchRateLimitError,
    advanced_search_capacity,
    advanced_search_statement_timeout,
    is_statement_timeout,
)
from project_management.services.search_indexing import (
    SOURCE_MODELS,
    backfill_search_records,
    enqueue_search_indexing,
)


def _scope_from_query_params(request):
    project_id = request.query_params.get("project_id")
    if project_id:
        try:
            return {"type": "project", "id": int(project_id)}
        except (TypeError, ValueError) as exc:
            raise AdvancedSearchValidationError("Project ID must be a positive integer.") from exc
    return {"type": "company"}


def _validation_response(exc):
    return Response(
        {"detail": exc.detail, "suggestions": exc.suggestions},
        status=status.HTTP_400_BAD_REQUEST,
    )


class AdvancedSearchQueryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not getattr(settings, "ADVANCED_SEARCH_ENABLED", True):
            return Response(
                {"detail": "Advanced search is disabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        serializer = AdvancedSearchQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with advanced_search_capacity(request.user.id):
                with advanced_search_statement_timeout():
                    response = advanced_search(request, serializer.validated_data)
            return Response(response)
        except AdvancedSearchValidationError as exc:
            return _validation_response(exc)
        except AdvancedSearchRateLimitError as exc:
            response = Response(
                {
                    "detail": "Too many advanced search requests. Please wait before trying again.",
                    "code": "advanced_search_rate_limited",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response["Retry-After"] = str(exc.retry_after)
            return response
        except AdvancedSearchBusyError as exc:
            response = Response(
                {
                    "detail": "Advanced search is busy. Please try again shortly.",
                    "code": "advanced_search_busy",
                    "retry_after": exc.retry_after,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
            response["Retry-After"] = str(exc.retry_after)
            return response
        except OperationalError as exc:
            if not is_statement_timeout(exc):
                raise
            return Response(
                {
                    "detail": "Advanced search took too long. Please refine the search and try again.",
                    "code": "advanced_search_timeout",
                },
                status=status.HTTP_504_GATEWAY_TIMEOUT,
            )


class AdvancedSearchConfigAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            return Response(search_config(request, _scope_from_query_params(request)))
        except AdvancedSearchValidationError as exc:
            return _validation_response(exc)


class AdvancedSearchIndexSummaryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        try:
            return Response(index_summary(request, _scope_from_query_params(request)))
        except AdvancedSearchValidationError as exc:
            return _validation_response(exc)


class AdvancedSearchBackfillAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not request.user.is_staff and not request.user.is_superuser:
            return Response(
                {"detail": "Only administrators can start an advanced search backfill."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not getattr(settings, "ADVANCED_SEARCH_BACKFILL_ENABLED", False):
            return Response(
                {"detail": "Advanced search backfill is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        serializer = AdvancedSearchBackfillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source_types = serializer.validated_data["source_types"] or None
        if source_types:
            invalid = sorted(set(source_types) - set(SOURCE_MODELS))
            if invalid:
                return Response(
                    {"detail": "Unknown source types.", "invalid": invalid},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        return Response(
            backfill_search_records(
                source_types=source_types,
                after_id=serializer.validated_data["after_id"],
                limit=serializer.validated_data["limit"],
            ),
            status=status.HTTP_202_ACCEPTED,
        )


class AdvancedSearchRetryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, source_type, source_id):
        if source_type not in SOURCE_MODELS:
            return Response(
                {"detail": "Unknown search source type."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        scope = _scope_from_query_params(request)
        source_querysets = authorized_source_querysets(
            request,
            scope,
            {source_type},
        )
        queryset = source_querysets.get(source_type)
        if queryset is None:
            return Response(
                {"detail": "This source cannot be retried."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        get_object_or_404(queryset, id=source_id)
        SearchIndexJob.objects.filter(
            source_type=source_type,
            source_id=source_id,
            status__in=["queued", "running", "retrying"],
        ).update(status=SearchIndexJob.Status.CANCELLED)
        job = enqueue_search_indexing(source_type, source_id)
        return Response(
            {"job_id": job.id, "status": job.status},
            status=status.HTTP_202_ACCEPTED,
        )
