import hashlib
import json
import logging

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from project_management.services.ai_extraction_client import post_extraction
from project_management.models import (
    DocumentContentSegment,
    ProductAttributeAppliedColumn,
    ProductAttributeAppliedValue,
    ProductAttributeExtractionRun,
    ProductAttributeExtractionSource,
    Project,
    ProjectMatrixConfig,
    SolutionDemand,
)
from project_management.services.product_assembly_extraction import (
    _source_payload,
    build_source_snapshot,
    eligible_customer_demand_documents,
)


ATTRIBUTE_SCHEMA_VERSION = "1"
RESERVED_DEMAND_COLUMN_KEYS = {
    "sequence",
    "subject",
    "demand_number",
    "status",
}
logger = logging.getLogger(__name__)


class ProductAttributeExtractionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _matrix_payload(project):
    config = ProjectMatrixConfig.objects.filter(project=project).first()
    columns = list(config.demand_columns or []) if config else []
    demands = [
        {
            "id": demand.id,
            "parent_id": demand.parent_id,
            "level": demand.level,
            "sort_order": demand.sort_order,
            "subject": demand.subject,
            "demand_number": demand.demand_number,
            "custom_fields": demand.custom_fields,
            "updated_at": demand.updated_at.isoformat(),
            "hierarchy_number": demand.get_sequence(),
        }
        for demand in SolutionDemand.objects.filter(project=project).order_by(
            "level",
            "sort_order",
            "id",
        )
    ]
    return {"columns": columns, "demands": demands}


def build_matrix_snapshot(project):
    payload = _matrix_payload(project)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return digest, payload


@transaction.atomic
def create_attribute_extraction_run(
    project,
    user,
    document_ids=None,
    project_language="en",
    request=None,
):
    project = Project.objects.select_for_update().get(pk=project.pk)
    if not SolutionDemand.objects.filter(project=project).exists():
        raise ProductAttributeExtractionError(
            "no_customer_demands",
            "Create or generate Customer Demands before extracting product attributes.",
        )

    if ProductAttributeExtractionRun.objects.filter(
        project=project,
    ).filter(
        Q(
            status__in=[
                ProductAttributeExtractionRun.Status.QUEUED,
                ProductAttributeExtractionRun.Status.EXTRACTING,
                ProductAttributeExtractionRun.Status.APPLYING,
            ]
        )
        | Q(
            status=ProductAttributeExtractionRun.Status.REVIEW_READY,
            hint_only=False,
        )
    ).exists():
        raise ProductAttributeExtractionError(
            "attribute_extraction_already_in_progress",
            "A product attribute extraction is already in progress for this project.",
        )

    documents = list(
        eligible_customer_demand_documents(
            project,
            document_ids,
            request=request,
        )
    )
    if not documents:
        raise ProductAttributeExtractionError(
            "no_eligible_source_documents",
            "No searchable documents marked as Customer Demand are available in this project.",
        )

    source_snapshot_hash, _ = build_source_snapshot(documents)
    matrix_snapshot_hash, _ = build_matrix_snapshot(project)
    hint_only = ProductAttributeExtractionRun.objects.filter(
        project=project,
        status=ProductAttributeExtractionRun.Status.APPLIED,
    ).exists()
    run = ProductAttributeExtractionRun.objects.create(
        project=project,
        status=ProductAttributeExtractionRun.Status.QUEUED,
        schema_version=ATTRIBUTE_SCHEMA_VERSION,
        project_language=(project_language or "en")[:16],
        source_snapshot_hash=source_snapshot_hash,
        source_selection_mode=(
            ProductAttributeExtractionRun.SourceSelectionMode.EXPLICIT
            if document_ids is not None
            else ProductAttributeExtractionRun.SourceSelectionMode.ALL_CURRENT
        ),
        matrix_snapshot_hash=matrix_snapshot_hash,
        hint_only=hint_only,
        created_by=user,
        modified_by=user,
    )
    ProductAttributeExtractionSource.objects.bulk_create(
        [
            ProductAttributeExtractionSource(
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


def dispatch_attribute_extraction_run(run_id):
    if settings.PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY:
        process_attribute_extraction_run(run_id)
        return

    from project_management.tasks import process_product_attribute_extraction_run

    process_product_attribute_extraction_run.apply_async(
        args=[run_id],
        queue=settings.PRODUCT_ASSEMBLY_EXTRACTION_QUEUE,
    )


def _known_columns(project):
    config = ProjectMatrixConfig.objects.filter(project=project).first()
    return {
        str(column.get("key") or "").strip(): column
        for column in (config.demand_columns or [] if config else [])
        if str(column.get("key") or "").strip()
    }


def _validate_evidence(evidence, source_document_ids, source_segment_text):
    if not evidence:
        raise ProductAttributeExtractionError(
            "missing_evidence",
            "Every extracted product attribute and value requires source evidence.",
        )
    for item in evidence:
        evidence_key = (item.get("document_id"), item.get("segment_id"))
        segment_text = source_segment_text.get(evidence_key)
        if item.get("document_id") not in source_document_ids or segment_text is None:
            raise ProductAttributeExtractionError(
                "invalid_evidence",
                "The extraction service referenced evidence outside the selected source documents.",
            )
        normalized_quote = " ".join(str(item.get("quote") or "").split())
        normalized_segment = " ".join(segment_text.split())
        if not normalized_quote or normalized_quote not in normalized_segment:
            raise ProductAttributeExtractionError(
                "invalid_evidence_quote",
                "The extraction service returned a quote that is not present in its source segment.",
            )


def _generated_column_key(attribute):
    identity = "|".join(
        [
            str(attribute.get("label_en") or attribute.get("label") or "").strip().lower(),
            str(attribute.get("unit") or "").strip().lower(),
        ]
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    label_slug = slugify(identity.split("|", 1)[0]).replace("-", "_")[:60]
    return f"ai_attr_{label_slug or 'attribute'}_{suffix}"


def _validate_service_result(result, run):
    if not isinstance(result, dict):
        raise ProductAttributeExtractionError(
            "invalid_service_response",
            "The extraction service returned an invalid response.",
        )
    if str(result.get("schema_version")) != ATTRIBUTE_SCHEMA_VERSION:
        raise ProductAttributeExtractionError(
            "schema_version_mismatch",
            "The product attribute response schema is incompatible.",
        )
    attributes = result.get("attributes")
    if not isinstance(attributes, list):
        raise ProductAttributeExtractionError(
            "invalid_service_response",
            "The extraction service must return an attributes list.",
        )

    source_document_ids = set(
        run.sources.values_list("document_id_snapshot", flat=True)
    )
    source_index_ids = set(
        run.sources.filter(search_index_id__isnull=False).values_list(
            "search_index_id",
            flat=True,
        )
    )
    source_segment_text = {
        (document_id, segment_id): text
        for document_id, segment_id, text in DocumentContentSegment.objects.filter(
            index_id__in=source_index_ids
        ).values_list("document_id", "id", "text")
    }
    demand_ids = set(
        SolutionDemand.objects.filter(project=run.project).values_list("id", flat=True)
    )
    known_columns = _known_columns(run.project)
    seen_keys = set()
    seen_column_keys = set()
    demands_by_id = {
        demand.id: demand
        for demand in SolutionDemand.objects.filter(project=run.project).only(
            "id",
            "custom_fields",
        )
    }
    normalized_attributes = []

    for attribute in attributes:
        if not isinstance(attribute, dict):
            raise ProductAttributeExtractionError(
                "invalid_attribute",
                "Every extracted product attribute must be an object.",
            )
        proposal_key = str(attribute.get("proposal_key") or "").strip()
        label = str(attribute.get("label") or "").strip()
        if not proposal_key or len(proposal_key) > 128 or proposal_key in seen_keys:
            raise ProductAttributeExtractionError(
                "invalid_proposal_key",
                "Extracted attributes must have unique proposal keys.",
            )
        if not label or len(label) > 255:
            raise ProductAttributeExtractionError(
                "invalid_attribute_label",
                "Every extracted product attribute must have a valid label.",
            )
        seen_keys.add(proposal_key)
        _validate_evidence(
            attribute.get("evidence") or [],
            source_document_ids,
            source_segment_text,
        )

        existing_column_key = attribute.get("existing_column_key") or None
        if existing_column_key and existing_column_key not in known_columns:
            raise ProductAttributeExtractionError(
                "unknown_existing_column",
                "The extraction service referenced an unknown Customer Demand column.",
            )
        if existing_column_key in RESERVED_DEMAND_COLUMN_KEYS:
            raise ProductAttributeExtractionError(
                "reserved_column",
                "Product attributes cannot target structural Customer Demand columns.",
            )

        target_column_key = existing_column_key or _generated_column_key(attribute)
        if target_column_key in seen_column_keys:
            raise ProductAttributeExtractionError(
                "duplicate_target_column",
                "Multiple extracted attributes target the same Customer Demand column. Merge them during review.",
            )
        seen_column_keys.add(target_column_key)
        values = []
        seen_demand_ids = set()
        for value in attribute.get("values") or []:
            demand_id = value.get("solution_demand_id")
            text_value = str(value.get("value") or "").strip()
            if demand_id not in demand_ids:
                raise ProductAttributeExtractionError(
                    "invalid_solution_demand",
                    "The extraction service referenced a Customer Demand outside this project.",
                )
            if demand_id in seen_demand_ids or not text_value:
                raise ProductAttributeExtractionError(
                    "invalid_attribute_value",
                    "Each product attribute may contain one non-empty value per Customer Demand.",
                )
            seen_demand_ids.add(demand_id)
            _validate_evidence(
                value.get("evidence") or [],
                source_document_ids,
                source_segment_text,
            )
            current_value = demands_by_id[demand_id].custom_fields.get(
                target_column_key
            )
            values.append(
                {
                    **value,
                    "value": text_value,
                    "selected": value.get("selected", True),
                    "current_value": current_value,
                    "is_new_value": current_value in (None, ""),
                    "has_conflict": current_value not in (None, "", text_value),
                }
            )

        normalized_attributes.append(
            {
                **attribute,
                "existing_column_key": existing_column_key,
                "target_column_key": target_column_key,
                "create_column": not bool(existing_column_key),
                "selected": attribute.get("selected", True),
                "is_new_attribute": target_column_key not in known_columns,
                "values": values,
            }
        )

    return {**result, "attributes": normalized_attributes}


def process_attribute_extraction_run(run_id):
    run = ProductAttributeExtractionRun.objects.select_related("project").get(id=run_id)
    if run.status not in {
        ProductAttributeExtractionRun.Status.QUEUED,
        ProductAttributeExtractionRun.Status.FAILED,
    }:
        return
    run.status = ProductAttributeExtractionRun.Status.EXTRACTING
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
        _, matrix = build_matrix_snapshot(run.project)
        payload = {
            "schema_version": ATTRIBUTE_SCHEMA_VERSION,
            "project": {
                "id": run.project_id,
                "name": run.project.project_name,
                "language": run.project_language,
            },
            "sources": _source_payload(run),
            "customer_demands": [
                {
                    "id": demand["id"],
                    "parent_id": demand["parent_id"],
                    "hierarchy_number": demand["hierarchy_number"],
                    "name": demand["subject"],
                    "demand_number": demand["demand_number"],
                    "custom_fields": demand["custom_fields"],
                }
                for demand in matrix["demands"]
            ],
            "configured_columns": matrix["columns"],
        }
        response = post_extraction(
            f"{settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL.rstrip('/')}/v1/product-attributes/extract",
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
            raise ProductAttributeExtractionError(
                "extraction_service_failed",
                detail or "The extraction service could not process product attributes.",
            )
        raw_result = response.json()
        result = _validate_service_result(raw_result, run)
        run.refresh_from_db(fields=["status"])
        if run.status == ProductAttributeExtractionRun.Status.CANCELLED:
            return
        run.raw_result = raw_result
        run.result = result
        run.warnings = result.get("warnings") or []
        run.status = ProductAttributeExtractionRun.Status.REVIEW_READY
        run.completed_at = timezone.now()
        run.save(
            update_fields=[
                "raw_result",
                "result",
                "warnings",
                "status",
                "completed_at",
                "updated_at",
            ]
        )
    except ProductAttributeExtractionError as exc:
        _fail_run(run, exc.code, exc.message)
    except requests.Timeout:
        _fail_run(
            run, "extraction_service_timeout",
            "The AI extraction timed out after automatic retry. Your data is unchanged. Please retry extraction.",
        )
    except (requests.RequestException, ValueError):
        _fail_run(
            run,
            "extraction_service_unavailable",
            "The product attribute extraction service is temporarily unavailable.",
        )
    except Exception:
        logger.exception(
            "Unexpected product attribute extraction failure",
            extra={"run_id": run.id, "project_id": run.project_id},
        )
        _fail_run(
            run,
            "unexpected_extraction_failure",
            "Product attribute extraction could not be completed. Please retry.",
        )


def _fail_run(run, code, message):
    run.refresh_from_db(fields=["status"])
    if run.status == ProductAttributeExtractionRun.Status.CANCELLED:
        return
    run.status = ProductAttributeExtractionRun.Status.FAILED
    run.error_code = code
    run.error_message = message
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


def _current_source_snapshot_hash(run, request=None):
    explicit_document_ids = None
    if (
        run.source_selection_mode
        == ProductAttributeExtractionRun.SourceSelectionMode.EXPLICIT
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
    snapshot_hash, _ = build_source_snapshot(documents)
    return snapshot_hash


def source_documents_changed(run, request=None):
    return (
        _current_source_snapshot_hash(run, request=request)
        != run.source_snapshot_hash
    )


@transaction.atomic
def retry_attribute_extraction_run(run, user, request=None):
    run_id = getattr(run, "pk", None) or run.id
    run = ProductAttributeExtractionRun.objects.select_for_update().get(pk=run_id)
    Project.objects.select_for_update().get(pk=run.project_id)
    retryable_statuses = {
        ProductAttributeExtractionRun.Status.FAILED,
        ProductAttributeExtractionRun.Status.STALE,
    }
    if run.status == ProductAttributeExtractionRun.Status.REVIEW_READY:
        if not source_documents_changed(run, request=request):
            raise ProductAttributeExtractionError(
                "source_documents_unchanged",
                "The Customer Demand source documents have not changed.",
            )
        run.status = ProductAttributeExtractionRun.Status.STALE
        run.error_code = "extraction_inputs_changed"
        run.error_message = (
            "The source documents changed after extraction. "
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
        raise ProductAttributeExtractionError(
            "run_not_retryable",
            "Only failed, stale, or changed review-ready extractions can be retried.",
        )
    return create_attribute_extraction_run(
        project=run.project,
        user=user,
        project_language=run.project_language,
        request=request,
    )


def apply_attribute_extraction_run(run, user, attributes=None, request=None):
    stale_error = None
    applied_run = None
    with transaction.atomic():
        run = ProductAttributeExtractionRun.objects.select_for_update().get(id=run.id)
        Project.objects.select_for_update().get(pk=run.project_id)
        if run.status == ProductAttributeExtractionRun.Status.APPLIED:
            return run
        if run.hint_only:
            raise ProductAttributeExtractionError(
                "hint_only_run",
                "Later product attribute extractions are suggestions only and cannot change the Customer Demand matrix.",
            )
        if run.status != ProductAttributeExtractionRun.Status.REVIEW_READY:
            raise ProductAttributeExtractionError(
                "run_not_review_ready",
                "Only an extraction that is ready for review can be applied.",
            )
        source_changed = source_documents_changed(run, request=request)
        matrix_hash, _ = build_matrix_snapshot(run.project)
        matrix_changed = matrix_hash != run.matrix_snapshot_hash
        if source_changed or matrix_changed:
            run.status = ProductAttributeExtractionRun.Status.STALE
            run.error_code = "extraction_inputs_changed"
            run.error_message = (
                "Customer Demand documents or matrix values changed after extraction. "
                "Run product attribute extraction again."
            )
            run.save(
                update_fields=["status", "error_code", "error_message", "updated_at"]
            )
            stale_error = ProductAttributeExtractionError(
                run.error_code,
                run.error_message,
            )
        else:
            applied_run = _apply_attribute_extraction_run_locked(
                run,
                user,
                attributes,
            )
    if stale_error:
        raise stale_error
    return applied_run


def _apply_attribute_extraction_run_locked(run, user, attributes=None):
    result = {
        **run.result,
        "attributes": (
            attributes if attributes is not None else run.result.get("attributes", [])
        ),
    }
    result = _validate_service_result(result, run)
    if not any(attribute.get("selected", True) for attribute in result["attributes"]):
        raise ProductAttributeExtractionError(
            "no_attributes_selected",
            "There are no reviewed product attributes to apply.",
        )
    run.status = ProductAttributeExtractionRun.Status.APPLYING
    run.modified_by = user
    run.result = result
    run.save(update_fields=["status", "modified_by", "result", "updated_at"])

    config, _ = ProjectMatrixConfig.objects.select_for_update().get_or_create(
        project=run.project,
        defaults={"created_by": user, "modified_by": user},
    )
    columns = list(config.demand_columns or [])
    columns_by_key = {
        str(column.get("key") or "").strip(): column for column in columns
    }
    demands = {
        demand.id: demand
        for demand in SolutionDemand.objects.select_for_update().filter(
            project=run.project
        )
    }
    changed_demands = {}
    apply_warnings = []

    for attribute in result["attributes"]:
        if not attribute.get("selected", True):
            continue
        column_key = attribute["target_column_key"]
        if column_key in RESERVED_DEMAND_COLUMN_KEYS:
            raise ProductAttributeExtractionError(
                "reserved_column",
                "Product attributes cannot change structural Customer Demand columns.",
            )
        if column_key not in columns_by_key:
            column = {
                "key": column_key,
                "label": attribute["label"],
                "label_en": attribute.get("label_en") or attribute["label"],
                "label_de": attribute.get("label_de") or attribute["label"],
                "label_fr": attribute.get("label_fr") or attribute["label"],
                "type": "text",
                "order": len(columns),
            }
            if attribute.get("unit"):
                column["unit"] = attribute["unit"]
            columns.append(column)
            columns_by_key[column_key] = column

        applied_column = ProductAttributeAppliedColumn.objects.create(
            run=run,
            proposal_key=attribute["proposal_key"],
            column_key=column_key,
            applied_payload=attribute,
        )
        for value in attribute.get("values") or []:
            if not value.get("selected", True):
                continue
            demand = demands[value["solution_demand_id"]]
            custom_fields = dict(demand.custom_fields or {})
            current_value = custom_fields.get(column_key)
            if current_value not in (None, ""):
                if current_value != value["value"]:
                    apply_warnings.append(
                        f"Kept the existing manual value for Customer Demand {demand.id}, column {column_key}."
                    )
                continue
            custom_fields[column_key] = value["value"]
            demand.custom_fields = custom_fields
            demand.modified_by = user
            demand.updated_at = timezone.now()
            changed_demands[demand.id] = demand
            ProductAttributeAppliedValue.objects.create(
                applied_column=applied_column,
                solution_demand=demand,
                value=value["value"],
                applied_payload=value,
            )

    config.demand_columns = columns
    config.modified_by = user
    config.save(update_fields=["demand_columns", "modified_by", "updated_at"])
    if changed_demands:
        SolutionDemand.objects.bulk_update(
            list(changed_demands.values()),
            ["custom_fields", "modified_by", "updated_at"],
        )

    run.status = ProductAttributeExtractionRun.Status.APPLIED
    run.warnings = list(run.warnings or []) + apply_warnings
    run.applied_at = timezone.now()
    run.save(update_fields=["status", "warnings", "applied_at", "updated_at"])
    return run
