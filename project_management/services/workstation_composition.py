import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models.project import CompanySolutionSpaceItem, Project, ProjectSolutionSpaceItem
from project_management.models.workstation import (
    ProjectWorkstation,
    ProjectWorkstationNode,
    WorkstationRevision,
    WorkstationSnapshot,
)


@dataclass(frozen=True)
class WorkstationCreationResult:
    workstation: ProjectWorkstation
    created: bool


@transaction.atomic
def create_blank_workstation(
    *, project, actor, idempotency_key, name, source=None, source_requirement=None,
):
    name = str(name or "").strip()
    if not name or len(name) > 255:
        raise ValidationError({"name": "Enter an assembly name of at most 255 characters."})
    if source is not None or source_requirement is not None:
        raise ValidationError({"source": "A new assembly must not reference a catalog source or generated requirement."})
    Project.objects.select_for_update().get(pk=project.pk)
    existing = ProjectWorkstation.objects.filter(project=project, idempotency_key=idempotency_key).first()
    if existing:
        if existing.source_process_assembly_id or existing.name != name:
            raise ValidationError({"idempotency_key": "This request key was already used for another assembly."})
        return WorkstationCreationResult(existing, False)
    content = {
        "source_process_assembly_id": None,
        "nodes": [{
            "source_company_item_id": None,
            "parent_source_company_item_id": None,
            "name": name,
            "assembly_type": CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            "level": 1,
            "sort_order": 0,
            "quantity": "1",
            "unit": "",
            "required_skills": [],
            "provided_skills": [],
            "custom_fields": {},
            "approval_status": "open",
        }],
    }
    fingerprint = _content_hash(content)
    workstation = ProjectWorkstation.objects.create(
        project=project, name=name, template_hash=fingerprint,
        idempotency_key=idempotency_key, created_by=actor, modified_by=actor,
    )
    revision = WorkstationRevision.objects.create(
        workstation=workstation, number=1,
        reason="Created a new engineering assembly without a catalog source.",
        source_hash=fingerprint, created_by=actor,
    )
    ProjectWorkstationNode.objects.create(
        workstation=workstation, revision=revision, name=name,
        assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        role=ProjectWorkstationNode.ROLE_ADDED, quantity=1, level=1,
        sort_order=0, created_by=actor,
    )
    WorkstationSnapshot.objects.create(revision=revision, content=content, content_hash=fingerprint)
    return WorkstationCreationResult(workstation, True)


def _quantity_for(item):
    raw_quantity = (item.custom_fields or {}).get("quantity", 1)
    try:
        quantity = Decimal(str(raw_quantity))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError(
            {"source": f"Assembly {item.id} has an invalid quantity."},
        ) from exc
    if quantity <= 0:
        raise ValidationError(
            {"source": f"Assembly {item.id} must have a quantity greater than zero."},
        )
    return quantity


def _source_tree(source):
    company_items = list(
        CompanySolutionSpaceItem.objects.filter(
            company_profile_id=source.company_profile_id,
        ).order_by("level", "sort_order", "id")
    )
    children_by_parent = {}
    for item in company_items:
        children_by_parent.setdefault(item.parent_id, []).append(item)

    ordered = []
    active_path = set()
    visited = set()

    def visit(item, relative_level):
        if item.id in active_path or item.id in visited:
            raise ValidationError({"source": "The Process Assembly hierarchy contains a cycle."})
        if relative_level > CompanySolutionSpaceItem.MAX_LEVEL:
            raise ValidationError(
                {
                    "source": (
                        "The Process Assembly hierarchy exceeds the supported "
                        f"depth of {CompanySolutionSpaceItem.MAX_LEVEL}."
                    ),
                },
            )
        active_path.add(item.id)
        visited.add(item.id)
        ordered.append((item, relative_level, _quantity_for(item)))
        for child in children_by_parent.get(item.id, []):
            visit(child, relative_level + 1)
        active_path.remove(item.id)

    visit(source, 1)
    return ordered


def _snapshot_content(source, ordered_items):
    source_ids = {item.id for item, _level, _quantity in ordered_items}
    nodes = []
    for item, level, quantity in ordered_items:
        nodes.append(
            {
                "source_company_item_id": item.id,
                "parent_source_company_item_id": (
                    item.parent_id if item.parent_id in source_ids else None
                ),
                "name": item.assembly_name,
                "assembly_type": item.assembly_type,
                "level": level,
                "sort_order": item.sort_order,
                "quantity": str(quantity.normalize()),
                "unit": str((item.custom_fields or {}).get("unit") or ""),
                "required_skills": list(item.required_skills or []),
                "provided_skills": list(item.provided_skills or []),
                "custom_fields": dict(item.custom_fields or {}),
                "approval_status": "open",
            }
        )
    return {
        "source_process_assembly_id": source.id,
        "nodes": nodes,
    }


def _content_hash(content):
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@transaction.atomic
def create_project_workstation(*, project, source, actor, idempotency_key, name="", source_requirement=None):
    if source_requirement is not None:
        if source_requirement.project_id != project.pk or not ProjectSolutionSpaceItem.objects.filter(
            project=project, source_company_item=source, requirements=source_requirement,
        ).exists():
            raise ValidationError({"source_requirement": "Select a Requirement linked to this project's Process Assembly."})
        name = source_requirement.name
        idempotency_key = f"requirement:{source_requirement.pk}"
    name = str(name or "").strip()
    if len(name) > 255:
        raise ValidationError({"name": "The workstation name is too long."})
    if not isinstance(project, Project) or not project.pk:
        raise ValidationError({"project": "A saved project is required."})
    if not idempotency_key or not str(idempotency_key).strip():
        raise ValidationError({"idempotency_key": "An idempotency key is required."})
    idempotency_key = str(idempotency_key).strip()
    if len(idempotency_key) > 128:
        raise ValidationError({"idempotency_key": "The idempotency key is too long."})
    if not source or not source.pk:
        raise ValidationError({"source": "A saved Process Assembly is required."})
    if not project.company_profile_id or source.company_profile_id != project.company_profile_id:
        raise ValidationError(
            {"source": "The Process Assembly must belong to the project's company."},
        )
    if source.assembly_type != CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES:
        raise ValidationError({"source": "A Process Assembly is required."})

    Project.objects.select_for_update().get(pk=project.pk)
    existing = ProjectWorkstation.objects.filter(
        project=project,
        idempotency_key=idempotency_key,
    ).first()
    if existing:
        if existing.source_process_assembly_id != source.id:
            raise ValidationError(
                {"idempotency_key": "This key was already used for another Process Assembly."},
            )
        if name and existing.name != name:
            raise ValidationError({"idempotency_key": "This key was already used for another workstation name."})
        return WorkstationCreationResult(workstation=existing, created=False)

    ordered_items = _source_tree(source)
    snapshot_content = _snapshot_content(source, ordered_items)
    snapshot_hash = _content_hash(snapshot_content)
    workstation = ProjectWorkstation.objects.create(
        project=project,
        source_requirement=source_requirement,
        source_process_assembly=source,
        name=name or source.assembly_name,
        template_hash=snapshot_hash,
        active_revision=1,
        idempotency_key=idempotency_key,
        created_by=actor,
        modified_by=actor,
    )
    revision = WorkstationRevision.objects.create(
        workstation=workstation,
        number=1,
        reason="Created from company Process Assembly template.",
        source_hash=snapshot_hash,
        created_by=actor,
    )

    source_to_node = {}
    for relative_level in sorted({level for _item, level, _quantity in ordered_items}):
        level_nodes = []
        level_sources = []
        for item, level, quantity in ordered_items:
            if level != relative_level:
                continue
            level_nodes.append(
                ProjectWorkstationNode(
                    workstation=workstation,
                    revision=revision,
                    parent=source_to_node.get(item.parent_id),
                    source_company_item=item,
                    name=workstation.name if item.id == source.id else item.assembly_name,
                    assembly_type=item.assembly_type,
                    role=ProjectWorkstationNode.ROLE_BASELINE,
                    quantity=quantity,
                    unit=str((item.custom_fields or {}).get("unit") or ""),
                    level=relative_level,
                    sort_order=item.sort_order,
                    required_skills=list(item.required_skills or []),
                    provided_skills=list(item.provided_skills or []),
                    custom_fields=dict(item.custom_fields or {}),
                    created_by=actor,
                )
            )
            level_sources.append(item)
        ProjectWorkstationNode.objects.bulk_create(level_nodes)
        for item, node in zip(level_sources, level_nodes):
            source_to_node[item.id] = node

    if name:
        for node in snapshot_content["nodes"]:
            if node["source_company_item_id"] == source.id:
                node["name"] = name
    WorkstationSnapshot.objects.create(
        revision=revision,
        content=snapshot_content,
        content_hash=_content_hash(snapshot_content),
    )
    return WorkstationCreationResult(workstation=workstation, created=True)
