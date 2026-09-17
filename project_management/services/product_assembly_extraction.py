import hashlib
import json
import logging
import unicodedata
from collections import defaultdict

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from project_management.services.ai_extraction_client import post_extraction
from project_management.models import (
    CompanySolutionSpaceItem,
    Document,
    DocumentContentSegment,
    DocumentSearchIndex,
    ProductAssemblyAppliedRow,
    ProductAssemblyExtractionRun,
    ProductAssemblyExtractionSource,
    Project,
    SolutionDemand,
)
from project_management.models.constants import CUSTOMER_DEMAND_DOCUMENT_TYPE
from project_management.services.document_access import (
    get_accessible_document_queryset,
)


EXTRACTION_SCHEMA_VERSION = "1"
READY_INDEX_STATUSES = {
    DocumentSearchIndex.Status.SEARCHABLE,
    DocumentSearchIndex.Status.PARTIAL,
}
CUSTOMER_DEMAND_DOCUMENT_LABEL = CUSTOMER_DEMAND_DOCUMENT_TYPE.split(":", 1)[0]
logger = logging.getLogger(__name__)


def _customer_demand_document_type_filter():
    return Q(document_type__iexact=CUSTOMER_DEMAND_DOCUMENT_TYPE) | Q(
        document_type__iexact=CUSTOMER_DEMAND_DOCUMENT_LABEL
    )


class ProductAssemblyExtractionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _project_document_queryset(project, request=None):
    if request is not None:
        return get_accessible_document_queryset(
            request,
            project_id=project.id,
        ).filter(folder__project=project)
    return Document.objects.filter(folder__project=project)


def eligible_customer_demand_documents(project, document_ids=None, request=None):
    queryset = (
        _project_document_queryset(project, request)
        .filter(
            _customer_demand_document_type_filter(),
            folder__project=project,
            search_index__status__in=READY_INDEX_STATUSES,
        )
        .select_related("search_index")
    )
    if document_ids is not None:
        queryset = queryset.filter(id__in=document_ids)
    return queryset.distinct().order_by("id")


def build_source_snapshot(documents):
    source_rows = [
        {
            "document_id": document.id,
            "content_hash": document.search_index.content_hash,
            "parser_version": document.search_index.parser_version,
        }
        for document in documents
    ]
    digest = hashlib.sha256(
        json.dumps(source_rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, source_rows


def _company_catalog_context(project):
    rows = (
        CompanySolutionSpaceItem.objects.filter(
            company_profile_id=project.company_profile_id,
        )
        .order_by("id")
        .values("id", "parent_id", "assembly_name", "assembly_type")
    )
    return [
        {
            "id": row["id"],
            "parent_id": row["parent_id"],
            "name": row["assembly_name"],
            "assembly_type": (
                CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
                if row["assembly_type"]
                == CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
                else CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
            ),
        }
        for row in rows
    ]


def _product_extraction_snapshot(project, documents):
    _, source_rows = build_source_snapshot(documents)
    catalog_rows = _company_catalog_context(project)
    snapshot = {
        "documents": source_rows,
        "company_assembly_catalog": catalog_rows,
    }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, catalog_rows


def _normalized_assembly_identity(value):
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _company_process_assemblies_by_identity(project):
    return {
        _normalized_assembly_identity(row["name"]): row["name"]
        for row in _company_catalog_context(project)
        if row["assembly_type"]
        == CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
    }


@transaction.atomic
def create_extraction_run(
    project,
    user,
    document_ids=None,
    project_language="en",
    request=None,
):
    project = Project.objects.select_for_update().get(pk=project.pk)
    existing_runs = ProductAssemblyExtractionRun.objects.filter(project=project)
    hint_only = existing_runs.filter(
        status=ProductAssemblyExtractionRun.Status.APPLIED,
    ).exists()
    if existing_runs.filter(
        Q(
            status__in=[
                ProductAssemblyExtractionRun.Status.QUEUED,
                ProductAssemblyExtractionRun.Status.EXTRACTING,
                ProductAssemblyExtractionRun.Status.APPLYING,
            ]
        )
        | Q(
            status=ProductAssemblyExtractionRun.Status.REVIEW_READY,
            hint_only=False,
        )
    ).exists():
        raise ProductAssemblyExtractionError(
            "extraction_already_in_progress",
            "A Customer Demand generation is already in progress for this project.",
        )

    documents = list(
        eligible_customer_demand_documents(
            project,
            document_ids,
            request=request,
        )
    )
    if not documents:
        pending_count = (
            _project_document_queryset(project, request)
            .filter(_customer_demand_document_type_filter())
            .filter(
                Q(search_index__isnull=True)
                | Q(
                    search_index__status__in=[
                        DocumentSearchIndex.Status.PENDING,
                        DocumentSearchIndex.Status.PROCESSING,
                    ]
                )
            )
            .count()
        )
        if pending_count:
            raise ProductAssemblyExtractionError(
                "source_documents_not_ready",
                "Customer Demand documents are still being indexed. Try again after indexing completes.",
            )
        raise ProductAssemblyExtractionError(
            "no_eligible_source_documents",
            "No searchable documents marked as Customer Demand are available in this project.",
        )

    snapshot_hash, _ = _product_extraction_snapshot(project, documents)
    run = ProductAssemblyExtractionRun.objects.create(
        project=project,
        status=ProductAssemblyExtractionRun.Status.QUEUED,
        schema_version=EXTRACTION_SCHEMA_VERSION,
        project_language=(project_language or "en")[:16],
        source_snapshot_hash=snapshot_hash,
        source_selection_mode=(
            ProductAssemblyExtractionRun.SourceSelectionMode.EXPLICIT
            if document_ids is not None
            else ProductAssemblyExtractionRun.SourceSelectionMode.ALL_CURRENT
        ),
        hint_only=hint_only,
        created_by=user,
        modified_by=user,
    )
    ProductAssemblyExtractionSource.objects.bulk_create(
        [
            ProductAssemblyExtractionSource(
                run=run,
                document=document,
                search_index=document.search_index,
                document_id_snapshot=document.id,
                search_index_id_snapshot=document.search_index.id,
                document_name=document.file_name or "",
                content_hash=document.search_index.content_hash,
                parser_version=document.search_index.parser_version,
                segment_count=document.content_segments.count(),
            )
            for document in documents
        ]
    )
    return run


def dispatch_extraction_run(run_id):
    if settings.PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY:
        process_extraction_run(run_id)
        return

    from project_management.tasks import process_product_assembly_extraction_run

    process_product_assembly_extraction_run.apply_async(
        args=[run_id],
        queue=settings.PRODUCT_ASSEMBLY_EXTRACTION_QUEUE,
    )


def _source_payload(run):
    source_index_ids = run.sources.filter(search_index_id__isnull=False).values_list(
        "search_index_id",
        flat=True,
    )
    segments = (
        DocumentContentSegment.objects.filter(index_id__in=source_index_ids)
        .select_related("document")
        .order_by("document_id", "sequence", "id")
    )
    max_segments = settings.PRODUCT_ASSEMBLY_EXTRACTION_MAX_SEGMENTS
    max_characters = settings.PRODUCT_ASSEMBLY_EXTRACTION_MAX_CHARACTERS
    payload = []
    total_characters = 0

    for segment in segments.iterator(chunk_size=100):
        if len(payload) >= max_segments or total_characters >= max_characters:
            break
        remaining = max_characters - total_characters
        text = segment.text[:remaining]
        if not text:
            continue
        payload.append(
            {
                "document_id": segment.document_id,
                "document_name": segment.document.file_name,
                "segment_id": segment.id,
                "segment_type": segment.segment_type,
                "sequence": segment.sequence,
                "location": segment.location_label,
                "text": text,
            }
        )
        total_characters += len(text)

    if not payload:
        raise ProductAssemblyExtractionError(
            "source_content_empty",
            "The selected Customer Demand documents do not contain searchable text.",
        )
    return payload


def _validate_service_result(result, run):
    if not isinstance(result, dict):
        raise ProductAssemblyExtractionError(
            "invalid_service_response",
            "The extraction service returned an invalid response.",
        )
    if str(result.get("schema_version")) != EXTRACTION_SCHEMA_VERSION:
        raise ProductAssemblyExtractionError(
            "schema_version_mismatch",
            "The extraction service response schema is incompatible.",
        )
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ProductAssemblyExtractionError(
            "invalid_service_response",
            "The extraction service returned an invalid product assembly list.",
        )

    process_assemblies = _company_process_assemblies_by_identity(run.project)

    source_document_ids = set(
        run.sources.values_list("document_id_snapshot", flat=True)
    )
    source_index_ids = set(
        run.sources.filter(search_index_id__isnull=False).values_list(
            "search_index_id",
            flat=True,
        )
    )
    source_segments = DocumentContentSegment.objects.filter(
        index_id__in=source_index_ids
    )
    source_segment_text = {
        (document_id, segment_id): text
        for document_id, segment_id, text in source_segments.values_list(
            "document_id",
            "id",
            "text",
        )
    }
    seen_keys = set()
    parent_by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ProductAssemblyExtractionError("invalid_row", "Every extracted row must be an object.")
        key = str(row.get("proposal_key") or "").strip()
        name = str(row.get("name") or "").strip()
        if not key or len(key) > 128 or key in seen_keys:
            raise ProductAssemblyExtractionError(
                "invalid_proposal_key",
                "Extracted rows must have unique proposal keys.",
            )
        if not name or len(name) > 255:
            raise ProductAssemblyExtractionError(
                "invalid_assembly_name",
                "Every extracted row must have a valid product assembly name.",
            )
        process_name = process_assemblies.get(
            _normalized_assembly_identity(name),
        )
        if process_name:
            raise ProductAssemblyExtractionError(
                "assembly_classification_conflict",
                (
                    f"'{name}' is classified as the company Process Assembly "
                    f"'{process_name}' and cannot be added to Demand Management."
                ),
            )
        seen_keys.add(key)
        parent_by_key[key] = row.get("parent_key")
        evidence = row.get("evidence") or []
        if not evidence:
            raise ProductAssemblyExtractionError(
                "missing_evidence",
                f"Product assembly '{name}' has no source evidence.",
            )
        for item in evidence:
            evidence_key = (item.get("document_id"), item.get("segment_id"))
            segment_text = source_segment_text.get(evidence_key)
            if item.get("document_id") not in source_document_ids or segment_text is None:
                raise ProductAssemblyExtractionError(
                    "invalid_evidence",
                    "The extraction service referenced evidence outside the selected source documents.",
                )
            normalized_quote = " ".join(str(item.get("quote") or "").split())
            normalized_segment = " ".join(segment_text.split())
            if not normalized_quote or normalized_quote not in normalized_segment:
                raise ProductAssemblyExtractionError(
                    "invalid_evidence_quote",
                    "The extraction service returned a quote that is not present in its source segment.",
                )

    for key, parent_key in parent_by_key.items():
        if parent_key and parent_key not in seen_keys:
            raise ProductAssemblyExtractionError(
                "invalid_parent",
                f"Product assembly '{key}' references an unknown parent.",
            )

    def depth(key, path=None):
        path = set(path or ())
        if key in path:
            raise ProductAssemblyExtractionError(
                "hierarchy_cycle",
                "The proposed product assembly hierarchy contains a cycle.",
            )
        parent_key = parent_by_key.get(key)
        if not parent_key:
            return 1
        return 1 + depth(parent_key, path | {key})

    if seen_keys and max(depth(key) for key in seen_keys) > SolutionDemand.MAX_LEVEL:
        raise ProductAssemblyExtractionError(
            "hierarchy_too_deep",
            f"The proposed hierarchy exceeds {SolutionDemand.MAX_LEVEL} levels.",
        )
    return result


def process_extraction_run(run_id):
    run = ProductAssemblyExtractionRun.objects.select_related("project").get(id=run_id)
    if run.status not in {
        ProductAssemblyExtractionRun.Status.QUEUED,
        ProductAssemblyExtractionRun.Status.FAILED,
    }:
        return

    run.status = ProductAssemblyExtractionRun.Status.EXTRACTING
    run.started_at = timezone.now()
    run.error_code = ""
    run.error_message = ""
    run.save(
        update_fields=[
            "status",
            "started_at",
            "error_code",
            "error_message",
            "updated_at",
        ]
    )

    try:
        payload = {
            "schema_version": EXTRACTION_SCHEMA_VERSION,
            "project": {
                "id": run.project_id,
                "name": run.project.project_name,
                "language": run.project_language,
            },
            "sources": _source_payload(run),
            "company_assembly_catalog": _company_catalog_context(run.project),
        }
        response = post_extraction(
            f"{settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL.rstrip('/')}/v1/product-assemblies/extract",
            headers={
                settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER:
                    settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=settings.PRODUCT_ASSEMBLY_EXTRACTION_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail")
            except Exception:
                detail = response.text
            raise ProductAssemblyExtractionError(
                "extraction_service_failed",
                detail or "The extraction service could not process the source documents.",
            )
        raw_result = _validate_service_result(response.json(), run)
        result = raw_result
        if run.hint_only:
            applied_payloads = list(
                ProductAssemblyAppliedRow.objects.filter(
                    run__project=run.project,
                    run__status=ProductAssemblyExtractionRun.Status.APPLIED,
                ).values_list("applied_payload", flat=True)
            )
            result = {
                **raw_result,
                "rows": _annotate_hint_rows(
                    raw_result["rows"],
                    applied_payloads,
                ),
            }
        run.refresh_from_db(fields=["status"])
        if run.status == ProductAssemblyExtractionRun.Status.CANCELLED:
            return
        run.raw_result = raw_result
        run.result = result
        run.warnings = result.get("warnings") or []
        run.status = ProductAssemblyExtractionRun.Status.REVIEW_READY
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "result",
                "raw_result",
                "warnings",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    except ProductAssemblyExtractionError as exc:
        run.refresh_from_db(fields=["status"])
        if run.status == ProductAssemblyExtractionRun.Status.CANCELLED:
            return
        run.status = ProductAssemblyExtractionRun.Status.FAILED
        run.error_code = exc.code
        run.error_message = exc.message
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )
    except (requests.RequestException, ValueError) as exc:
        run.refresh_from_db(fields=["status"])
        if run.status == ProductAssemblyExtractionRun.Status.CANCELLED:
            return
        run.status = ProductAssemblyExtractionRun.Status.FAILED
        run.error_code = (
            "extraction_service_timeout" if isinstance(exc, requests.Timeout)
            else "extraction_service_unavailable"
        )
        run.error_message = (
            "The AI extraction timed out after automatic retry. Your data is unchanged. Please retry extraction."
            if isinstance(exc, requests.Timeout)
            else "The extraction service is temporarily unavailable."
        )
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )
        return
    except Exception:
        logger.exception(
            "Unexpected product assembly extraction failure",
            extra={"run_id": run.id, "project_id": run.project_id},
        )
        run.refresh_from_db(fields=["status"])
        if run.status == ProductAssemblyExtractionRun.Status.CANCELLED:
            return
        run.status = ProductAssemblyExtractionRun.Status.FAILED
        run.error_code = "unexpected_extraction_failure"
        run.error_message = "The extraction could not be completed. Please retry."
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "completed_at",
                "updated_at",
            ]
        )


def _current_snapshot_hash(run, request=None):
    explicit_document_ids = None
    if (
        run.source_selection_mode
        == ProductAssemblyExtractionRun.SourceSelectionMode.EXPLICIT
    ):
        explicit_document_ids = list(
            run.sources.values_list("document_id_snapshot", flat=True)
        )
    documents = list(
        eligible_customer_demand_documents(
            run.project,
            explicit_document_ids,
            request=request,
        )
    )
    if explicit_document_ids is not None and len(documents) != len(
        explicit_document_ids
    ):
        return None
    snapshot_hash, _ = _product_extraction_snapshot(run.project, documents)
    return snapshot_hash


def source_documents_changed(run, request=None):
    return _current_snapshot_hash(run, request=request) != run.source_snapshot_hash


@transaction.atomic
def retry_extraction_run(run, user, request=None):
    run_id = getattr(run, "pk", None) or run.id
    run = ProductAssemblyExtractionRun.objects.select_for_update().get(pk=run_id)
    Project.objects.select_for_update().get(pk=run.project_id)
    retryable_statuses = {
        ProductAssemblyExtractionRun.Status.FAILED,
        ProductAssemblyExtractionRun.Status.STALE,
    }
    if run.status == ProductAssemblyExtractionRun.Status.REVIEW_READY:
        if not source_documents_changed(run, request=request):
            raise ProductAssemblyExtractionError(
                "source_documents_unchanged",
                "The Customer Demand source documents have not changed.",
            )
        run.status = ProductAssemblyExtractionRun.Status.STALE
        run.error_code = "source_documents_changed"
        run.error_message = (
            "The source documents or company catalog changed after extraction. "
            "A replacement extraction was started with the current documents."
        )
        run.save(
            update_fields=[
                "status",
                "error_code",
                "error_message",
                "updated_at",
            ]
        )
    elif run.status not in retryable_statuses:
        raise ProductAssemblyExtractionError(
            "run_not_retryable",
            "Only failed, stale, or changed review-ready extractions can be retried.",
        )
    return create_extraction_run(
        project=run.project,
        user=user,
        project_language=run.project_language,
        request=request,
    )


def _normalized_text(value):
    return " ".join(str(value or "").casefold().split())


def _normalized_demand_numbers(row):
    return tuple(
        sorted(
            _normalized_text(value)
            for value in (row.get("demand_numbers") or [])
            if _normalized_text(value)
        )
    )


def _hint_match_score(row, applied_payload):
    score = 0
    proposal_key = _normalized_text(row.get("proposal_key"))
    applied_key = _normalized_text(applied_payload.get("proposal_key"))
    if proposal_key and proposal_key == applied_key:
        score += 4

    demand_numbers = _normalized_demand_numbers(row)
    applied_demand_numbers = _normalized_demand_numbers(applied_payload)
    if demand_numbers and demand_numbers == applied_demand_numbers:
        score += 8

    name = _normalized_text(row.get("name"))
    applied_name = _normalized_text(applied_payload.get("name"))
    if name and name == applied_name:
        score += 2
    return score


def _annotate_hint_rows(rows, applied_payloads):
    unmatched_payloads = list(applied_payloads)
    annotated_rows = []
    for row in rows:
        matches = [
            (_hint_match_score(row, payload), index)
            for index, payload in enumerate(unmatched_payloads)
        ]
        score, matched_index = max(matches, default=(0, -1))
        if score:
            unmatched_payloads.pop(matched_index)
        annotated_rows.append({**row, "is_new_assembly": score == 0})
    return annotated_rows


def apply_extraction_run(run, user, rows=None, request=None):
    stale_error = None
    applied_run = None
    with transaction.atomic():
        run = ProductAssemblyExtractionRun.objects.select_for_update().get(id=run.id)
        Project.objects.select_for_update().get(pk=run.project_id)
        if run.status == ProductAssemblyExtractionRun.Status.APPLIED:
            return run
        if run.hint_only:
            raise ProductAssemblyExtractionError(
                "hint_only_run",
                "Later product assembly extractions are suggestions only and cannot create or change Customer Demands.",
            )
        if run.status != ProductAssemblyExtractionRun.Status.REVIEW_READY:
            raise ProductAssemblyExtractionError(
                "run_not_review_ready",
                "Only an extraction that is ready for review can be applied.",
            )
        if ProductAssemblyExtractionRun.objects.filter(
            project=run.project,
            status=ProductAssemblyExtractionRun.Status.APPLIED,
        ).exclude(pk=run.pk).exists():
            raise ProductAssemblyExtractionError(
                "extraction_already_applied",
                "Customer Demands have already been generated for this project. This operation can only be completed once.",
            )
        if source_documents_changed(run, request=request):
            run.status = ProductAssemblyExtractionRun.Status.STALE
            run.error_code = "source_documents_changed"
            run.error_message = (
                "The source documents or company catalog changed after extraction. "
                "Run the extraction again."
            )
            run.save(
                update_fields=["status", "error_code", "error_message", "updated_at"]
            )
            stale_error = ProductAssemblyExtractionError(
                run.error_code,
                run.error_message,
            )
        else:
            applied_run = _apply_extraction_run_locked(run, user, rows)

    if stale_error:
        raise stale_error
    return applied_run


def _apply_extraction_run_locked(run, user, rows=None):
    result = {**run.result, "rows": rows if rows is not None else run.result.get("rows", [])}
    result = _validate_service_result(result, run)
    if not result["rows"]:
        raise ProductAssemblyExtractionError(
            "no_product_assemblies_to_apply",
            "No product or part assemblies are available to apply.",
        )
    run.status = ProductAssemblyExtractionRun.Status.APPLYING
    run.modified_by = user
    run.result = result
    run.save(update_fields=["status", "modified_by", "result", "updated_at"])

    rows_by_key = {row["proposal_key"]: row for row in result["rows"]}
    children_by_parent = defaultdict(list)
    for row in result["rows"]:
        children_by_parent[row.get("parent_key")].append(row)

    created_by_key = {}

    def create_subtree(parent_key=None, parent_item=None, level=1):
        existing_max = (
            SolutionDemand.objects.filter(
                project=run.project,
                parent=parent_item,
            ).aggregate(
                max_order=Max("sort_order")
            )["max_order"]
            or 0
        )
        for offset, row in enumerate(children_by_parent.get(parent_key, []), start=1):
            item = SolutionDemand.objects.create(
                project=run.project,
                parent=parent_item,
                level=level,
                sort_order=existing_max + offset,
                subject=str(row["name"]).strip(),
                demand_number=", ".join(row.get("demand_numbers") or []) or None,
                custom_fields={
                    "product_assembly_extraction": {
                        "run_id": run.id,
                        "proposal_key": row["proposal_key"],
                        "confidence": row.get("confidence"),
                        "demand_numbers": row.get("demand_numbers") or [],
                    }
                },
                created_by=user,
                modified_by=user,
            )
            evidence_document_ids = {
                item.get("document_id")
                for item in row.get("evidence") or []
                if item.get("document_id")
            }
            if not evidence_document_ids:
                evidence_document_ids = set(run.sources.values_list("document_id", flat=True))
            item.documents.set(evidence_document_ids)
            ProductAssemblyAppliedRow.objects.create(
                run=run,
                proposal_key=row["proposal_key"],
                applied_payload=row,
                solution_demand=item,
            )
            created_by_key[row["proposal_key"]] = item
            create_subtree(row["proposal_key"], item, level + 1)

    create_subtree()
    if len(created_by_key) != len(rows_by_key):
        raise ProductAssemblyExtractionError(
            "unreachable_rows",
            "Some product assembly rows are not connected to a hierarchy root.",
        )

    run.status = ProductAssemblyExtractionRun.Status.APPLIED
    run.applied_at = timezone.now()
    run.save(update_fields=["status", "applied_at", "updated_at"])
    return run
