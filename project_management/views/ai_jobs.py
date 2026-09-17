from django.conf import settings
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from project_content.services import ProjectContentError
from project_management.services.process_requirements_extraction import (
    ProcessRequirementsExtractionError,
)
from project_management.services.product_assembly_extraction import (
    ProductAssemblyExtractionError,
)
from project_management.services.product_attribute_extraction import (
    ProductAttributeExtractionError,
)
from project_management.services.ai_jobs import (
    ADAPTER_BY_KIND,
    RETRYABLE_STATES,
    adapt_ai_run,
    create_replacement_ai_run,
    find_active_replacement,
    get_project_ai_job,
    list_project_ai_jobs,
    normalize_state,
)
from project_management.pagination import CustomPageNumberPagination
from project_management.project_access import accessible_projects


AI_JOB_RETRY_ERRORS = (
    ProductAssemblyExtractionError,
    ProductAttributeExtractionError,
    ProcessRequirementsExtractionError,
    ProjectContentError,
)


class ProjectAIJobViewSet(viewsets.ViewSet):
    permission_classes = [IsAuthenticated]
    pagination_class = CustomPageNumberPagination

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not settings.PROJECT_AI_JOB_VIEW_ENABLED:
            raise Http404()

    def list(self, request):
        project_id = request.query_params.get("project")
        if not project_id:
            return Response({"detail": "project is required."}, status=400)
        project = get_object_or_404(accessible_projects(request.user), pk=project_id)
        active_only = request.query_params.get("active", "false").lower() == "true"
        jobs = list_project_ai_jobs(
            request.user,
            project.pk,
            active_only=active_only,
            request=request,
        )
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(jobs, request, view=self)
        return paginator.get_paginated_response(page)

    def retrieve(self, request, pk=None):
        job = get_project_ai_job(request.user, pk, request=request)
        if not job:
            raise Http404
        return Response(job)

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        job = get_project_ai_job(request.user, pk, request=request, write=True)
        if not job:
            raise Http404
        if job["state"] not in RETRYABLE_STATES:
            return Response(
                {"detail": "Only failed or stale jobs can be retried."},
                status=status.HTTP_409_CONFLICT,
            )
        kind, source_id = pk.split(":", 1)
        adapter = ADAPTER_BY_KIND[kind]
        run = adapter.model.objects.select_related("project").get(pk=source_id)
        active = find_active_replacement(run)
        if active:
            return Response({**adapt_ai_run(active), "reused": True})

        try:
            replacement = create_replacement_ai_run(adapter, run, request.user, request)
        except AI_JOB_RETRY_ERRORS as exc:
            return Response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(
            {**adapt_ai_run(replacement), "reused": False},
            status=status.HTTP_201_CREATED,
        )
