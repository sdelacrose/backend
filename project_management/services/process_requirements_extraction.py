import hashlib
import json
import logging
import re

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.text import slugify

from project_management.services.ai_extraction_client import post_extraction
from project_management.services.workstation_composition import create_project_workstation
from project_management.services.product_assembly_extraction import eligible_customer_demand_documents
from project_management.models import (
    DocumentContentSegment,
    ProcessAssemblyAppliedRow,
    ProcessAssemblyExtractionRun,
    ProcessAttributeAppliedColumn,
    ProcessAttributeAppliedValue,
    ProcessAttributeExtractionRun,
    Project,
    ProjectMatrixConfig,
    ProjectSolutionSpaceItem,
    Requirement,
    SolutionDemand,
    SolutionSpaceConfig,
)


SCHEMA_VERSION = "1"
RESERVED_REQUIREMENT_COLUMN_KEYS = {
    "sequence",
    "name",
    "object_number",
    "skill",
    "status",
}
logger = logging.getLogger(__name__)


class ProcessRequirementsExtractionError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _ordered_demands(project):
    return list(
        SolutionDemand.objects.filter(project=project).order_by(
            "level", "parent_id", "sort_order", "id"
        )
    )


def _ordered_solution_space(project):
    return list(
        ProjectSolutionSpaceItem.objects.filter(
            project=project,
            source_company_item__isnull=False,
            source_company_item__assembly_type="production_lines",
        )
        .select_related("source_company_item")
        .order_by("level", "parent_id", "sort_order", "id")
    )


def _ordered_requirements(project):
    return list(
        Requirement.objects.filter(project=project).order_by(
            "level", "parent_id", "sort_order", "id"
        )
    )


def _document_evidence(project, request=None, document_ids=None):
    documents = list(eligible_customer_demand_documents(
        project, document_ids=document_ids, request=request,
    ))
    by_id = {document.id: document for document in documents}
    evidence = {}
    characters = 0
    segments = DocumentContentSegment.objects.filter(
        document_id__in=by_id,
        index_id__in=[document.search_index.id for document in documents],
    ).order_by("document_id", "sequence", "id")
    for segment in segments.iterator():
        if not segment.text.strip():
            continue
        characters += len(segment.text)
        if (len(evidence) >= settings.PRODUCT_ASSEMBLY_EXTRACTION_MAX_SEGMENTS
                or characters > settings.PRODUCT_ASSEMBLY_EXTRACTION_MAX_CHARACTERS):
            raise ProcessRequirementsExtractionError(
                "source_content_too_large",
                "The Customer Demand sources exceed the extraction limit. Reduce the source scope before retrying.",
            )
        document = by_id[segment.document_id]
        evidence[f"segment_{segment.id}"] = {
            "document_id": document.id,
            "document_name": document.file_name,
            "content_hash": document.search_index.content_hash,
            "parser_version": document.search_index.parser_version,
            "location": segment.location_label,
            "text": segment.text,
        }
    return evidence


def build_process_input_snapshot(project, request=None, document_ids=None):
    matrix_config = ProjectMatrixConfig.objects.filter(project=project).first()
    solution_config = SolutionSpaceConfig.objects.filter(project=project).first()
    demands = _ordered_demands(project)
    solution_items = _ordered_solution_space(project)
    approved_solution_ids = {item.id for item in solution_items}
    requirements = _ordered_requirements(project)
    snapshot = {
        "project": {
            "id": project.id,
            "name": project.project_name,
        },
        "customer_demands": [
            {
                "id": demand.id,
                "parent_id": demand.parent_id,
                "hierarchy_number": demand.get_sequence(),
                "name": demand.subject,
                "demand_number": demand.demand_number,
                "custom_fields": demand.custom_fields or {},
            }
            for demand in demands
        ],
        "demand_columns": list(matrix_config.demand_columns or []) if matrix_config else [],
        "project_solution_space": [
            {
                "id": item.id,
                "parent_id": (
                    item.parent_id if item.parent_id in approved_solution_ids else None
                ),
                "hierarchy_number": item.get_sequence(),
                "name": item.source_company_item.assembly_name,
                "assembly_type": item.source_company_item.assembly_type,
                "required_skills": item.source_company_item.required_skills or [],
                "provided_skills": item.source_company_item.provided_skills or [],
                "custom_fields": item.source_company_item.custom_fields or {},
                "company_source_id": item.source_company_item_id,
            }
            for item in solution_items
        ],
        "solution_space_columns": list(solution_config.columns or []) if solution_config else [],
        "requirements": [
            {
                "id": requirement.id,
                "parent_id": requirement.parent_id,
                "hierarchy_number": requirement.get_sequence(),
                "name": requirement.name,
                "station_number": requirement.object_number,
                "custom_fields": requirement.custom_fields or {},
            }
            for requirement in requirements
        ],
        "requirement_columns": list(matrix_config.requirement_columns or []) if matrix_config else [],
    }
    # A single context carrier avoids repeating the full document for every variant.
    # This is snapshot-only evidence, not an attribute written to a Demand row.
    evidence = _document_evidence(project, request=request, document_ids=document_ids)
    if evidence and snapshot["customer_demands"]:
        carrier = snapshot["customer_demands"][0]
        carrier["custom_fields"] = {
            **carrier["custom_fields"], "source_document_evidence": evidence,
        }
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return digest, snapshot


@transaction.atomic
def create_process_assembly_run(project, user, project_language="en", request=None):
    project = Project.objects.select_for_update().get(pk=project.pk)
    if not SolutionDemand.objects.filter(project=project).exists():
        raise ProcessRequirementsExtractionError(
            "no_customer_demands",
            "Create or generate Customer Demands before extracting process assemblies.",
        )
    if not _ordered_solution_space(project):
        raise ProcessRequirementsExtractionError(
            "no_approved_process_assemblies",
            (
                "Select at least one company-approved Process Assembly for this "
                "project before extracting process requirements."
            ),
        )
    active = ProcessAssemblyExtractionRun.objects.filter(project=project).filter(
        Q(
            status__in=[
                ProcessAssemblyExtractionRun.Status.QUEUED,
                ProcessAssemblyExtractionRun.Status.EXTRACTING,
                ProcessAssemblyExtractionRun.Status.APPLYING,
            ]
        )
        | Q(
            status=ProcessAssemblyExtractionRun.Status.REVIEW_READY,
            hint_only=False,
        )
    )
    if active.exists():
        raise ProcessRequirementsExtractionError(
            "extraction_already_in_progress",
            "A Process Assembly extraction is already in progress for this project.",
        )
    snapshot_hash, snapshot = build_process_input_snapshot(project, request=request)
    return ProcessAssemblyExtractionRun.objects.create(
        project=project,
        status=ProcessAssemblyExtractionRun.Status.QUEUED,
        schema_version=SCHEMA_VERSION,
        project_language=(project_language or "en")[:16],
        input_snapshot_hash=snapshot_hash,
        input_snapshot=snapshot,
        hint_only=ProcessAssemblyExtractionRun.objects.filter(
            project=project,
            status=ProcessAssemblyExtractionRun.Status.APPLIED,
        ).exists(),
        created_by=user,
        modified_by=user,
    )


@transaction.atomic
def retry_process_assembly_run(run, user, request=None):
    run = ProcessAssemblyExtractionRun.objects.select_for_update().get(pk=run.pk)
    recoverable_review = (
        run.status == run.Status.REVIEW_READY
        and not run.hint_only
        and ((run.result or {}).get("rows") == [] or (run.result or {}).get("missing_capabilities"))
    )
    if run.status not in {run.Status.FAILED, run.Status.STALE} and not recoverable_review:
        raise ProcessRequirementsExtractionError(
            "run_not_retryable", "Only failed, stale or catalog-gap review runs can be retried."
        )
    if recoverable_review:
        # Retain the old review as history. Roll back this transition if creating
        # the replacement fails, so the user does not lose a usable review.
        run.status = run.Status.CANCELLED
        run.modified_by = user
        run.save(update_fields=["status", "modified_by", "updated_at"])
    return create_process_assembly_run(
        run.project, user, run.project_language, request=request,
    )


def dispatch_process_assembly_run(run_id):
    if settings.PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY:
        process_process_assembly_run(run_id)
        return
    from project_management.tasks import process_process_assembly_extraction_run

    process_process_assembly_extraction_run.apply_async(
        args=[run_id], queue=settings.PRODUCT_ASSEMBLY_EXTRACTION_QUEUE
    )


def _known_source_ids(run):
    snapshot = run.input_snapshot or {}
    return (
        {item["id"] for item in snapshot.get("customer_demands", [])},
        {item["id"] for item in snapshot.get("project_solution_space", [])},
        {item["id"] for item in snapshot.get("requirements", [])},
    )


def _source_records(run):
    snapshot = run.input_snapshot or {}
    return (
        {
            item["id"]: item
            for item in snapshot.get("customer_demands", [])
        },
        {
            item["id"]: item
            for item in snapshot.get("project_solution_space", [])
        },
    )


def _resolve_source_field(record, field):
    value = record
    for part in str(field or "").split("."):
        if not part or not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _validate_source_references(references, demand_records, solution_records):
    if not references:
        raise ProcessRequirementsExtractionError(
            "missing_source_references",
            "Every proposed station and value must reference its Demand or Solution Space inputs.",
        )
    for reference in references:
        source_type = reference.get("source_type")
        source_id = reference.get("source_id")
        records = (
            demand_records
            if source_type == "customer_demand"
            else solution_records
            if source_type == "solution_space"
            else None
        )
        if records is None or source_id not in records:
            raise ProcessRequirementsExtractionError(
                "invalid_source_reference",
                "The extraction service referenced data outside this project snapshot.",
            )
        field = reference.get("field")
        if field and _resolve_source_field(records[source_id], field) is None:
            raise ProcessRequirementsExtractionError(
                "invalid_source_field",
                "The extraction service referenced an unknown source field.",
            )


def _validate_quantitative_grounding(value, references, demand_records, solution_records):
    numeric_tokens = re.findall(
        r"(?<![\w.])-?\d+(?:[.,]\d+)?",
        str(value or ""),
    )
    if not numeric_tokens:
        return
    source_values = []
    for reference in references:
        field = reference.get("field")
        if not field:
            raise ProcessRequirementsExtractionError(
                "missing_quantitative_source_field",
                "Quantitative station data requires an explicit source field.",
            )
        records = (
            demand_records
            if reference.get("source_type") == "customer_demand"
            else solution_records
        )
        source_value = _resolve_source_field(records[reference["source_id"]], field)
        source_values.append(json.dumps(source_value, ensure_ascii=False, default=str))
    source_text = " ".join(source_values)
    if any(token not in source_text for token in numeric_tokens):
        raise ProcessRequirementsExtractionError(
            "unsupported_quantitative_value",
            "The extraction service introduced a quantitative value not present in its cited source field.",
        )


def _next_station_numbers(rows, existing_requirements):
    used = {
        str(item.get("station_number") or "").strip().casefold()
        for item in existing_requirements
        if str(item.get("station_number") or "").strip()
    }
    for row in rows:
        number = str(row.get("station_number") or "").strip()
        if number:
            key = number.casefold()
            if key in used:
                raise ProcessRequirementsExtractionError(
                    "duplicate_station_number",
                    f"Station number '{number}' is already in use.",
                )
            used.add(key)
            row["station_number"] = number
            continue
        index = 1
        while f"st-{index:03d}" in used:
            index += 1
        row["station_number"] = f"ST-{index:03d}"
        used.add(row["station_number"].casefold())


def validate_process_assembly_result(result, run):
    if not isinstance(result, dict) or str(result.get("schema_version")) != SCHEMA_VERSION:
        raise ProcessRequirementsExtractionError(
            "invalid_service_response", "The process extraction response is incompatible."
        )
    missing = result.get("missing_capabilities", [])
    if not isinstance(missing, list) or len(missing) > 100 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in missing
    ):
        raise ProcessRequirementsExtractionError(
            "invalid_service_response", "The process capability gaps are invalid."
        )
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise ProcessRequirementsExtractionError(
            "no_process_assemblies_found",
            "No process assemblies could be identified from the Demand List and Project Solution Space.",
        )
    if not rows:
        explanations = [
            message
            for field in ("unresolved_items", "warnings")
            for message in (result.get(field) if isinstance(result.get(field), list) else [])
            if isinstance(message, str) and message.strip()
        ]
        if run.hint_only or explanations or missing:
            return {**result, "rows": []}
        raise ProcessRequirementsExtractionError(
            "no_process_assemblies_found",
            "No process assemblies could be identified from the Demand List and Project Solution Space.",
        )
    demand_ids, solution_ids, _ = _known_source_ids(run)
    demand_records, solution_records = _source_records(run)
    normalized_rows = []
    seen_keys = set()
    parent_by_key = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ProcessRequirementsExtractionError("invalid_row", "Every proposed station must be an object.")
        key = str(row.get("proposal_key") or "").strip()
        name = str(row.get("name") or "").strip()
        if not key or len(key) > 128 or key in seen_keys:
            raise ProcessRequirementsExtractionError("invalid_proposal_key", "Station proposal keys must be unique.")
        if not name or len(name) > 255:
            raise ProcessRequirementsExtractionError("invalid_station_name", "Every station needs a valid name.")
        linked_demand_ids = list(dict.fromkeys(row.get("linked_demand_ids") or []))
        linked_solution_ids = list(dict.fromkeys(row.get("linked_solution_space_ids") or []))
        if not linked_demand_ids or not set(linked_demand_ids).issubset(demand_ids):
            raise ProcessRequirementsExtractionError(
                "invalid_demand_link", "Every station must link to a Customer Demand in this project."
            )
        if not set(linked_solution_ids).issubset(solution_ids):
            raise ProcessRequirementsExtractionError(
                "invalid_solution_space_link", "A station referenced an unknown Project Solution Space item."
            )
        source_kind = row.get("source_kind")
        if source_kind != "matched":
            raise ProcessRequirementsExtractionError(
                "unapproved_process_assembly",
                "Every proposed station must match a company-approved Process Assembly.",
            )
        if not linked_solution_ids:
            raise ProcessRequirementsExtractionError(
                "missing_solution_space_match", "Matched stations require a Project Solution Space link."
            )
        references = row.get("source_references") or []
        _validate_source_references(references, demand_records, solution_records)
        _validate_quantitative_grounding(
            f"{name} {row.get('rationale') or ''}",
            references,
            demand_records,
            solution_records,
        )
        seen_keys.add(key)
        parent_by_key[key] = row.get("parent_key") or None
        normalized_rows.append(
            {
                **row,
                "proposal_key": key,
                "parent_key": row.get("parent_key") or None,
                "name": name,
                "linked_demand_ids": linked_demand_ids,
                "linked_solution_space_ids": linked_solution_ids,
                "selected": row.get("selected", True),
            }
        )
    for key, parent_key in parent_by_key.items():
        if parent_key and parent_key not in seen_keys:
            raise ProcessRequirementsExtractionError("invalid_parent", f"Station '{key}' has an unknown parent.")

    def depth(key, path=None):
        path = set(path or ())
        if key in path:
            raise ProcessRequirementsExtractionError("hierarchy_cycle", "The proposed station hierarchy contains a cycle.")
        parent_key = parent_by_key[key]
        return 1 if not parent_key else 1 + depth(parent_key, path | {key})

    if max(depth(key) for key in seen_keys) > Requirement.MAX_LEVEL:
        raise ProcessRequirementsExtractionError(
            "hierarchy_too_deep", f"The proposed hierarchy exceeds {Requirement.MAX_LEVEL} levels."
        )
    _next_station_numbers(
        normalized_rows, (run.input_snapshot or {}).get("requirements", [])
    )
    parent_keys = {row.get("parent_key") for row in normalized_rows}
    for row in normalized_rows:
        row["create_workstation"] = bool(
            settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED
            and row["proposal_key"] not in parent_keys
            and len(row["linked_solution_space_ids"]) == 1
            and row.get("create_workstation", True) is True
        )
    return {**result, "rows": normalized_rows}


def process_process_assembly_run(run_id):
    run = ProcessAssemblyExtractionRun.objects.select_related("project").get(id=run_id)
    if run.status not in {
        ProcessAssemblyExtractionRun.Status.QUEUED,
        ProcessAssemblyExtractionRun.Status.FAILED,
    }:
        return
    run.status = ProcessAssemblyExtractionRun.Status.EXTRACTING
    run.started_at = timezone.now()
    run.error_code = ""
    run.error_message = ""
    run.save(update_fields=["status", "started_at", "error_code", "error_message", "updated_at"])
    try:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": {
                **run.input_snapshot["project"],
                "language": run.project_language,
            },
            "customer_demands": run.input_snapshot["customer_demands"],
            "demand_columns": run.input_snapshot["demand_columns"],
            "project_solution_space": run.input_snapshot["project_solution_space"],
            "solution_space_columns": run.input_snapshot["solution_space_columns"],
            "existing_requirements": run.input_snapshot["requirements"],
            "requirement_columns": run.input_snapshot["requirement_columns"],
        }
        response = post_extraction(
            f"{settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL.rstrip('/')}/v1/process-assemblies/extract",
            headers={
                settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER: settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY,
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
            raise ProcessRequirementsExtractionError(
                "extraction_service_failed",
                detail or "The extraction service could not process the station plan.",
            )
        raw_result = response.json()
        result = validate_process_assembly_result(raw_result, run)
        # Coverage confirmation is a human review, never model-authored metadata.
        result.pop("coverage_review", None)
        if run.hint_only:
            applied = {
                _station_fingerprint(item.applied_payload)
                for item in ProcessAssemblyAppliedRow.objects.filter(
                    run__project=run.project,
                    run__status=ProcessAssemblyExtractionRun.Status.APPLIED,
                )
            }
            result = {
                **result,
                "rows": [
                    {**row, "is_new_station": _station_fingerprint(row) not in applied}
                    for row in result["rows"]
                ],
            }
        run.refresh_from_db(fields=["status"])
        if run.status == ProcessAssemblyExtractionRun.Status.CANCELLED:
            return
        run.raw_result = raw_result
        run.result = result
        run.warnings = result.get("warnings") or []
        run.status = ProcessAssemblyExtractionRun.Status.REVIEW_READY
        run.completed_at = timezone.now()
        run.save(update_fields=["raw_result", "result", "warnings", "status", "completed_at", "updated_at"])
    except ProcessRequirementsExtractionError as exc:
        _fail_run(run, exc.code, exc.message)
    except requests.Timeout:
        _fail_run(run, "extraction_service_timeout", "The AI extraction timed out after automatic retry. Your data is unchanged. Please retry extraction.")
    except (requests.RequestException, ValueError):
        _fail_run(run, "extraction_service_unavailable", "The process extraction service is temporarily unavailable.")
    except Exception:
        logger.exception("Unexpected Process Assembly extraction failure", extra={"run_id": run.id})
        _fail_run(run, "unexpected_extraction_failure", "Process Assembly extraction could not be completed.")


def _station_fingerprint(row):
    return (
        " ".join(str(row.get("name") or "").casefold().split()),
        tuple(sorted(row.get("linked_demand_ids") or [])),
    )


def _current_snapshot_hash(run):
    demands = (run.input_snapshot or {}).get("customer_demands", [])
    evidence = (demands[0].get("custom_fields", {}).get("source_document_evidence", {})
                if demands else {})
    document_ids = {item["document_id"] for item in evidence.values()}
    digest, _ = build_process_input_snapshot(run.project, document_ids=document_ids)
    return digest


def _topological_rows(rows):
    by_key = {row["proposal_key"]: row for row in rows}
    ordered = []
    pending = list(rows)
    while pending:
        ready = [
            row
            for row in pending
            if not row.get("parent_key") or row["parent_key"] in {item["proposal_key"] for item in ordered}
        ]
        if not ready:
            raise ProcessRequirementsExtractionError("hierarchy_cycle", "The proposed station hierarchy contains a cycle.")
        for row in ready:
            ordered.append(by_key[row["proposal_key"]])
            pending.remove(row)
    return ordered


def _station_coverage_signature(run):
    context = {
        "requirements": list(Requirement.objects.filter(project_id=run.project_id)
            .order_by("id").values("id", "name", "object_number")),
        "rows": (run.result or {}).get("rows", []),
        "unresolved_items": (run.result or {}).get("unresolved_items", []),
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest()


def station_coverage_is_reviewed(run):
    review = (run.result or {}).get("coverage_review") or {}
    return bool(review.get("user_id") and review.get("notes")
        and review.get("signature") == _station_coverage_signature(run))


@transaction.atomic
def confirm_station_coverage(run, user, notes):
    run = ProcessAssemblyExtractionRun.objects.select_for_update().get(pk=run.pk)
    Project.objects.select_for_update().get(pk=run.project_id)
    notes = str(notes or "").strip()
    if run.status != run.Status.APPLIED or not notes or len(notes) > 2000:
        raise ProcessRequirementsExtractionError("invalid_coverage_review",
            "Review the applied stations and explain how the source scope is covered.")
    run.result = {**run.result, "coverage_review": {
        "user_id": user.pk, "notes": notes, "reviewed_at": timezone.now().isoformat(),
        "signature": _station_coverage_signature(run),
    }}
    run.modified_by = user
    run.save(update_fields=["result", "modified_by", "updated_at"])
    return run


def apply_process_assembly_run(run, user, rows=None):
    stale_error = None
    with transaction.atomic():
        run = ProcessAssemblyExtractionRun.objects.select_for_update().get(id=run.id)
        Project.objects.select_for_update().get(pk=run.project_id)
        if run.status == ProcessAssemblyExtractionRun.Status.APPLIED:
            return run
        if run.hint_only:
            raise ProcessRequirementsExtractionError(
                "hint_only_run", "Later station extractions are suggestions only and cannot change Requirements."
            )
        if run.status != ProcessAssemblyExtractionRun.Status.REVIEW_READY:
            raise ProcessRequirementsExtractionError("run_not_review_ready", "Only a review-ready extraction can be applied.")
        if (run.result or {}).get("missing_capabilities"):
            raise ProcessRequirementsExtractionError(
                "missing_process_capabilities",
                "Review the missing process capabilities, update the selected catalog and retry extraction before applying stations.",
            )
        if ProcessAssemblyExtractionRun.objects.filter(
            project=run.project, status=ProcessAssemblyExtractionRun.Status.APPLIED
        ).exclude(pk=run.pk).exists():
            raise ProcessRequirementsExtractionError(
                "extraction_already_applied", "Process Assemblies have already been generated for this project."
            )
        if _current_snapshot_hash(run) != run.input_snapshot_hash:
            run.status = ProcessAssemblyExtractionRun.Status.STALE
            run.error_code = "extraction_inputs_changed"
            run.error_message = "Demand, Solution Space, or Requirement data changed after extraction. Run it again."
            run.save(update_fields=["status", "error_code", "error_message", "updated_at"])
            stale_error = ProcessRequirementsExtractionError(run.error_code, run.error_message)
        else:
            result = validate_process_assembly_result(
                {**run.result, "rows": rows if rows is not None else run.result.get("rows", [])},
                run,
            )
            selected_rows = [row for row in result["rows"] if row.get("selected", True)]
            if not selected_rows:
                raise ProcessRequirementsExtractionError("no_selected_rows", "Select at least one station to apply.")
            selected_keys = {row["proposal_key"] for row in selected_rows}
            if any(
                row.get("parent_key") and row["parent_key"] not in selected_keys
                for row in selected_rows
            ):
                raise ProcessRequirementsExtractionError(
                    "unselected_parent",
                    "A selected child station cannot be applied without its parent station.",
                )
            run.status = ProcessAssemblyExtractionRun.Status.APPLYING
            run.result = result
            run.modified_by = user
            run.save(update_fields=["status", "result", "modified_by", "updated_at"])
            created = {}
            for row in _topological_rows(selected_rows):
                parent = created.get(row.get("parent_key"))
                sort_order = (
                    Requirement.objects.filter(project=run.project, parent=parent).aggregate(max_order=Max("sort_order"))["max_order"]
                    or 0
                ) + 1
                requirement = Requirement.objects.create(
                    project=run.project,
                    parent=parent,
                    level=parent.level + 1 if parent else 1,
                    sort_order=sort_order,
                    name=row["name"],
                    object_number=row["station_number"],
                    created_by=user,
                    modified_by=user,
                )
                requirement.solution_demands.set(row["linked_demand_ids"])
                for item in ProjectSolutionSpaceItem.objects.filter(
                    project=run.project, id__in=row["linked_solution_space_ids"]
                ):
                    item.requirements.add(requirement)
                if row.get("create_workstation"):
                    selected_template = ProjectSolutionSpaceItem.objects.select_related(
                        "source_company_item"
                    ).get(project=run.project, pk=row["linked_solution_space_ids"][0])
                    try:
                        create_project_workstation(
                            project=run.project, source=selected_template.source_company_item,
                            actor=user, source_requirement=requirement,
                            idempotency_key=f"requirement:{requirement.pk}",
                        )
                    except ValidationError as exc:
                        raise ProcessRequirementsExtractionError(
                            "invalid_workstation_template",
                            "The selected workstation template cannot be copied. Check its hierarchy and quantities before applying again.",
                        ) from exc
                ProcessAssemblyAppliedRow.objects.create(
                    run=run,
                    proposal_key=row["proposal_key"],
                    applied_payload=row,
                    requirement=requirement,
                )
                created[row["proposal_key"]] = requirement
            run.status = ProcessAssemblyExtractionRun.Status.APPLIED
            run.applied_at = timezone.now()
            run.save(update_fields=["status", "applied_at", "updated_at"])
    if stale_error:
        raise stale_error
    return run


@transaction.atomic
def create_process_attribute_run(project, user, project_language="en", request=None):
    project = Project.objects.select_for_update().get(pk=project.pk)
    if not Requirement.objects.filter(project=project).exists():
        raise ProcessRequirementsExtractionError(
            "no_requirements", "Create or generate stations before extracting station attributes."
        )
    if not _ordered_solution_space(project):
        raise ProcessRequirementsExtractionError(
            "no_approved_process_assemblies",
            (
                "Select at least one company-approved Process Assembly for this "
                "project before extracting station attributes."
            ),
        )
    if ProcessAttributeExtractionRun.objects.filter(project=project).filter(
        Q(status__in=[
            ProcessAttributeExtractionRun.Status.QUEUED,
            ProcessAttributeExtractionRun.Status.EXTRACTING,
            ProcessAttributeExtractionRun.Status.APPLYING,
        ])
        | Q(status=ProcessAttributeExtractionRun.Status.REVIEW_READY, hint_only=False)
    ).exists():
        raise ProcessRequirementsExtractionError(
            "attribute_extraction_already_in_progress", "A station attribute extraction is already in progress."
        )
    snapshot_hash, snapshot = build_process_input_snapshot(project, request=request)
    return ProcessAttributeExtractionRun.objects.create(
        project=project,
        status=ProcessAttributeExtractionRun.Status.QUEUED,
        schema_version=SCHEMA_VERSION,
        project_language=(project_language or "en")[:16],
        input_snapshot_hash=snapshot_hash,
        input_snapshot=snapshot,
        hint_only=ProcessAttributeExtractionRun.objects.filter(
            project=project, status=ProcessAttributeExtractionRun.Status.APPLIED
        ).exists(),
        created_by=user,
        modified_by=user,
    )


def dispatch_process_attribute_run(run_id):
    if settings.PRODUCT_ASSEMBLY_EXTRACTION_RUN_SYNCHRONOUSLY:
        process_process_attribute_run(run_id)
        return
    from project_management.tasks import process_process_attribute_extraction_run

    process_process_attribute_extraction_run.apply_async(
        args=[run_id], queue=settings.PRODUCT_ASSEMBLY_EXTRACTION_QUEUE
    )


def _known_requirement_columns(run):
    return {
        str(column.get("key") or "").strip(): column
        for column in (run.input_snapshot or {}).get("requirement_columns", [])
        if str(column.get("key") or "").strip()
    }


def _generated_column_key(attribute):
    identity = "|".join(
        [
            str(attribute.get("label_en") or attribute.get("label") or "").strip().casefold(),
            str(attribute.get("unit") or "").strip().casefold(),
        ]
    )
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    label_slug = slugify(identity.split("|", 1)[0]).replace("-", "_")[:60]
    return f"ai_process_attr_{label_slug or 'attribute'}_{suffix}"


def validate_process_attribute_result(result, run):
    if not isinstance(result, dict) or str(result.get("schema_version")) != SCHEMA_VERSION:
        raise ProcessRequirementsExtractionError("invalid_service_response", "The station attribute response is incompatible.")
    attributes = result.get("attributes")
    if not isinstance(attributes, list):
        raise ProcessRequirementsExtractionError("no_process_attributes_found", "No station attributes could be identified.")
    if not attributes:
        if run.hint_only:
            return {**result, "attributes": []}
        raise ProcessRequirementsExtractionError("no_process_attributes_found", "No station attributes could be identified.")
    demand_ids, solution_ids, requirement_ids = _known_source_ids(run)
    demand_records, solution_records = _source_records(run)
    known_columns = _known_requirement_columns(run)
    requirements = {
        item["id"]: item for item in (run.input_snapshot or {}).get("requirements", [])
    }
    seen_proposals = set()
    seen_columns = set()
    normalized = []
    for attribute in attributes:
        proposal_key = str(attribute.get("proposal_key") or "").strip()
        label = str(attribute.get("label") or "").strip()
        if not proposal_key or proposal_key in seen_proposals or len(proposal_key) > 128:
            raise ProcessRequirementsExtractionError("invalid_proposal_key", "Attribute proposal keys must be unique.")
        if not label or len(label) > 255:
            raise ProcessRequirementsExtractionError("invalid_attribute_label", "Every station attribute needs a valid label.")
        attribute_references = attribute.get("source_references") or []
        _validate_source_references(
            attribute_references,
            demand_records,
            solution_records,
        )
        existing_key = attribute.get("existing_column_key") or None
        if existing_key and existing_key not in known_columns:
            raise ProcessRequirementsExtractionError("unknown_existing_column", "An unknown Requirement column was referenced.")
        if existing_key in RESERVED_REQUIREMENT_COLUMN_KEYS:
            raise ProcessRequirementsExtractionError("reserved_column", "Station attributes cannot target structural Requirement columns.")
        target_key = existing_key or _generated_column_key(attribute)
        values = []
        seen_requirements = set()
        for value in attribute.get("values") or []:
            requirement_id = value.get("requirement_id")
            text_value = str(value.get("value") or "").strip()
            if requirement_id not in requirement_ids or requirement_id in seen_requirements or not text_value:
                raise ProcessRequirementsExtractionError("invalid_attribute_value", "Each station may have one non-empty value per attribute.")
            value_references = value.get("source_references") or []
            _validate_source_references(
                value_references,
                demand_records,
                solution_records,
            )
            _validate_quantitative_grounding(
                text_value,
                value_references,
                demand_records,
                solution_records,
            )
            current_value = (requirements[requirement_id].get("custom_fields") or {}).get(target_key)
            values.append({
                **value,
                "value": text_value,
                "selected": value.get("selected", True),
                "current_value": current_value,
                "is_new_value": current_value in (None, ""),
                "has_conflict": current_value not in (None, "", text_value),
            })
            seen_requirements.add(requirement_id)
        if not values:
            raise ProcessRequirementsExtractionError("missing_attribute_values", "Every station attribute needs at least one value.")
        seen_proposals.add(proposal_key)
        if target_key in seen_columns:
            previous = next(item for item in normalized if item["target_column_key"] == target_key)
            same_attribute = _generated_column_key(previous) == _generated_column_key(attribute)
            previous_ids = {value["requirement_id"] for value in previous["values"]}
            if (not same_attribute or previous_ids.intersection(seen_requirements)
                    or previous["selected"] != attribute.get("selected", True)):
                raise ProcessRequirementsExtractionError(
                    "duplicate_target_column",
                    "Conflicting attributes target the same Requirement column. Review the proposed values.",
                )
            # Providers may return one attribute per station. Keep one column
            # when those independently validated station values do not overlap.
            previous["values"].extend(values)
            previous["source_references"] = [
                *(previous.get("source_references") or []), *attribute_references,
            ]
            previous["conflicts"] = [
                *(previous.get("conflicts") or []), *(attribute.get("conflicts") or []),
            ]
            continue
        seen_columns.add(target_key)
        localized_label_key = f"label_{run.project_language}"
        normalized.append({
            **attribute,
            "proposal_key": proposal_key,
            "label": label,
            localized_label_key: label,
            "existing_column_key": existing_key,
            "target_column_key": target_key,
            "create_column": not bool(existing_key),
            "selected": attribute.get("selected", True),
            "is_new_attribute": target_key not in known_columns,
            "values": values,
        })
    return {**result, "attributes": normalized}


def process_process_attribute_run(run_id):
    run = ProcessAttributeExtractionRun.objects.select_related("project").get(id=run_id)
    if run.status not in {ProcessAttributeExtractionRun.Status.QUEUED, ProcessAttributeExtractionRun.Status.FAILED}:
        return
    run.status = ProcessAttributeExtractionRun.Status.EXTRACTING
    run.started_at = timezone.now()
    run.error_code = ""
    run.error_message = ""
    run.save(update_fields=["status", "started_at", "error_code", "error_message", "updated_at"])
    try:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": {**run.input_snapshot["project"], "language": run.project_language},
            "customer_demands": run.input_snapshot["customer_demands"],
            "demand_columns": run.input_snapshot["demand_columns"],
            "project_solution_space": run.input_snapshot["project_solution_space"],
            "solution_space_columns": run.input_snapshot["solution_space_columns"],
            "requirements": run.input_snapshot["requirements"],
            "requirement_columns": run.input_snapshot["requirement_columns"],
        }
        response = post_extraction(
            f"{settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_URL.rstrip('/')}/v1/process-attributes/extract",
            headers={
                settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY_HEADER: settings.PRODUCT_ASSEMBLY_EXTRACTION_SERVICE_KEY,
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
            raise ProcessRequirementsExtractionError(
                "extraction_service_failed", detail or "The extraction service could not process station attributes."
            )
        raw_result = response.json()
        result = validate_process_attribute_result(raw_result, run)
        run.refresh_from_db(fields=["status"])
        if run.status == ProcessAttributeExtractionRun.Status.CANCELLED:
            return
        run.raw_result = raw_result
        run.result = result
        run.warnings = result.get("warnings") or []
        run.status = ProcessAttributeExtractionRun.Status.REVIEW_READY
        run.completed_at = timezone.now()
        run.save(update_fields=["raw_result", "result", "warnings", "status", "completed_at", "updated_at"])
    except ProcessRequirementsExtractionError as exc:
        _fail_run(run, exc.code, exc.message)
    except requests.Timeout:
        _fail_run(run, "extraction_service_timeout", "The AI extraction timed out after automatic retry. Your data is unchanged. Please retry extraction.")
    except (requests.RequestException, ValueError):
        _fail_run(run, "extraction_service_unavailable", "The station attribute service is temporarily unavailable.")
    except Exception:
        logger.exception("Unexpected station attribute extraction failure", extra={"run_id": run.id})
        _fail_run(run, "unexpected_extraction_failure", "Station attribute extraction could not be completed.")


def apply_process_attribute_run(run, user, attributes=None):
    stale_error = None
    with transaction.atomic():
        run = ProcessAttributeExtractionRun.objects.select_for_update().get(id=run.id)
        Project.objects.select_for_update().get(pk=run.project_id)
        if run.status == ProcessAttributeExtractionRun.Status.APPLIED:
            return run
        if run.hint_only:
            raise ProcessRequirementsExtractionError(
                "hint_only_run", "Later station attribute extractions are suggestions only and cannot change Requirements."
            )
        if run.status != ProcessAttributeExtractionRun.Status.REVIEW_READY:
            raise ProcessRequirementsExtractionError("run_not_review_ready", "Only a review-ready extraction can be applied.")
        if _current_snapshot_hash(run) != run.input_snapshot_hash:
            run.status = ProcessAttributeExtractionRun.Status.STALE
            run.error_code = "extraction_inputs_changed"
            run.error_message = "Requirements, Demands, or Solution Space changed after extraction. Run it again."
            run.save(update_fields=["status", "error_code", "error_message", "updated_at"])
            stale_error = ProcessRequirementsExtractionError(run.error_code, run.error_message)
        else:
            result = validate_process_attribute_result(
                {**run.result, "attributes": attributes if attributes is not None else run.result.get("attributes", [])}, run
            )
            run.status = ProcessAttributeExtractionRun.Status.APPLYING
            run.result = result
            run.modified_by = user
            run.save(update_fields=["status", "result", "modified_by", "updated_at"])
            config, _ = ProjectMatrixConfig.objects.select_for_update().get_or_create(
                project=run.project, defaults={"created_by": user, "modified_by": user}
            )
            columns = list(config.requirement_columns or [])
            columns_by_key = {str(column.get("key") or "").strip(): column for column in columns}
            requirements = {
                item.id: item
                for item in Requirement.objects.select_for_update().filter(project=run.project)
            }
            changed = {}
            warnings = []
            for attribute in result["attributes"]:
                if not attribute.get("selected", True):
                    continue
                column_key = attribute["target_column_key"]
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
                applied_column = ProcessAttributeAppliedColumn.objects.create(
                    run=run,
                    proposal_key=attribute["proposal_key"],
                    column_key=column_key,
                    applied_payload=attribute,
                )
                for value in attribute.get("values") or []:
                    if not value.get("selected", True):
                        continue
                    requirement = requirements[value["requirement_id"]]
                    custom_fields = dict(requirement.custom_fields or {})
                    current_value = custom_fields.get(column_key)
                    if current_value not in (None, ""):
                        if current_value != value["value"]:
                            warnings.append(
                                f"Kept the existing manual value for Requirement {requirement.id}, column {column_key}."
                            )
                        continue
                    custom_fields[column_key] = value["value"]
                    requirement.custom_fields = custom_fields
                    requirement.modified_by = user
                    requirement.updated_at = timezone.now()
                    changed[requirement.id] = requirement
                    ProcessAttributeAppliedValue.objects.create(
                        applied_column=applied_column,
                        requirement=requirement,
                        value=value["value"],
                        applied_payload=value,
                    )
            config.requirement_columns = columns
            config.modified_by = user
            config.save(update_fields=["requirement_columns", "modified_by", "updated_at"])
            if changed:
                Requirement.objects.bulk_update(
                    list(changed.values()), ["custom_fields", "modified_by", "updated_at"]
                )
            run.status = ProcessAttributeExtractionRun.Status.APPLIED
            run.warnings = list(run.warnings or []) + warnings
            run.applied_at = timezone.now()
            run.save(update_fields=["status", "warnings", "applied_at", "updated_at"])
    if stale_error:
        raise stale_error
    return run


def _fail_run(run, code, message):
    run.refresh_from_db(fields=["status"])
    status_values = type(run).Status
    if run.status == status_values.CANCELLED:
        return
    run.status = status_values.FAILED
    run.error_code = code
    run.error_message = message
    run.completed_at = timezone.now()
    run.save(update_fields=["status", "error_code", "error_message", "completed_at", "updated_at"])
