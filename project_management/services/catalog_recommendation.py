import hashlib
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from project_management.models import (
    CatalogRecommendationRun,
    CompanySolutionSpaceItem,
    DocumentContentSegment,
    Project,
    ProjectMatrixConfig,
    SolutionDemand,
)
from project_management.permissions import (
    CompanySolutionSpacePermission,
    ProjectSolutionSpacePermission,
    SolutionDemandPermission,
)
from project_management.services.ai_extraction_client import post_extraction
from project_management.services.product_assembly_extraction import (
    eligible_customer_demand_documents,
)


logger = logging.getLogger(__name__)


def require_catalog_access(request, project, *, write=False, run=None):
    if not settings.PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED:
        raise PermissionDenied("Catalog recommendations are disabled.")
    user = request.user
    context = {"project": project}
    catalog = ProjectSolutionSpacePermission()
    allowed = (
        catalog._check_create_permission(user, context)
        if write
        else catalog._check_view_permission_project(user, None, context)
    )
    if (
        not allowed
        or user.is_freelancer
        or user.company_id != project.company_profile_id
        or not CompanySolutionSpacePermission()._check_view_permission(user)
        or not SolutionDemandPermission()._check_view_permission_project(
            user, None, context
        )
    ):
        raise PermissionDenied()
    if run:
        visible = set(
            eligible_customer_demand_documents(project, request=request).values_list(
                "id", flat=True
            )
        )
        if not set(run.input_snapshot.get("document_ids", [])).issubset(visible):
            raise PermissionDenied("Source document access changed.")


def snapshot(project, request, language="en"):
    demands = list(
        SolutionDemand.objects.filter(project=project)
        .order_by("id")
        .values(
            "id",
            "parent_id",
            "subject",
            "demand_number",
            "custom_fields",
            "status",
        )
    )
    if not demands:
        raise ValidationError(
            {
                "code": "no_demands",
                "detail": "Create and review Customer Demands first.",
            }
        )
    catalog = list(
        CompanySolutionSpaceItem.objects.filter(
            company_profile_id=project.company_profile_id,
            assembly_type__in=["components", "production_lines"],
        )
        .order_by("id")
        .values(
            "id",
            "parent_id",
            "assembly_name",
            "assembly_type",
            "required_skills",
            "provided_skills",
            "custom_fields",
            "updated_at",
        )
    )
    if not catalog:
        raise ValidationError(
            {
                "code": "empty_catalog",
                "detail": "No classified company assemblies are available. Use the company catalog to add approved templates first.",
            }
        )
    documents = list(eligible_customer_demand_documents(project, request=request))
    sources = [
        {
            "segment_id": row.id,
            "document_id": row.document_id,
            "location": row.location_label,
            "text": row.text,
        }
        for row in DocumentContentSegment.objects.filter(
            index_id__in=[doc.search_index.id for doc in documents],
        ).order_by("document_id", "sequence", "id")
    ]
    config = ProjectMatrixConfig.objects.filter(project=project).first()
    payload = {
        "schema_version": "1",
        "project": {
            "id": project.pk,
            "name": project.project_name,
            "language": language,
        },
        "demands": demands,
        "columns": config.demand_columns if config else [],
        "catalog": catalog,
        "sources": sources,
    }
    # Bound the complete input; never silently recommend from an arbitrary first page.
    encoded = json.dumps(payload, sort_keys=True, default=str)
    if max(len(catalog), len(demands), len(sources)) > 1000 or len(encoded) > 750000:
        raise ValidationError(
            {
                "code": "input_too_large",
                "detail": "The complete input exceeds the recommendation limit. Select assemblies manually or reduce the project input.",
            }
        )
    snapshot_data = {
        "payload": json.loads(encoded),
        "document_ids": [doc.id for doc in documents],
    }
    return hashlib.sha256(encoded.encode()).hexdigest(), snapshot_data


@transaction.atomic
def create_catalog_run(project, user, request, language="en"):
    project = Project.objects.select_for_update().get(pk=project.pk)
    require_catalog_access(request, project, write=True)
    input_hash, data = snapshot(project, request, language)
    # Recover abandoned workers without hiding a pending run indefinitely.
    CatalogRecommendationRun.objects.filter(
        project=project,
        status__in=["queued", "running"],
        updated_at__lt=timezone.now() - timedelta(minutes=15),
    ).update(
        status="failed",
        error_code="worker_timeout",
        error_message="The recommendation job timed out. Generate recommendations again.",
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )
    existing = CatalogRecommendationRun.objects.filter(
        project=project,
        input_hash=input_hash,
        status__in=["queued", "running", "review_ready"],
    ).first()
    if existing:
        require_catalog_access(request, project, run=existing)
        return existing
    return CatalogRecommendationRun.objects.create(
        project=project,
        created_by=user,
        project_language=language,
        input_hash=input_hash,
        input_snapshot=data,
    )


def dispatch_catalog_run(run_id):
    if settings.PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY:
        process_catalog_run(run_id)
    else:
        from project_management.tasks import process_catalog_recommendation_run

        try:
            process_catalog_recommendation_run.apply_async(
                args=[run_id], queue=settings.PRODUCT_ASSEMBLY_EXTRACTION_QUEUE
            )
        except Exception:
            logger.exception("Unable to dispatch catalog recommendation run %s", run_id)
            CatalogRecommendationRun.objects.filter(pk=run_id, status="queued").update(
                status="failed",
                error_code="queue_unavailable",
                error_message="The recommendation service is temporarily unavailable. Please retry.",
                completed_at=timezone.now(),
                updated_at=timezone.now(),
            )


@transaction.atomic
def confirm_catalog_run(run, request, selected_ids):
    # Same lock order as manual selection, followed by the proposal lock.
    project = Project.objects.select_for_update().get(pk=run.project_id)
    run = CatalogRecommendationRun.objects.select_for_update().get(pk=run.pk)
    require_catalog_access(request, project, write=True, run=run)
    if run.status == "applied":
        if sorted(run.selected_ids) != sorted(selected_ids):
            raise ValidationError(
                "This proposal was already confirmed with different selections."
            )
        return run
    if run.status != "review_ready":
        raise ValidationError("Only a review-ready proposal can be confirmed.")
    current_hash, _ = snapshot(project, request, run.project_language)
    if current_hash != run.input_hash:
        run.status = "stale"
        run.error_code = "inputs_changed"
        run.error_message = "Demands, source documents or company assemblies changed. Generate recommendations again before confirming."
        run.save(update_fields=["status", "error_code", "error_message", "updated_at"])
        return run
    recommendations = {row["catalog_id"]: row for row in run.result["recommendations"]}
    if (
        not selected_ids
        or len(selected_ids) != len(set(selected_ids))
        or not set(selected_ids).issubset(recommendations)
    ):
        raise ValidationError(
            "Select one or more proposed assemblies, without duplicates."
        )
    if any(recommendations[pk]["conflicts"] for pk in selected_ids):
        raise ValidationError(
            "Resolve conflicting assemblies before selecting them. Manual catalog selection remains available."
        )
    from project_management.services.catalog_selection import select_company_assembly
    from aas_integration.services import mark_project_assembly_out_of_date

    candidates = CompanySolutionSpaceItem.objects.filter(
        pk__in=selected_ids, company_profile_id=project.company_profile_id
    ).order_by("level", "sort_order", "id")
    if candidates.count() != len(selected_ids):
        raise ValidationError("A selected company assembly is no longer available.")
    for source in candidates:
        copied, _ = select_company_assembly(
            source, project, request.user, refresh_existing=False
        )
        # Match manual selection: an existing root may have gained descendants.
        mark_project_assembly_out_of_date(copied, request.user.pk)
    run.status = "applied"
    run.selected_ids = selected_ids
    run.confirmed_by = request.user
    run.applied_at = timezone.now()
    run.save(
        update_fields=[
            "status",
            "selected_ids",
            "confirmed_by",
            "applied_at",
            "updated_at",
        ]
    )
    return run


def _text(value):
    if isinstance(value, dict):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value) if value is not None else ""


def validate_result(payload, answer):
    if not isinstance(answer, dict) or not isinstance(
        answer.get("recommendations"), list
    ):
        raise ValueError("Invalid recommendation response")
    catalog = {row["id"]: row for row in payload["catalog"]}
    sources = {
        **{("demand", row["id"]): row for row in payload["demands"]},
        **{("document_segment", row["segment_id"]): row for row in payload["sources"]},
    }
    rows, seen = [], set()
    if len(answer["recommendations"]) > 100:
        raise ValueError("Too many recommendations")
    for row in answer["recommendations"]:
        candidate = row.get("catalog_id")
        if type(candidate) is not int or candidate not in catalog or candidate in seen:
            raise ValueError("Invalid catalog candidate")
        seen.add(candidate)
        for key in ("gaps", "conflicts"):
            if not isinstance(row.get(key), list) or any(
                not isinstance(value, str) for value in row[key]
            ):
                raise ValueError("Invalid review details")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Missing explanation")
        for key in ("evidence", "catalog_evidence"):
            if not isinstance(row.get(key), list) or not row[key]:
                raise ValueError("Missing evidence")
            for evidence in row[key]:
                if key == "evidence":
                    source = sources.get(
                        (evidence.get("source_type"), evidence.get("source_id"))
                    )
                else:
                    source = catalog.get(evidence.get("source_id"))
                    ancestor, visited = source, set()
                    while (
                        ancestor
                        and ancestor["id"] != candidate
                        and ancestor["id"] not in visited
                    ):
                        visited.add(ancestor["id"])
                        ancestor = catalog.get(ancestor.get("parent_id"))
                    if not ancestor or ancestor["id"] != candidate:
                        raise ValueError("Unrelated catalog evidence")
                quote = " ".join(str(evidence.get("quote") or "").split())
                if (
                    not source
                    or not quote
                    or quote not in " ".join(_text(source).split())
                ):
                    raise ValueError("Ungrounded evidence")
        rows.append(
            {
                **row,
                "assembly_name": catalog[candidate]["assembly_name"],
                "assembly_type": catalog[candidate]["assembly_type"],
                "catalog_revision": str(catalog[candidate].get("updated_at") or ""),
                # Evidence-backed prose is not a verified engineering comparison.
                "review_category": (
                    "incompatible" if row["conflicts"] else "needs_engineering_review"
                ),
            }
        )
    unresolved = answer.get("unresolved_items", [])
    if not isinstance(unresolved, list) or any(
        not isinstance(value, str) for value in unresolved
    ):
        raise ValueError("Invalid unresolved items")
    return {"recommendations": rows, "unresolved_items": unresolved}


def process_catalog_run(run_id):
    if not CatalogRecommendationRun.objects.filter(pk=run_id, status="queued").update(
        status="running", started_at=timezone.now(), updated_at=timezone.now()
    ):
        return
    run = CatalogRecommendationRun.objects.get(pk=run_id)
    try:
        response = post_extraction(
            f"{settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL.rstrip('/')}/v1/catalog/recommend",
            headers={
                settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER: settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY
            },
            json=run.input_snapshot["payload"],
            timeout=settings.PRODUCT_ASSEMBLY_EXTRACTION_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        result = validate_result(run.input_snapshot["payload"], response.json())
    except Exception as exc:
        logger.warning(
            "Catalog recommendation failed: run=%s type=%s", run_id, type(exc).__name__
        )
        CatalogRecommendationRun.objects.filter(pk=run_id, status="running").update(
            status="failed",
            error_code="recommendation_failed",
            error_message="Catalog recommendations could not be generated. Try again or select assemblies manually.",
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return
    CatalogRecommendationRun.objects.filter(pk=run_id, status="running").update(
        status="review_ready",
        result=result,
        completed_at=timezone.now(),
        updated_at=timezone.now(),
    )
