from dataclasses import dataclass
from typing import Any, Callable

from rest_framework.exceptions import PermissionDenied

from project_management.models import (
    CatalogRecommendationRun,
    ProcessAssemblyExtractionRun,
    ProcessAttributeExtractionRun,
    ProductAssemblyExtractionRun,
    ProductAttributeExtractionRun,
)
from project_content.models import AcceptanceGenerationRun, ProjectDocumentGenerationRun
from project_management.project_access import accessible_projects


ACTIVE_STATES = {"queued", "running", "review_ready", "applying"}
RETRYABLE_STATES = {"failed", "stale"}


def _retry_product_assembly(run, user, request):
    from project_management.services.product_assembly_extraction import (
        create_extraction_run,
        dispatch_extraction_run,
    )

    replacement = create_extraction_run(
        run.project,
        user,
        project_language=run.project_language,
        request=request,
    )
    dispatch_extraction_run(replacement.pk)
    return replacement


def _retry_product_attribute(run, user, request):
    from project_management.services.product_attribute_extraction import (
        create_attribute_extraction_run,
        dispatch_attribute_extraction_run,
    )

    replacement = create_attribute_extraction_run(
        run.project,
        user,
        project_language=run.project_language,
        request=request,
    )
    dispatch_attribute_extraction_run(replacement.pk)
    return replacement


def _retry_process_assembly(run, user, _request):
    from project_management.services.process_requirements_extraction import (
        create_process_assembly_run,
        dispatch_process_assembly_run,
    )

    replacement = create_process_assembly_run(
        run.project,
        user,
        project_language=run.project_language,
    )
    dispatch_process_assembly_run(replacement.pk)
    return replacement


def _retry_process_attribute(run, user, _request):
    from project_management.services.process_requirements_extraction import (
        create_process_attribute_run,
        dispatch_process_attribute_run,
    )

    replacement = create_process_attribute_run(
        run.project,
        user,
        project_language=run.project_language,
    )
    dispatch_process_attribute_run(replacement.pk)
    return replacement


def _retry_project_content(run, user, request):
    from project_content.services import retry_generation_run

    return retry_generation_run(run, user, request=request)


def _retry_catalog(run, user, request):
    from project_management.services.catalog_recommendation import create_catalog_run, dispatch_catalog_run
    replacement = create_catalog_run(run.project, user, request, run.project_language)
    dispatch_catalog_run(replacement.pk)
    return replacement


@dataclass(frozen=True)
class AIJobAdapter:
    kind: str
    label: str
    model: type
    initiating_url: str
    source_endpoint: str
    retry_handler: Callable[[Any, Any, Any], Any]


ADAPTERS = (
    AIJobAdapter(
        "catalog_recommendation", "Assembly catalog recommendations",
        CatalogRecommendationRun, "/project-product-services/solution-space",
        "catalog-recommendations", _retry_catalog,
    ),
    AIJobAdapter(
        "product_assembly",
        "Customer Demand generation",
        ProductAssemblyExtractionRun,
        "/project-system-requirements/customer-demands",
        "product-assembly-extractions",
        _retry_product_assembly,
    ),
    AIJobAdapter(
        "product_attribute",
        "Product attribute extraction",
        ProductAttributeExtractionRun,
        "/project-system-requirements/customer-demands",
        "product-attribute-extractions",
        _retry_product_attribute,
    ),
    AIJobAdapter(
        "process_assembly",
        "Technical Requirement generation",
        ProcessAssemblyExtractionRun,
        "/project-system-requirements/technical-requirements",
        "process-assembly-extractions",
        _retry_process_assembly,
    ),
    AIJobAdapter(
        "process_attribute",
        "Process attribute extraction",
        ProcessAttributeExtractionRun,
        "/project-system-requirements/technical-requirements",
        "process-attribute-extractions",
        _retry_process_attribute,
    ),
    AIJobAdapter(
        "project_document",
        "Project document generation",
        ProjectDocumentGenerationRun,
        "/project-document-management",
        "project-content/generation-runs",
        _retry_project_content,
    ),
    AIJobAdapter(
        "acceptance",
        "Acceptance test generation",
        AcceptanceGenerationRun,
        "/project-execution/acceptance-tests",
        "project-content/acceptance-generation-runs",
        _retry_project_content,
    ),
)
ADAPTER_BY_KIND = {adapter.kind: adapter for adapter in ADAPTERS}
ADAPTER_BY_MODEL = {adapter.model: adapter for adapter in ADAPTERS}


def normalize_state(status):
    return {
        "extracting": "running",
        "generating": "running",
        "applied": "completed",
    }.get(status, status)


def adapt_ai_run(run):
    adapter = ADAPTER_BY_MODEL[type(run)]
    state = normalize_state(run.status)
    job_id = f"{adapter.kind}:{run.pk}"
    expired = state == "review_ready" and not bool(run.result)
    label = adapter.label
    initiating_url = adapter.initiating_url
    if adapter.kind == "acceptance" and run.schema_version == "2" and run.fat_phase_id:
        initiating_url = "/project-system-requirements/technical-requirements"
    if adapter.kind == "project_document":
        label = (
            "Technical Specification generation"
            if run.kind == "requirements_specification"
            else "Offer generation"
        )
    source_prefix = "/api/" if adapter.kind in {"project_document", "acceptance"} else "/api/project_management/"
    return {
        "id": job_id,
        "source_id": str(run.pk),
        "project": run.project_id,
        "kind": adapter.kind,
        "label": label,
        "state": state,
        "progress": {"step": state, "percent": None},
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": getattr(run, "applied_at", None) or run.completed_at,
        "updated_at": run.updated_at,
        "initiating_url": initiating_url,
        "result_url": initiating_url,
        "source_url": f"{source_prefix}{adapter.source_endpoint}/{run.pk}/",
        "can_retry": state in RETRYABLE_STATES,
        "retry_url": (
            f"/api/project_management/ai-jobs/{job_id}/retry/"
            if state in RETRYABLE_STATES
            else None
        ),
        # The aggregate endpoint does not proxy cancellation. Keep this false
        # until a job type has an explicit, race-safe aggregate cancel handler.
        "can_cancel": False,
        "result_expired": expired,
        "error": (
            {"code": run.error_code, "message": run.error_message}
            if run.error_code or run.error_message
            else None
        ),
    }


def has_ai_job_access(request, adapter, run, *, write=False):
    if adapter.kind == "catalog_recommendation":
        from project_management.services.catalog_recommendation import require_catalog_access
        try:
            require_catalog_access(request, run.project, write=write, run=run)
        except PermissionDenied:
            return False
        return True
    from project_content.permissions import (
        ACCEPTANCE_TESTS_MODULE,
        PROJECT_CONTENT_MODULE,
        require_acceptance_generation_access,
        require_project_content_access,
    )
    from project_management.permissions import RequirementPermission, SolutionDemandPermission
    from project_management.views.product_assembly_extraction import (
        ProductAssemblyProjectManagerPermission,
    )

    context = {"project": run.project}
    if adapter.kind in {"product_assembly", "product_attribute"}:
        permission = SolutionDemandPermission()
    elif adapter.kind in {"process_assembly", "process_attribute"}:
        permission = RequirementPermission()
    else:
        permission = None
    if permission:
        allowed = (
            permission._check_create_permission(request.user, context)
            if write
            else permission._check_view_permission_project(request.user, run, context)
        )
        if not allowed:
            return False
        return not write or ProductAssemblyProjectManagerPermission._is_project_manager(
            request.user,
            run.project,
        )
    try:
        if adapter.kind == "project_document":
            require_project_content_access(
                request,
                run.project,
                module=PROJECT_CONTENT_MODULE,
                action="generate" if write else None,
            )
        elif run.schema_version == "2" and run.fat_phase_id:
            require_acceptance_generation_access(request, run.project)
        else:
            require_project_content_access(
                request,
                run.project,
                module=ACCEPTANCE_TESTS_MODULE,
                action="generate" if write else None,
            )
    except PermissionDenied:
        return False
    return True


def list_project_ai_jobs(user, project_id, active_only=False, request=None):
    if not accessible_projects(user).filter(pk=project_id).exists():
        return []
    jobs = []
    for adapter in ADAPTERS:
        runs = adapter.model.objects.filter(project_id=project_id).select_related("project")
        if request is not None:
            runs = [run for run in runs if has_ai_job_access(request, adapter, run)]
        adapted = [adapt_ai_run(run) for run in runs]
        if active_only:
            active = [job for job in adapted if job["state"] in ACTIVE_STATES]
            if active:
                jobs.append(max(active, key=lambda job: (job["created_at"], job["source_id"])))
        else:
            jobs.extend(adapted)
    return sorted(jobs, key=lambda job: (job["created_at"], job["id"]), reverse=True)


def get_project_ai_job(user, job_id, request=None, *, write=False):
    try:
        kind, source_id = job_id.split(":", 1)
        adapter = ADAPTER_BY_KIND[kind]
    except (KeyError, ValueError):
        return None
    run = (
        adapter.model.objects.filter(
            project__in=accessible_projects(user),
            pk=source_id,
        )
        .select_related("project")
        .first()
    )
    if not run or (
        request is not None
        and not has_ai_job_access(request, adapter, run, write=write)
    ):
        return None
    return adapt_ai_run(run)


def find_active_replacement(run):
    adapter = ADAPTER_BY_MODEL[type(run)]
    candidates = adapter.model.objects.filter(project=run.project).exclude(pk=run.pk)
    for candidate in candidates:
        if normalize_state(candidate.status) in ACTIVE_STATES:
            return candidate
    return None


def create_replacement_ai_run(adapter, run, user, request):
    replacement = adapter.retry_handler(run, user, request)
    replacement.refresh_from_db()
    return replacement
