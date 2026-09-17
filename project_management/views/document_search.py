from django.conf import settings
from django.db.models import Count
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from project_management.models import (
    Document,
    DocumentContentSegment,
    DocumentIndexJob,
    DocumentSearchIndex,
)
from project_management.permissions import DocumentPermission
from project_management.services.document_indexing import (
    accessible_document_queryset,
    build_search_result,
    enqueue_document_indexing,
    get_document_indexing_summary,
    retry_due_jobs,
    search_documents,
)


def parse_positive_int(value, *, default, maximum):
    try:
        parsed = int(value or default)
    except (TypeError, ValueError) as exc:
        raise ValueError("Value must be a positive integer.") from exc
    if parsed < 1:
        raise ValueError("Value must be a positive integer.")
    return min(parsed, maximum)


class DocumentContentSearchAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if not getattr(settings, "DOCUMENT_CONTENT_SEARCH_ENABLED", False):
            return Response(
                {"detail": "Document content search is disabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        query = (request.query_params.get("q") or "").strip()
        if len(query) < 3:
            return Response(
                {
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                    "indexing": get_document_indexing_summary(request),
                }
            )

        try:
            page_size = parse_positive_int(
                request.query_params.get("page_size"),
                default=10,
                maximum=100,
            )
            page_number = parse_positive_int(
                request.query_params.get("page"),
                default=1,
                maximum=2_147_483_647,
            )
        except ValueError:
            return Response(
                {"detail": "Page and page_size must be positive integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        documents = accessible_document_queryset(request)
        document_results, total_count = search_documents(
            request,
            documents=documents,
        )
        total_pages = max(1, (total_count + page_size - 1) // page_size)
        page_number = min(page_number, total_pages)
        offset = (page_number - 1) * page_size
        page_documents = list(
            document_results[offset : offset + page_size]
        )
        segment_ids = {
            document.matched_content_segment_id
            for document in page_documents
            if document.matched_content_segment_id
        }
        segments_by_id = {
            segment.id: segment
            for segment in DocumentContentSegment.objects.filter(
                id__in=segment_ids,
            ).select_related("index")
        }
        results = [
            build_search_result(
                document,
                query,
                segments_by_id=segments_by_id,
                request=request,
            )
            for document in page_documents
        ]
        return Response(
            {
                "count": total_count,
                "next": page_number + 1 if page_number < total_pages else None,
                "previous": page_number - 1 if page_number > 1 else None,
                "results": results,
                "indexing": get_document_indexing_summary(
                    request,
                    documents=documents,
                ),
            }
        )


class DocumentIndexStatusAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        document = get_object_or_404(accessible_document_queryset(request), pk=pk)
        try:
            index = document.search_index
        except DocumentSearchIndex.DoesNotExist:
            return Response(
                {
                    "document_id": document.id,
                    "status": "not_indexed",
                    "attempt_count": 0,
                    "error": "",
                    "failure_category": "",
                    "indexed_at": None,
                }
            )
        return Response(
            {
                "document_id": document.id,
                "status": index.status,
                "attempt_count": index.attempt_count,
                "error": index.error if request.user.is_staff or request.user.is_superuser else "",
                "failure_category": index.failure_category,
                "indexed_at": index.indexed_at,
                "extraction_method": index.extraction_method,
                "mime_type": index.mime_type,
                "page_count": index.page_count,
            }
        )


class DocumentIndexRetryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        document = get_object_or_404(accessible_document_queryset(request), pk=pk)
        permission = DocumentPermission("update")
        if not permission.has_update_permission(request, self, document):
            return Response(
                {"detail": "You do not have permission to retry extraction for this document."},
                status=status.HTTP_403_FORBIDDEN,
            )
        job = enqueue_document_indexing(document.id, force=True)
        if not job:
            return Response(
                {"detail": "Document indexing is disabled or the document has no file."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"job_id": job.id, "status": job.status}, status=status.HTTP_202_ACCEPTED)


class DocumentIndexSummaryAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        documents = accessible_document_queryset(request)
        index_counts = (
            DocumentSearchIndex.objects.filter(document__in=documents)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        )
        job_counts = (
            DocumentIndexJob.objects.filter(document__in=documents)
            .values("status")
            .annotate(count=Count("id"))
            .order_by("status")
        )
        return Response(
            {
                "indexes": {row["status"]: row["count"] for row in index_counts},
                "jobs": {row["status"]: row["count"] for row in job_counts},
            }
        )


class DocumentIndexBackfillAPIView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if not request.user.is_staff and not request.user.is_superuser:
            return Response(
                {"detail": "Only administrators can start a document indexing backfill."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not getattr(settings, "DOCUMENT_BACKFILL_ENABLED", False):
            return Response(
                {"detail": "Document indexing backfill is disabled."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            limit = parse_positive_int(
                request.data.get("limit"),
                default=100,
                maximum=500,
            )
            after_id = int(request.data.get("after_id", 0) or 0)
        except ValueError:
            return Response(
                {"detail": "Limit must be positive and after_id must not be negative."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if after_id < 0:
            return Response(
                {"detail": "Limit must be positive and after_id must not be negative."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        documents = list(
            accessible_document_queryset(request)
            .filter(file__isnull=False, id__gt=after_id)
            .order_by("id")[:limit]
        )
        retried = retry_due_jobs(limit=limit)
        created = 0
        for document in documents:
            if enqueue_document_indexing(document.id):
                created += 1
        return Response(
            {
                "processed": len(documents),
                "queued": created,
                "retried": retried,
                "next_after_id": documents[-1].id if len(documents) == limit else None,
            }
        )
