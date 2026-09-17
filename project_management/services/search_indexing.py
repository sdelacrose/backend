import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from django.conf import settings
from django.contrib.postgres.search import SearchVector
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.utils.html import strip_tags

from project_management.models import (
    ActionItemComment,
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    Document,
    DocumentContentSegment,
    DocumentSearchIndex,
    Equipment,
    EquipmentConfig,
    Invoice,
    Machine,
    MachineConfig,
    MatrixComment,
    ObjectAssessment,
    PartsListConfig,
    ProjectMatrixConfig,
    ProjectSolutionSpaceItem,
    PurchaseOrder,
    Requirement,
    SearchContentSegment,
    SearchFieldValue,
    SearchIndexAttempt,
    SearchIndexJob,
    SearchRecord,
    SearchRelation,
    SolutionDemand,
    SolutionSpaceConfig,
)
from project_management.services.document_indexing import (
    DocumentIndexingError,
    extract_file_content,
    normalize_extracted_text,
    split_search_vector_text,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexedField:
    key: str
    value: Any
    field_ref: str = ""
    field_type: str = "text"
    semantic_id: str = ""
    label_en: str = ""
    label_de: str = ""
    label_fr: str = ""


@dataclass(frozen=True)
class IndexedRelation:
    relation_type: str
    related_type: str
    related_id: int
    label: str = ""


@dataclass
class IndexedPayload:
    company_profile_id: int | None
    project_id: int | None
    freelancer_id: int | None
    title: str
    subtitle: str = ""
    searchable_values: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    fields: list[IndexedField] = field(default_factory=list)
    relations: list[IndexedRelation] = field(default_factory=list)
    source_updated_at: Any = None
    index_status: str = SearchRecord.IndexStatus.SEARCHABLE


SOURCE_MODELS = {
    SearchRecord.SourceType.DOCUMENT: Document,
    SearchRecord.SourceType.OBJECT_ASSESSMENT: ObjectAssessment,
    SearchRecord.SourceType.SOLUTION_DEMAND: SolutionDemand,
    SearchRecord.SourceType.REQUIREMENT: Requirement,
    SearchRecord.SourceType.AVAILABLE_PRODUCT_PART: AvailableProductPart,
    SearchRecord.SourceType.MACHINE: Machine,
    SearchRecord.SourceType.EQUIPMENT: Equipment,
    SearchRecord.SourceType.BOM_PART: BillOfMaterialPart,
    SearchRecord.SourceType.COMPANY_SOLUTION_SPACE: CompanySolutionSpaceItem,
    SearchRecord.SourceType.PROJECT_SOLUTION_SPACE: ProjectSolutionSpaceItem,
    SearchRecord.SourceType.PURCHASE_ORDER: PurchaseOrder,
    SearchRecord.SourceType.INVOICE: Invoice,
    SearchRecord.SourceType.ACTION_ITEM_COMMENT: ActionItemComment,
    SearchRecord.SourceType.MATRIX_COMMENT: MatrixComment,
}


SOURCE_ROUTES = {
    SearchRecord.SourceType.DOCUMENT: {
        "company": "/company-documents",
        "project": "/project-document-management",
    },
    SearchRecord.SourceType.OBJECT_ASSESSMENT: "/project-object-assessment",
    SearchRecord.SourceType.SOLUTION_DEMAND: "/project-system-requirements/customer-demands",
    SearchRecord.SourceType.REQUIREMENT: "/project-system-requirements/technical-requirements",
    SearchRecord.SourceType.AVAILABLE_PRODUCT_PART: "/company-product-services/available-products",
    SearchRecord.SourceType.MACHINE: "/company-product-services/machines",
    SearchRecord.SourceType.EQUIPMENT: "/company-product-services/equipment",
    SearchRecord.SourceType.BOM_PART: "/project-product-services/bill-of-material",
    SearchRecord.SourceType.COMPANY_SOLUTION_SPACE: "/company-product-services/solution-space",
    SearchRecord.SourceType.PROJECT_SOLUTION_SPACE: "/project-product-services/solution-space",
    SearchRecord.SourceType.PURCHASE_ORDER: "/purchase-orders",
    SearchRecord.SourceType.INVOICE: "/billing-management",
    SearchRecord.SourceType.ACTION_ITEM_COMMENT: {
        "company": "/company-action-items",
        "project": "/project-action-items",
    },
}


MATRIX_COMMENT_ROUTES = {
    MatrixComment.TARGET_SOLUTION_DEMAND: SOURCE_ROUTES[SearchRecord.SourceType.SOLUTION_DEMAND],
    MatrixComment.TARGET_REQUIREMENT: SOURCE_ROUTES[SearchRecord.SourceType.REQUIREMENT],
    MatrixComment.TARGET_TRANSPORT: "/project-execution/transport",
    MatrixComment.TARGET_AVAILABLE_PRODUCT_PART: SOURCE_ROUTES[
        SearchRecord.SourceType.AVAILABLE_PRODUCT_PART
    ],
    MatrixComment.TARGET_MACHINE: SOURCE_ROUTES[SearchRecord.SourceType.MACHINE],
    MatrixComment.TARGET_EQUIPMENT: SOURCE_ROUTES[SearchRecord.SourceType.EQUIPMENT],
    MatrixComment.TARGET_BOM_PART: SOURCE_ROUTES[SearchRecord.SourceType.BOM_PART],
    MatrixComment.TARGET_COMPANY_SOLUTION_SPACE: SOURCE_ROUTES[
        SearchRecord.SourceType.COMPANY_SOLUTION_SPACE
    ],
    MatrixComment.TARGET_PROJECT_SOLUTION_SPACE: SOURCE_ROUTES[
        SearchRecord.SourceType.PROJECT_SOLUTION_SPACE
    ],
}


def advanced_search_enabled() -> bool:
    return bool(getattr(settings, "ADVANCED_SEARCH_ENABLED", True))


def source_indexing_enabled(source_type: str) -> bool:
    if not advanced_search_enabled():
        return False
    if source_type == SearchRecord.SourceType.DOCUMENT:
        return True
    if source_type == SearchRecord.SourceType.OBJECT_ASSESSMENT:
        return bool(getattr(settings, "ADVANCED_SEARCH_OBJECT_ASSESSMENTS", True))
    if source_type in {
        SearchRecord.SourceType.ACTION_ITEM_COMMENT,
        SearchRecord.SourceType.MATRIX_COMMENT,
    }:
        return bool(getattr(settings, "ADVANCED_SEARCH_COMMENTS", True))
    return bool(getattr(settings, "ADVANCED_SEARCH_BUSINESS_RECORDS", True))


def serialize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return " ".join(serialize_value(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(serialize_value(item) for item in value)
    return str(value)


def user_label(user) -> str:
    if not user:
        return ""
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return name or getattr(user, "email", "") or str(user)


def object_label(obj) -> str:
    for attribute in (
        "file_name",
        "assembly_name",
        "subject",
        "name",
        "record_title",
        "reference",
        "reference_number",
        "title",
        "company_name",
        "project_name",
        "item_name",
    ):
        value = getattr(obj, attribute, None)
        if value:
            return str(value)
    return str(obj)


def relation_values(manager, relation_type: str, related_type: str) -> list[IndexedRelation]:
    if not manager:
        return []
    return [
        IndexedRelation(relation_type, related_type, related.id, object_label(related))
        for related in manager.all()
    ]


def scope_for(instance) -> tuple[int | None, int | None, int | None]:
    if isinstance(instance, ActionItemComment):
        instance = instance.action_item
    milestone = getattr(instance, "milestone", None)
    if milestone and milestone.project_id:
        return milestone.project.company_profile_id, milestone.project_id, None
    parent_action_item = getattr(instance, "parent_action_item", None)
    if parent_action_item:
        return scope_for(parent_action_item)
    project = getattr(instance, "project", None)
    if project:
        return project.company_profile_id, project.id, None
    company_profile_id = getattr(instance, "company_profile_id", None)
    freelancer_id = getattr(instance, "freelancer_id", None)
    folder = getattr(instance, "folder", None)
    if folder:
        if folder.project_id:
            return folder.project.company_profile_id, folder.project_id, None
        return folder.company_profile_id, None, folder.freelancer_id
    return company_profile_id, None, freelancer_id


def source_route(source_type: str, payload: IndexedPayload) -> str:
    route = SOURCE_ROUTES.get(source_type, "")
    if isinstance(route, dict):
        route = route["project" if payload.project_id else "company"]
    if source_type == SearchRecord.SourceType.MATRIX_COMMENT:
        route = MATRIX_COMMENT_ROUTES.get(payload.metadata.get("parent_type"), "")
    if not route:
        return ""
    parameters = [
        f"searchResultType={source_type}",
        f"id={payload.metadata.get('target_id') or payload.metadata.get('id')}",
    ]
    if payload.project_id:
        parameters.append(f"project_id={payload.project_id}")
    separator = "&" if "?" in route else "?"
    return f"{route}{separator}{'&'.join(parameters)}"


def search_record_route(record: SearchRecord) -> str:
    """Build a canonical URL without relying on potentially stale indexed metadata."""
    return source_route(
        record.source_type,
        IndexedPayload(
            company_profile_id=record.company_profile_id,
            project_id=record.project_id,
            freelancer_id=record.freelancer_id,
            title=record.title,
            metadata=dict(record.metadata or {}),
        ),
    )


def config_columns(instance, source_type: str) -> list[dict[str, Any]]:
    if source_type in {SearchRecord.SourceType.SOLUTION_DEMAND, SearchRecord.SourceType.REQUIREMENT}:
        config = ProjectMatrixConfig.objects.filter(project_id=instance.project_id).first()
        if not config:
            return []
        return config.demand_columns if source_type == SearchRecord.SourceType.SOLUTION_DEMAND else config.requirement_columns
    if source_type in {SearchRecord.SourceType.AVAILABLE_PRODUCT_PART, SearchRecord.SourceType.BOM_PART}:
        filters = (
            {"company_profile_id": instance.company_profile_id, "project__isnull": True}
            if source_type == SearchRecord.SourceType.AVAILABLE_PRODUCT_PART
            else {"project_id": instance.project_id}
        )
        config = PartsListConfig.objects.filter(**filters).first()
        if not config:
            return []
        return config.available_product_columns if source_type == SearchRecord.SourceType.AVAILABLE_PRODUCT_PART else config.bom_columns
    if source_type == SearchRecord.SourceType.MACHINE:
        config = MachineConfig.objects.filter(
            company_profile_id=instance.company_profile_id,
        ).first()
        return config.machine_columns if config else []
    if source_type == SearchRecord.SourceType.EQUIPMENT:
        config = EquipmentConfig.objects.filter(
            company_profile_id=instance.company_profile_id,
        ).first()
        return config.equipment_columns if config else []
    if source_type in {SearchRecord.SourceType.COMPANY_SOLUTION_SPACE, SearchRecord.SourceType.PROJECT_SOLUTION_SPACE}:
        filters = (
            {"company_profile_id": instance.company_profile_id, "project__isnull": True}
            if source_type == SearchRecord.SourceType.COMPANY_SOLUTION_SPACE
            else {"project_id": instance.project_id}
        )
        config = SolutionSpaceConfig.objects.filter(**filters).first()
        return config.columns if config else []
    return []


def configurable_fields(instance, source_type: str) -> list[IndexedField]:
    values = dict(getattr(instance, "custom_fields", None) or {})
    if source_type in {
        SearchRecord.SourceType.COMPANY_SOLUTION_SPACE,
        SearchRecord.SourceType.PROJECT_SOLUTION_SPACE,
    }:
        values.update(
            {
                "assembly_name": instance.assembly_name,
                "required_skills": instance.required_skills,
                "provided_skills": instance.provided_skills,
                "responsible_engineer": user_label(instance.responsible_engineer),
                "year_of_construction": instance.year_of_construction,
            }
        )
    definitions = {
        str(column.get("key") or column.get("id") or ""): column
        for column in config_columns(instance, source_type)
    }
    owner = getattr(instance, "project_id", None) or getattr(instance, "company_profile_id", None) or "global"
    fields = []
    for key, value in values.items():
        definition = definitions.get(str(key), {})
        fields.append(
            IndexedField(
                key=str(key),
                value=value,
                field_ref=f"{source_type}:{owner}:{key}",
                field_type=str(definition.get("type") or "text"),
                semantic_id=str(definition.get("idta_semantic_id") or ""),
                label_en=str(definition.get("label_en") or definition.get("label") or key),
                label_de=str(definition.get("label_de") or definition.get("label") or key),
                label_fr=str(definition.get("label_fr") or definition.get("label") or key),
            )
        )
    return fields


def common_matrix_payload(instance, source_type: str, title: str, number: str = "") -> IndexedPayload:
    company_id, project_id, freelancer_id = scope_for(instance)
    fields = configurable_fields(instance, source_type)
    relations = []
    relations += relation_values(getattr(instance, "documents", None), "linked", "document")
    relations += relation_values(getattr(instance, "object_assessments", None), "linked", "object_assessment")
    relations += relation_values(getattr(instance, "requirements", None), "linked", "requirement")
    relations += relation_values(getattr(instance, "solution_demands", None), "linked", "solution_demand")
    relations += relation_values(getattr(instance, "linked_folders", None), "linked", "folder")
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=title,
        subtitle=number,
        searchable_values=[title, number, *(field.value for field in fields)],
        fields=fields,
        relations=relations,
        metadata={
            "id": instance.id,
            "number": number,
            "status": getattr(instance, "status", ""),
            "created_at": getattr(instance, "created_at", None),
            "updated_at": getattr(instance, "updated_at", None),
        },
        source_updated_at=getattr(instance, "updated_at", None),
    )


def document_payload(instance: Document) -> IndexedPayload:
    company_id, project_id, freelancer_id = scope_for(instance)
    metadata_values = [
        instance.file_name,
        instance.full_document_number,
        instance.identification_number,
        instance.document_type,
        instance.status,
        instance.category,
        instance.discipline,
        instance.folder.name if instance.folder_id else "",
        user_label(instance.author),
    ]
    relations = []
    relation_specs = (
        ("action_items", "action_item"),
        ("object_assesments", "object_assessment"),
        ("solution_demands", "solution_demand"),
        ("requirements", "requirement"),
        ("transports", "transport"),
        ("available_product_parts", "available_product_part"),
        ("machines", "machine"),
        ("equipments", "equipment"),
        ("bom_parts", "bom_part"),
        ("company_solution_space_items", "company_solution_space"),
        ("project_solution_space_items", "project_solution_space"),
        ("purchase_orders", "purchase_order"),
        ("billing_invoices", "invoice"),
    )
    for relation_name, related_type in relation_specs:
        relations += relation_values(getattr(instance, relation_name, None), "linked", related_type)
    if instance.folder_id:
        relations.append(
            IndexedRelation("contained_in", "folder", instance.folder_id, instance.folder.name)
        )
    try:
        index = instance.search_index
    except DocumentSearchIndex.DoesNotExist:
        index = None
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=instance.file_name or object_label(instance),
        subtitle=instance.full_document_number or instance.identification_number or "",
        searchable_values=metadata_values,
        relations=relations,
        metadata={
            "id": instance.id,
            "document_type": instance.document_type,
            "status": instance.status,
            "author": user_label(instance.author),
            "author_id": instance.author_id,
            "uploaded_at": instance.uploaded_at,
            "modified_at": instance.updated_at,
            "folder_id": instance.folder_id,
            "folder_name": instance.folder.name if instance.folder_id else "",
        },
        source_updated_at=instance.updated_at,
        index_status=getattr(index, "status", SearchRecord.IndexStatus.PENDING),
    )


def object_assessment_payload(instance: ObjectAssessment) -> IndexedPayload:
    company_id, project_id, freelancer_id = scope_for(instance)
    note_text = normalize_extracted_text(strip_tags(instance.notes_content_html or ""))
    relations = relation_values(instance.documents, "linked", "document")
    relation_specs = (
        ("solution_demands", "solution_demand"),
        ("requirements", "requirement"),
        ("transports", "transport"),
        ("available_product_parts", "available_product_part"),
        ("machines", "machine"),
        ("equipments", "equipment"),
        ("bom_parts", "bom_part"),
        ("project_solution_space_items", "project_solution_space"),
    )
    for relation_name, related_type in relation_specs:
        relations += relation_values(
            getattr(instance, relation_name, None),
            "linked",
            related_type,
        )
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=instance.record_title or instance.file_name or instance.record_type,
        subtitle=instance.record_type,
        searchable_values=[
            instance.record_title,
            instance.record_type,
            instance.description,
            note_text,
            instance.document_type,
            instance.identification_number,
            instance.status,
            instance.file_name,
            user_label(instance.author),
        ],
        relations=relations,
        metadata={
            "id": instance.id,
            "record_type": instance.record_type,
            "status": instance.status,
            "author": user_label(instance.author),
            "author_id": instance.author_id,
            "created_at": instance.created_at,
            "updated_at": instance.updated_at,
            "has_file": bool(instance.file),
            "note_text": note_text,
        },
        source_updated_at=instance.updated_at,
        index_status=SearchRecord.IndexStatus.PENDING if instance.file else SearchRecord.IndexStatus.SEARCHABLE,
    )


def solution_space_payload(instance, source_type: str) -> IndexedPayload:
    payload = common_matrix_payload(instance, source_type, instance.assembly_name)
    payload.searchable_values.extend(
        [
            instance.required_skills,
            instance.provided_skills,
            user_label(instance.responsible_engineer),
            instance.year_of_construction,
            instance.assembly_type,
        ]
    )
    if hasattr(instance, "projects"):
        payload.searchable_values.extend(project.project_name for project in instance.projects.all())
        payload.relations += relation_values(instance.projects, "used_in", "project")
    customers = list(instance.customers.all())
    payload.searchable_values.extend(object_label(customer) for customer in customers)
    payload.relations += [
        IndexedRelation("customer", "contact_company", customer.id, object_label(customer))
        for customer in customers
    ]
    payload.metadata.update(
        {
            "required_skills": instance.required_skills,
            "provided_skills": instance.provided_skills,
            "responsible_engineer": user_label(instance.responsible_engineer),
            "year_of_construction": instance.year_of_construction,
            "assembly_type": instance.assembly_type,
            "customers": [object_label(customer) for customer in customers],
        }
    )
    return payload


def purchase_order_payload(instance: PurchaseOrder) -> IndexedPayload:
    company_id, project_id, freelancer_id = scope_for(instance)
    line_items = list(instance.line_items.all())
    relations = relation_values(instance.documents, "document", "document")
    relations += relation_values(instance.invoices, "invoice", "invoice")
    relations += relation_values(instance.linked_folders, "linked", "folder")
    relations += relation_values(instance.change_notices, "change_notice", "purchase_order")
    relations += [
        IndexedRelation(
            "line_item_part",
            "available_product_part",
            item.available_product_id,
            object_label(item.available_product),
        )
        for item in line_items
        if item.available_product_id
    ]
    if instance.source_po_id:
        relations.append(IndexedRelation("source_po", "purchase_order", instance.source_po_id, object_label(instance.source_po)))
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=instance.reference,
        subtitle=instance.customer_reference or instance.get_document_type_display(),
        searchable_values=[
            instance.reference,
            instance.customer_reference,
            instance.po_type,
            instance.document_type,
            instance.status,
            instance.currency,
            instance.notes,
            object_label(instance.receiving_user) if instance.receiving_user else "",
            object_label(instance.receiving_company) if instance.receiving_company else "",
            object_label(instance.emitting_user) if instance.emitting_user else "",
            object_label(instance.emitting_company) if instance.emitting_company else "",
            *(f"{item.line_number} {item.item_name} {item.quantity} {item.rate} {item.total}" for item in line_items),
        ],
        relations=relations,
        metadata={
            "id": instance.id,
            "reference": instance.reference,
            "document_type": instance.document_type,
            "status": instance.status,
            "date": instance.po_date,
            "currency": instance.currency,
            "amount": str(instance.amount_incl_vat),
            "line_item_count": len(line_items),
        },
        source_updated_at=instance.updated_at,
    )


def invoice_payload(instance: Invoice) -> IndexedPayload:
    company_id, project_id, freelancer_id = scope_for(instance)
    line_items = list(instance.line_items.all())
    relations = relation_values(instance.documents, "document", "document")
    if instance.purchase_order_id:
        relations.append(IndexedRelation("purchase_order", "purchase_order", instance.purchase_order_id, object_label(instance.purchase_order)))
    if instance.source_invoice_id:
        relations.append(
            IndexedRelation(
                "source_invoice",
                "invoice",
                instance.source_invoice_id,
                object_label(instance.source_invoice),
            )
        )
    if instance.replaced_invoice_id:
        relations.append(
            IndexedRelation(
                "replaced_invoice",
                "invoice",
                instance.replaced_invoice_id,
                object_label(instance.replaced_invoice),
            )
        )
    relations += relation_values(instance.credit_notes, "credit_note", "invoice")
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=instance.reference_number or f"Invoice {instance.id}",
        subtitle=instance.get_document_type_display(),
        searchable_values=[
            instance.reference_number,
            instance.document_type,
            instance.status,
            instance.notes,
            instance.invoice_date,
            instance.due_date,
            instance.grand_total,
            object_label(instance.reciepient_user) if instance.reciepient_user else "",
            object_label(instance.reciepient_company) if instance.reciepient_company else "",
            *(f"{item.description} {item.quantity} {item.price} {item.vat_amount}" for item in line_items),
        ],
        relations=relations,
        metadata={
            "id": instance.id,
            "reference": instance.reference_number,
            "document_type": instance.document_type,
            "status": instance.status,
            "date": instance.invoice_date,
            "due_date": instance.due_date,
            "amount": instance.grand_total,
            "line_item_count": len(line_items),
        },
        source_updated_at=instance.updated_at,
    )


def action_item_comment_payload(instance: ActionItemComment) -> IndexedPayload | None:
    if instance.is_deleted:
        return None
    company_id, project_id, freelancer_id = scope_for(instance.action_item)
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=f"Comment on {object_label(instance.action_item)}",
        subtitle=user_label(instance.sender),
        searchable_values=[instance.content, instance.linked_objects],
        relations=[IndexedRelation("parent", "action_item", instance.action_item_id, object_label(instance.action_item))],
        metadata={
            "id": instance.id,
            "target_id": instance.action_item_id,
            "parent_type": "action_item",
            "sender": user_label(instance.sender),
            "sender_id": instance.sender_id,
            "created_at": instance.created_at,
        },
        source_updated_at=instance.updated_at,
    )


def matrix_comment_target(instance: MatrixComment):
    return (
        instance.solution_demand
        or instance.requirement
        or instance.transport
        or instance.available_product_part
        or instance.machine
        or instance.equipment
        or instance.bom_part
        or instance.company_solution_space_item
        or instance.project_solution_space_item
    )


def matrix_comment_payload(instance: MatrixComment) -> IndexedPayload | None:
    if instance.is_deleted:
        return None
    target = matrix_comment_target(instance)
    if not target:
        return None
    company_id, project_id, freelancer_id = scope_for(target)
    return IndexedPayload(
        company_profile_id=company_id,
        project_id=project_id,
        freelancer_id=freelancer_id,
        title=f"Comment on {object_label(target)}",
        subtitle=user_label(instance.sender),
        searchable_values=[instance.content, instance.linked_objects],
        relations=[IndexedRelation("parent", instance.target_type, target.id, object_label(target))],
        metadata={
            "id": instance.id,
            "target_id": target.id,
            "parent_type": instance.target_type,
            "sender": user_label(instance.sender),
            "sender_id": instance.sender_id,
            "created_at": instance.created_at,
        },
        source_updated_at=instance.updated_at,
    )


def build_payload(instance, source_type: str) -> IndexedPayload | None:
    if source_type == SearchRecord.SourceType.DOCUMENT:
        return document_payload(instance)
    if source_type == SearchRecord.SourceType.OBJECT_ASSESSMENT:
        return object_assessment_payload(instance)
    if source_type == SearchRecord.SourceType.SOLUTION_DEMAND:
        return common_matrix_payload(instance, source_type, instance.subject, instance.demand_number or "")
    if source_type == SearchRecord.SourceType.REQUIREMENT:
        payload = common_matrix_payload(instance, source_type, instance.name, instance.object_number or "")
        payload.searchable_values.append(instance.skill)
        payload.metadata["skill"] = instance.skill
        return payload
    if source_type == SearchRecord.SourceType.AVAILABLE_PRODUCT_PART:
        return common_matrix_payload(instance, source_type, instance.name, instance.part_number or "")
    if source_type in {SearchRecord.SourceType.MACHINE, SearchRecord.SourceType.EQUIPMENT}:
        payload = common_matrix_payload(instance, source_type, instance.name)
        if source_type == SearchRecord.SourceType.MACHINE:
            projects = list(instance.projects.all())
            payload.searchable_values.extend(project.project_name for project in projects)
            payload.relations += [
                IndexedRelation("used_in", "project", project.id, project.project_name)
                for project in projects
            ]
        return payload
    if source_type == SearchRecord.SourceType.BOM_PART:
        payload = common_matrix_payload(
            instance,
            source_type,
            instance.name,
            instance.part_number or "",
        )
        if instance.available_product_id:
            payload.relations.append(
                IndexedRelation(
                    "available_product",
                    "available_product_part",
                    instance.available_product_id,
                    object_label(instance.available_product),
                )
            )
        return payload
    if source_type in {SearchRecord.SourceType.COMPANY_SOLUTION_SPACE, SearchRecord.SourceType.PROJECT_SOLUTION_SPACE}:
        return solution_space_payload(instance, source_type)
    if source_type == SearchRecord.SourceType.PURCHASE_ORDER:
        return purchase_order_payload(instance)
    if source_type == SearchRecord.SourceType.INVOICE:
        return invoice_payload(instance)
    if source_type == SearchRecord.SourceType.ACTION_ITEM_COMMENT:
        return action_item_comment_payload(instance)
    if source_type == SearchRecord.SourceType.MATRIX_COMMENT:
        return matrix_comment_payload(instance)
    raise ValueError(f"Unsupported search source type: {source_type}")


def json_safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(metadata, default=str))


def build_content_segment_objects(
    record: SearchRecord,
    segments: Iterable[tuple[str, int, str, str, str, float | None]],
) -> list[SearchContentSegment]:
    objects = []
    for segment_type, sequence, location_label, text, attachment_name, confidence in segments:
        chunks = split_search_vector_text(text)
        for chunk_number, chunk in enumerate(chunks, start=1):
            chunk_label = location_label
            if len(chunks) > 1:
                base_label = location_label or "Content"
                chunk_label = f"{base_label} ({chunk_number}/{len(chunks)})"[:255]
            objects.append(
                SearchContentSegment(
                    record=record,
                    segment_type=segment_type,
                    sequence=sequence,
                    location_label=chunk_label,
                    text=chunk,
                    attachment_name=attachment_name,
                    ocr_confidence=confidence,
                )
            )
    return objects


def index_object_assessment_content(record: SearchRecord, instance: ObjectAssessment) -> None:
    SearchContentSegment.objects.filter(record=record).delete()
    segments = []
    note_text = normalize_extracted_text(strip_tags(instance.notes_content_html or ""))
    if note_text:
        segments.append(("note", 1, "Notes", note_text, "", None))
    if instance.description:
        description = normalize_extracted_text(instance.description)
        if description:
            segments.append(("note", 2, "Description", description, "", None))

    if instance.file:
        try:
            extracted, metadata = extract_file_content(
                instance.file,
                instance.file_name or instance.record_title or instance.file.name,
            )
            for segment in extracted:
                text = normalize_extracted_text(segment.text)
                if text:
                    segments.append(
                        (
                            segment.segment_type,
                            segment.sequence + 10,
                            segment.location_label,
                            text,
                            segment.attachment_name,
                            segment.ocr_confidence,
                        )
                    )
            record.mime_type = metadata.get("mime_type", "")
            record.language = metadata.get("language", "")
            record.extraction_method = metadata.get("extraction_method", "")
            record.page_count = metadata.get("page_count", len(extracted))
            record.index_status = (
                SearchRecord.IndexStatus.PARTIAL
                if metadata.get("partial") or metadata.get("metadata_only")
                else SearchRecord.IndexStatus.SEARCHABLE
            )
        except DocumentIndexingError as exc:
            record.index_status = SearchRecord.IndexStatus.PARTIAL if segments else SearchRecord.IndexStatus.FAILED
            record.error = str(exc)[:2000]
            record.failure_category = exc.category
    else:
        record.index_status = SearchRecord.IndexStatus.SEARCHABLE

    SearchContentSegment.objects.bulk_create(
        build_content_segment_objects(record, segments),
        batch_size=100,
    )
    SearchContentSegment.objects.filter(record=record).update(
        search_vector=SearchVector("text", config="simple", weight="C")
    )
    record.save()


def index_document_content(record: SearchRecord, instance: Document) -> None:
    """Copy existing OCR/native segments into the generalized derived index."""
    SearchContentSegment.objects.filter(record=record).delete()
    source_segments = DocumentContentSegment.objects.filter(document=instance).order_by(
        "sequence",
        "id",
    )
    SearchContentSegment.objects.bulk_create(
        build_content_segment_objects(
            record,
            (
                (
                    segment.segment_type,
                    segment.sequence,
                    segment.location_label,
                    segment.text,
                    segment.attachment_name,
                    segment.ocr_confidence,
                )
                for segment in source_segments.iterator(chunk_size=200)
            ),
        ),
        batch_size=200,
    )
    SearchContentSegment.objects.filter(record=record).update(
        search_vector=SearchVector("text", config="simple", weight="C")
    )
    try:
        source_index = instance.search_index
    except DocumentSearchIndex.DoesNotExist:
        record.index_status = SearchRecord.IndexStatus.PENDING
    else:
        record.index_status = source_index.status
        record.content_hash = source_index.content_hash
        record.mime_type = source_index.mime_type
        record.language = source_index.language
        record.extraction_method = source_index.extraction_method
        record.page_count = source_index.page_count
        record.parser_version = source_index.parser_version
        record.error = source_index.error
        record.failure_category = source_index.failure_category
    record.save()


@transaction.atomic
def index_source(source_type: str, source_id: int) -> SearchRecord | None:
    model = SOURCE_MODELS.get(source_type)
    if not model:
        raise ValueError(f"Unsupported search source type: {source_type}")
    try:
        instance = model.objects.get(pk=source_id)
    except model.DoesNotExist:
        SearchRecord.objects.filter(source_type=source_type, source_id=source_id).delete()
        return None

    payload = build_payload(instance, source_type)
    if payload is None:
        SearchRecord.objects.filter(source_type=source_type, source_id=source_id).delete()
        return None
    payload.metadata.setdefault("id", source_id)
    payload.metadata["url"] = source_route(source_type, payload)
    searchable_text = normalize_extracted_text(
        " ".join(serialize_value(value) for value in payload.searchable_values if value is not None)
    )
    record, _ = SearchRecord.objects.update_or_create(
        source_type=source_type,
        source_id=source_id,
        defaults={
            "company_profile_id": payload.company_profile_id,
            "project_id": payload.project_id,
            "freelancer_id": payload.freelancer_id,
            "title": payload.title,
            "subtitle": payload.subtitle,
            "searchable_text": searchable_text,
            "metadata": json_safe_metadata(payload.metadata),
            "index_status": payload.index_status,
            "source_updated_at": payload.source_updated_at,
            "indexed_at": timezone.now(),
            "error": "",
            "failure_category": "",
        },
    )
    SearchRecord.objects.filter(pk=record.pk).update(
        search_vector=(
            SearchVector("title", config="simple", weight="A")
            + SearchVector("subtitle", config="simple", weight="B")
            + SearchVector("searchable_text", config="simple", weight="C")
        )
    )
    SearchFieldValue.objects.filter(record=record).delete()
    field_values = []
    for indexed_field in payload.fields:
        value_text = normalize_extracted_text(serialize_value(indexed_field.value))
        field_values.append(
            SearchFieldValue(
                record=record,
                field_ref=indexed_field.field_ref or f"{source_type}:{indexed_field.key}",
                field_key=indexed_field.key,
                field_type=indexed_field.field_type,
                semantic_id=indexed_field.semantic_id,
                label_en=indexed_field.label_en,
                label_de=indexed_field.label_de,
                label_fr=indexed_field.label_fr,
                value_text=value_text,
                value_normalized=value_text.casefold(),
            )
        )
    SearchFieldValue.objects.bulk_create(field_values, batch_size=100)
    SearchFieldValue.objects.filter(record=record).update(
        search_vector=SearchVector("value_text", config="simple", weight="B")
    )
    SearchRelation.objects.filter(record=record).delete()
    deduplicated_relations = {
        (relation.relation_type, relation.related_type, relation.related_id): relation
        for relation in payload.relations
        if relation.related_id
    }
    SearchRelation.objects.bulk_create(
        [
            SearchRelation(
                record=record,
                relation_type=relation.relation_type,
                related_type=relation.related_type,
                related_id=relation.related_id,
                label=relation.label,
            )
            for relation in deduplicated_relations.values()
        ],
        batch_size=100,
    )
    if source_type == SearchRecord.SourceType.DOCUMENT:
        index_document_content(record, instance)
    elif source_type == SearchRecord.SourceType.OBJECT_ASSESSMENT:
        index_object_assessment_content(record, instance)
    return record


def enqueue_search_indexing(source_type: str, source_id: int) -> SearchIndexJob | None:
    if not source_indexing_enabled(source_type) or source_type not in SOURCE_MODELS:
        return None
    active = SearchIndexJob.objects.filter(
        source_type=source_type,
        source_id=source_id,
        status__in=[
            SearchIndexJob.Status.QUEUED,
            SearchIndexJob.Status.RUNNING,
            SearchIndexJob.Status.RETRYING,
        ],
    ).first()
    if active:
        return active
    try:
        with transaction.atomic():
            job = SearchIndexJob.objects.create(
                source_type=source_type,
                source_id=source_id,
                max_attempts=int(getattr(settings, "ADVANCED_SEARCH_MAX_ATTEMPTS", 3)),
            )
    except IntegrityError:
        return SearchIndexJob.objects.filter(
            source_type=source_type,
            source_id=source_id,
            status__in=[
                SearchIndexJob.Status.QUEUED,
                SearchIndexJob.Status.RUNNING,
                SearchIndexJob.Status.RETRYING,
            ],
        ).first()
    transaction.on_commit(lambda: dispatch_search_indexing_job(job.id))
    return job


def dispatch_search_indexing_job(job_id: int) -> None:
    try:
        from project_management.tasks import process_search_index_job

        job = SearchIndexJob.objects.filter(id=job_id).only("available_at").first()
        if not job:
            return
        process_search_index_job.apply_async(
            args=[job_id],
            queue=getattr(settings, "ADVANCED_SEARCH_QUEUE", "business_search_indexing"),
            eta=job.available_at if job.available_at > timezone.now() else None,
        )
    except Exception:
        logger.exception("Failed to dispatch search indexing job %s", job_id)


def process_search_job(job_id: int) -> None:
    with transaction.atomic():
        job = SearchIndexJob.objects.select_for_update().filter(id=job_id).first()
        if not job or job.status in {SearchIndexJob.Status.COMPLETED, SearchIndexJob.Status.CANCELLED}:
            return
        if job.status == SearchIndexJob.Status.RUNNING:
            return
        job.status = SearchIndexJob.Status.RUNNING
        job.started_at = timezone.now()
        job.attempt_count = F("attempt_count") + 1
        job.save(update_fields=["status", "started_at", "attempt_count", "updated_at"])
    job.refresh_from_db()
    started_at = time.monotonic()
    try:
        record = index_source(job.source_type, job.source_id)
        job.status = SearchIndexJob.Status.COMPLETED
        job.completed_at = timezone.now()
        job.error = ""
        job.failure_category = ""
        job.save(update_fields=["status", "completed_at", "error", "failure_category", "updated_at"])
        SearchIndexAttempt.objects.create(
            job=job,
            source_type=job.source_type,
            source_id=job.source_id,
            status=SearchIndexJob.Status.COMPLETED,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            indexed_text_size=len(record.searchable_text) if record else 0,
            parser=record.parser_version if record else "",
        )
    except Exception as exc:
        should_retry = job.attempt_count < job.max_attempts
        job.status = SearchIndexJob.Status.RETRYING if should_retry else SearchIndexJob.Status.FAILED
        job.error = str(exc)[:2000]
        job.failure_category = "unexpected_error"
        job.available_at = timezone.now() + timezone.timedelta(minutes=min(30, 2 ** job.attempt_count))
        job.save(update_fields=["status", "error", "failure_category", "available_at", "updated_at"])
        SearchIndexAttempt.objects.create(
            job=job,
            source_type=job.source_type,
            source_id=job.source_id,
            status=job.status,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            failure_category=job.failure_category,
            error=job.error,
        )
        if should_retry:
            dispatch_search_indexing_job(job.id)
        else:
            logger.exception("Search indexing failed for %s:%s", job.source_type, job.source_id)


def dispatch_ready_search_jobs(*, limit: int = 100) -> int:
    ready_jobs = SearchIndexJob.objects.filter(
        status__in=[SearchIndexJob.Status.QUEUED, SearchIndexJob.Status.RETRYING],
        available_at__lte=timezone.now(),
    ).order_by("available_at", "id")[:limit]
    dispatched = 0
    for job in ready_jobs:
        dispatch_search_indexing_job(job.id)
        dispatched += 1
    return dispatched


def source_fingerprint(source_type: str, source_id: int) -> str:
    return hashlib.sha256(f"{source_type}:{source_id}".encode("utf-8")).hexdigest()


def backfill_search_records(*, source_types: Iterable[str] | None = None, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
    selected_types = [
        source_type
        for source_type in (source_types or SOURCE_MODELS.keys())
        if source_indexing_enabled(source_type)
    ]
    queued = 0
    processed = 0
    next_cursors = {}
    for source_type in selected_types:
        model = SOURCE_MODELS.get(source_type)
        if not model:
            continue
        ids = list(model.objects.filter(id__gt=after_id).order_by("id").values_list("id", flat=True)[:limit])
        for source_id in ids:
            processed += 1
            if enqueue_search_indexing(source_type, source_id):
                queued += 1
        next_cursors[source_type] = ids[-1] if len(ids) == limit else None
    next_cursor = next_cursors.get(selected_types[0]) if len(selected_types) == 1 else None
    return {
        "processed": processed,
        "queued": queued,
        "next_after_id": next_cursor,
        "next_after_by_source": next_cursors,
    }
