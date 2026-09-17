import hashlib
import json

from django.conf import settings

from companyprofileapis.models import Opportunity
from project_management.models import (
    BillOfMaterialPart,
    Document,
    DocumentContentSegment,
    ProjectSolutionSpaceItem,
    ProcessAssemblyAppliedRow,
    ProcessAssemblyExtractionRun,
    ProcessAttributeAppliedValue,
    ProcessAttributeExtractionRun,
    ProductAssemblyAppliedRow,
    ProductAssemblyExtractionRun,
    ProductAttributeAppliedValue,
    ProductAttributeExtractionRun,
    ProjectWorkstation,
    ProcessAssemblyRecommendationRun,
    Requirement,
    SolutionDemand,
)
from project_management.services.document_access import get_accessible_document_queryset


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _identifier(value):
    return str(value) if value is not None else None


def _date(value):
    return value.isoformat() if value is not None else None


def _tree_rows(queryset, name_field, number_field=None):
    items = list(queryset.order_by("sort_order", "id"))
    sibling_ids = {}
    for item in items:
        sibling_ids.setdefault(item.parent_id, []).append(item.id)
    sibling_positions = {
        item_id: position
        for ids in sibling_ids.values()
        for position, item_id in enumerate(ids, start=1)
    }
    parent_ids = {item.id: item.parent_id for item in items}
    sequence_cache = {}

    def sequence_for(item_id, visiting=None):
        if item_id in sequence_cache:
            return sequence_cache[item_id]
        visiting = visiting or set()
        if item_id in visiting:
            return str(sibling_positions.get(item_id, 1))
        visiting.add(item_id)
        part = str(sibling_positions.get(item_id, 1))
        parent_id = parent_ids.get(item_id)
        if parent_id in parent_ids:
            value = f"{sequence_for(parent_id, visiting)}.{part}"
        else:
            value = part
        visiting.remove(item_id)
        sequence_cache[item_id] = value
        return value

    rows = []
    for item in items:
        row = {
            "id": _identifier(item.id),
            "parent_id": _identifier(item.parent_id),
            "hierarchy_number": sequence_for(item.id),
            "name": getattr(item, name_field),
            "status": getattr(item, "status", None),
            "custom_fields": item.custom_fields or {},
            "updated_at": item.updated_at.isoformat(),
        }
        if number_field:
            row[number_field] = getattr(item, number_field, None)
        rows.append(row)
    return rows


def _document_customer_demand_rows(project):
    rows = _tree_rows(SolutionDemand.objects.filter(project=project), "subject", "demand_number")
    indexed = {row["id"]: row for row in rows}
    values = ProductAttributeAppliedValue.objects.filter(
        applied_column__run__project=project,
        applied_column__run__status=ProductAttributeExtractionRun.Status.APPLIED,
        solution_demand__project=project,
    ).select_related("applied_column").order_by("id")
    definitions = {}
    for value in values:
        row = indexed.get(str(value.solution_demand_id))
        column_key = value.applied_column.column_key
        if row and row["custom_fields"].get(column_key) not in (None, ""):
            definitions.setdefault(row["id"], {})[column_key] = _attribute_definition(value.applied_column)
    for row in rows:
        if row["id"] in definitions:
            row["custom_fields"] = {
                **row["custom_fields"],
                "_reviewed_attribute_definitions": definitions[row["id"]],
            }
    return rows


def _project_workstation_rows(project):
    workstations = (
        ProjectWorkstation.objects.filter(project=project, status="active")
        .select_related("source_process_assembly")
        .prefetch_related("revisions__snapshot", "nodes__parent")
        .order_by("name", "id")
    )
    rows = []
    for workstation in workstations:
        revision = next(
            (item for item in workstation.revisions.all() if item.number == workstation.active_revision),
            None,
        )
        if revision is None:
            continue
        nodes = [
            {
                "occurrence_id": str(node.occurrence_id),
                "parent_occurrence_id": str(node.parent.occurrence_id) if node.parent_id else None,
                "name": node.name,
                "role": node.role,
                "is_active": node.is_active,
                "quantity": str(node.quantity),
                "unit": node.unit,
                "level": node.level,
                "sort_order": node.sort_order,
                "custom_fields": node.custom_fields or {},
            }
            for node in workstation.nodes.all()
            if node.revision_id == revision.id
        ]
        nodes.sort(key=lambda node: (node["sort_order"], node["level"], node["occurrence_id"]))
        rows.append(
            {
                "id": str(workstation.id),
                "name": workstation.name,
                "source_process_assembly_id": str(workstation.source_process_assembly_id) if workstation.source_process_assembly_id else None,
                "active_revision": revision.number,
                "revision_reason": revision.reason,
                "source_hash": revision.source_hash,
                "snapshot_hash": revision.snapshot.content_hash if hasattr(revision, "snapshot") else "",
                "nodes": nodes,
                "updated_at": workstation.updated_at.isoformat(),
            }
        )
    return rows


def _unresolved_decision_rows(project):
    return [
        {
            "id": str(run.id),
            "type": "process_assembly_recommendation",
            "reason": run.decision_reason,
            "candidate_count": len(run.ranked_candidates or []),
            "updated_at": run.updated_at.isoformat(),
        }
        for run in ProcessAssemblyRecommendationRun.objects.filter(
            project=project,
            selection_outcome=ProcessAssemblyRecommendationRun.OUTCOME_REVIEW,
        ).order_by("id")
    ]


def build_project_content_snapshot(project, document_ids=None):
    documents = Document.objects.filter(folder__project=project).select_related(
        "search_index"
    )
    if document_ids is not None:
        documents = documents.filter(id__in=document_ids)
    documents = list(documents.order_by("id"))

    max_segments = settings.PROJECT_CONTENT_MAX_SEGMENTS
    max_characters = settings.PROJECT_CONTENT_MAX_CHARACTERS
    segments = []
    total_characters = 0
    indexes = [
        document.search_index.id
        for document in documents
        if hasattr(document, "search_index")
        and document.search_index.status in {"searchable", "partial"}
    ]
    for segment in (
        DocumentContentSegment.objects.filter(index_id__in=indexes)
        .select_related("document")
        .order_by("document_id", "sequence", "id")
    ):
        if len(segments) >= max_segments or total_characters >= max_characters:
            break
        remaining = max_characters - total_characters
        text = segment.text[:remaining]
        if not text:
            continue
        segments.append(
            {
                "document_id": _identifier(segment.document_id),
                "document_name": segment.document.file_name,
                "segment_id": _identifier(segment.id),
                "segment_type": segment.segment_type,
                "sequence": segment.sequence,
                "location": segment.location_label,
                "text": text,
            }
        )
        total_characters += len(text)

    requirements = _tree_rows(
        Requirement.objects.filter(project=project),
        "name",
        "object_number",
    )
    opportunities = [
        {
            "id": _identifier(opportunity.id),
            "title": opportunity.title,
            "customer_company": (
                opportunity.customer_company.company_name
                if opportunity.customer_company_id
                else ""
            ),
            "country": opportunity.country or "",
            "currency": project.currency,
            "revenue": (
                str(opportunity.revenue)
                if opportunity.revenue is not None
                else None
            ),
            "value": (
                str(opportunity.value)
                if opportunity.value is not None
                else None
            ),
            "contract_date": _date(opportunity.contract_date),
            "planned_delivery_start_date": _date(
                opportunity.planned_delivery_start_date
            ),
            "planned_delivery_end_date": _date(
                opportunity.planned_delivery_end_date
            ),
            "purchase_order": opportunity.purchase_order or "",
            "type_of_service": opportunity.type_of_service or "",
            "updated_at": opportunity.updated_at.isoformat(),
        }
        for opportunity in Opportunity.objects.filter(projects=project)
        .select_related("customer_company")
        .order_by("id")
    ]

    snapshot = {
        "schema_version": "1",
        "project": {
            "id": _identifier(project.id),
            "name": project.project_name,
            "description": project.project_description or "",
            "start_date": _date(project.project_start_date),
            "end_date": _date(project.project_end_date),
            "currency": project.currency,
            "contract_value": (
                str(project.contract_value)
                if project.contract_value not in (None, 0)
                else None
            ),
            "contract_type": project.contract_type or "",
            "commercial_model": project.commercial_model or "",
        },
        "documents": [
            {
                "id": _identifier(document.id),
                "name": document.file_name,
                "document_type": document.document_type,
                "content_hash": (
                    document.search_index.content_hash
                    if hasattr(document, "search_index")
                    else ""
                ),
                "updated_at": document.updated_at.isoformat(),
            }
            for document in documents
        ],
        "sources": segments,
        "customer_demands": _document_customer_demand_rows(project),
        "requirements": requirements,
        # Requirements are the project process/station hierarchy in this solution.
        # Keep an explicit collection so generation prompts do not need to infer that
        # domain meaning from the generic model name.
        "process_stations": requirements,
        "solution_space": _tree_rows(
            ProjectSolutionSpaceItem.objects.filter(project=project),
            "assembly_name",
        ),
        "bill_of_material": _tree_rows(
            BillOfMaterialPart.objects.filter(project=project),
            "name",
            "part_number",
        ),
        "project_workstations": _project_workstation_rows(project),
        "unresolved_decisions": _unresolved_decision_rows(project),
        "opportunities": opportunities,
    }
    return snapshot, canonical_hash(snapshot)


ACCEPTANCE_SOURCE_SCHEMA_VERSION = "2"


def _evidence_references(value):
    """Yield document/segment references from reviewed extraction payloads."""
    if isinstance(value, dict):
        if value.get("document_id") is not None:
            yield {
                "document_id": int(value["document_id"]),
                "segment_id": (
                    int(value["segment_id"])
                    if value.get("segment_id") is not None
                    else None
                ),
                "location": str(value.get("location") or ""),
                "quote": str(value.get("quote") or ""),
            }
        for nested in value.values():
            yield from _evidence_references(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _evidence_references(nested)


def _current_applied_attribute_value(applied_value, owner):
    custom_fields = owner.custom_fields or {}
    return custom_fields.get(applied_value.applied_column.column_key)


def _attribute_definition(applied_column):
    payload = applied_column.applied_payload or {}
    return {
        key: payload[key]
        for key in ("label", "label_en", "label_de", "label_fr", "unit")
        if payload.get(key) not in (None, "")
    }


def acceptance_prerequisite_state(project):
    applied_products = ProductAssemblyAppliedRow.objects.filter(
        run__project=project,
        run__status=ProductAssemblyExtractionRun.Status.APPLIED,
        solution_demand__project=project,
    )
    product_attributes = ProductAttributeAppliedValue.objects.filter(
        applied_column__run__project=project,
        applied_column__run__status=ProductAttributeExtractionRun.Status.APPLIED,
        solution_demand__project=project,
    ).select_related("solution_demand", "applied_column")
    applied_processes = ProcessAssemblyAppliedRow.objects.filter(
        run__project=project,
        run__status=ProcessAssemblyExtractionRun.Status.APPLIED,
        requirement__project=project,
    )
    process_attributes = ProcessAttributeAppliedValue.objects.filter(
        applied_column__run__project=project,
        applied_column__run__status=ProcessAttributeExtractionRun.Status.APPLIED,
        requirement__project=project,
    ).select_related("requirement", "applied_column")
    approved_assemblies = ProjectSolutionSpaceItem.objects.filter(
        project=project,
        source_company_item__isnull=False,
        source_company_item__company_profile=project.company_profile,
    )
    state = {
        "products": applied_products.exists(),
        "product_attributes": any(
            _current_applied_attribute_value(value, value.solution_demand)
            not in (None, "")
            for value in product_attributes
        ),
        "processes": applied_processes.exists(),
        "machinery": approved_assemblies.filter(
            assembly_type="production_lines"
        ).exists(),
        "stations": applied_processes.exists(),
        "process_attributes": any(
            _current_applied_attribute_value(value, value.requirement)
            not in (None, "")
            for value in process_attributes
        ),
    }
    return {
        **state,
        "ready": all(state.values()),
        "missing": [key for key, available in state.items() if not available],
    }


def build_acceptance_source_snapshot(project, request=None):
    """Build the immutable, applied-only source contract for acceptance review."""
    prerequisites = acceptance_prerequisite_state(project)

    product_rows = list(
        ProductAssemblyAppliedRow.objects.filter(
            run__project=project,
            run__status=ProductAssemblyExtractionRun.Status.APPLIED,
            solution_demand__project=project,
        )
        .select_related("solution_demand", "run")
        .order_by("solution_demand__sort_order", "solution_demand_id", "id")
    )
    product_values = list(
        ProductAttributeAppliedValue.objects.filter(
            applied_column__run__project=project,
            applied_column__run__status=ProductAttributeExtractionRun.Status.APPLIED,
            solution_demand__project=project,
        )
        .select_related("solution_demand", "applied_column", "applied_column__run")
        .order_by("applied_column_id", "solution_demand_id", "id")
    )
    process_rows = list(
        ProcessAssemblyAppliedRow.objects.filter(
            run__project=project,
            run__status=ProcessAssemblyExtractionRun.Status.APPLIED,
            requirement__project=project,
        )
        .select_related("requirement", "run")
        .order_by("requirement__sort_order", "requirement_id", "id")
    )
    process_values = list(
        ProcessAttributeAppliedValue.objects.filter(
            applied_column__run__project=project,
            applied_column__run__status=ProcessAttributeExtractionRun.Status.APPLIED,
            requirement__project=project,
        )
        .select_related("requirement", "applied_column", "applied_column__run")
        .order_by("applied_column_id", "requirement_id", "id")
    )
    product_values = [
        value
        for value in product_values
        if _current_applied_attribute_value(value, value.solution_demand)
        not in (None, "")
    ]
    process_values = [
        value
        for value in process_values
        if _current_applied_attribute_value(value, value.requirement)
        not in (None, "")
    ]
    assemblies = list(
        ProjectSolutionSpaceItem.objects.filter(
            project=project,
            source_company_item__isnull=False,
            source_company_item__company_profile=project.company_profile,
        )
        .select_related("source_company_item")
        .order_by("sort_order", "id")
    )

    product_attributes_by_id = {}
    for value in product_values:
        product_attributes_by_id.setdefault(value.solution_demand_id, []).append(
            {
                "proposal_key": value.applied_column.proposal_key,
                "column_key": value.applied_column.column_key,
                "definition": _attribute_definition(value.applied_column),
                "value": _current_applied_attribute_value(
                    value,
                    value.solution_demand,
                ),
            }
        )
    process_attributes_by_id = {}
    for value in process_values:
        process_attributes_by_id.setdefault(value.requirement_id, []).append(
            {
                "proposal_key": value.applied_column.proposal_key,
                "column_key": value.applied_column.column_key,
                "definition": _attribute_definition(value.applied_column),
                "value": _current_applied_attribute_value(
                    value,
                    value.requirement,
                ),
            }
        )

    reviewed_payloads = [
        *(row.applied_payload or {} for row in product_rows),
        *(value.applied_payload or {} for value in product_values),
        *(row.applied_payload or {} for row in process_rows),
        *(value.applied_payload or {} for value in process_values),
    ]
    evidence_by_identity = {}
    for payload in reviewed_payloads:
        for evidence in _evidence_references(payload):
            identity = (evidence["document_id"], evidence["segment_id"])
            evidence_by_identity[identity] = evidence
    evidence_document_ids = {identity[0] for identity in evidence_by_identity}

    project_documents = Document.objects.filter(
        folder__project=project,
        id__in=evidence_document_ids,
    )
    if request is not None:
        accessible_documents = get_accessible_document_queryset(
            request,
            project_id=project.id,
        ).filter(folder__project=project, id__in=evidence_document_ids)
    else:
        accessible_documents = project_documents
    project_document_ids = set(project_documents.values_list("id", flat=True))
    accessible_document_ids = set(accessible_documents.values_list("id", flat=True))
    if project_document_ids != evidence_document_ids:
        raise ValueError("Acceptance evidence references a document outside this project.")
    if accessible_document_ids != evidence_document_ids:
        raise PermissionError("Acceptance evidence includes an inaccessible document.")

    requested_segment_ids = {
        identity[1] for identity in evidence_by_identity if identity[1] is not None
    }
    segment_rows = {
        (segment.document_id, segment.id): segment
        for segment in DocumentContentSegment.objects.filter(
            document_id__in=accessible_document_ids,
            id__in=requested_segment_ids,
        )
    }
    if set(segment_rows) != {
        identity for identity in evidence_by_identity if identity[1] is not None
    }:
        raise ValueError("Acceptance evidence references an unknown source segment.")

    snapshot = {
        "schema_version": ACCEPTANCE_SOURCE_SCHEMA_VERSION,
        "project": {"id": str(project.id), "name": project.project_name},
        "applied_products": [
            {
                "id": str(row.solution_demand_id),
                "proposal_key": row.proposal_key,
                "name": row.solution_demand.subject,
                "number": row.solution_demand.demand_number,
                "attributes": product_attributes_by_id.get(
                    row.solution_demand_id,
                    [],
                ),
            }
            for row in product_rows
        ],
        "product_attributes": [
            {
                "product_id": str(value.solution_demand_id),
                "proposal_key": value.applied_column.proposal_key,
                "column_key": value.applied_column.column_key,
                "definition": _attribute_definition(value.applied_column),
                "value": _current_applied_attribute_value(
                    value,
                    value.solution_demand,
                ),
            }
            for value in product_values
        ],
        "applied_processes": [
            {
                "id": str(row.requirement_id),
                "proposal_key": row.proposal_key,
                "name": row.requirement.name,
                "number": row.requirement.object_number,
                "attributes": process_attributes_by_id.get(
                    row.requirement_id,
                    [],
                ),
            }
            for row in process_rows
        ],
        "process_attributes": [
            {
                "requirement_id": str(value.requirement_id),
                "proposal_key": value.applied_column.proposal_key,
                "column_key": value.applied_column.column_key,
                "definition": _attribute_definition(value.applied_column),
                "value": _current_applied_attribute_value(
                    value,
                    value.requirement,
                ),
            }
            for value in process_values
        ],
        "approved_project_assemblies": [
            {
                "id": str(item.id),
                "company_assembly_id": str(item.source_company_item_id),
                "name": item.assembly_name,
                "assembly_type": item.assembly_type,
                "custom_fields": item.custom_fields or {},
            }
            for item in assemblies
        ],
        "evidence": [
            {
                **reference,
                "document_id": str(reference["document_id"]),
                "segment_id": (
                    str(reference["segment_id"])
                    if reference["segment_id"] is not None
                    else None
                ),
                "source_text": (
                    segment_rows[(reference["document_id"], reference["segment_id"])].text
                    if reference["segment_id"] is not None
                    else ""
                ),
            }
            for _, reference in sorted(evidence_by_identity.items())
        ],
        "prerequisites": prerequisites,
        "project_workstations": [
            {
                key: value
                for key, value in row.items()
                if key not in {"updated_at", "revision_reason"}
            }
            for row in _project_workstation_rows(project)
        ],
    }
    return snapshot, canonical_hash(snapshot), prerequisites
