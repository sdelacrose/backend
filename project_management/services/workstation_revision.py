import hashlib
import json
import uuid
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models.project import (
    CompanySolutionSpaceItem,
    ProjectSolutionSpaceItem,
)
from project_management.models.workstation import (
    ProjectWorkstation,
    ProjectWorkstationNode,
    WorkstationRevision,
    WorkstationSnapshot,
)


class WorkstationRevisionConflict(Exception):
    def __init__(self, latest_revision):
        self.latest_revision = latest_revision
        super().__init__(f"Workstation is now at revision {latest_revision}.")


@dataclass(frozen=True)
class WorkingNode:
    occurrence_id: str
    original_node_id: int | None
    parent_occurrence_id: str | None
    source_company_item_id: int | None
    source_project_item_id: int | None
    supersedes_occurrence_id: str | None
    name: str
    assembly_type: str | None
    role: str
    is_active: bool
    quantity: Decimal
    unit: str
    level: int
    sort_order: int
    required_skills: list
    provided_skills: list
    custom_fields: dict
    approval_status: str = "open"


@dataclass(frozen=True)
class WorkstationRevisionPreview:
    expected_revision: int
    resulting_revision: int
    source_hash: str
    content_hash: str
    summary: dict
    nodes: list[dict]
    _working_nodes: tuple[WorkingNode, ...]
    before_nodes: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class WorkstationRevisionResult:
    revision: WorkstationRevision
    created: bool
    preview: WorkstationRevisionPreview


def _positive_decimal(raw_value, field_name="quantity"):
    try:
        value = Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValidationError({field_name: "Enter a valid quantity."}) from exc
    if not value.is_finite() or value <= 0:
        raise ValidationError({field_name: "Quantity must be greater than zero."})
    return value


def _current_nodes(workstation):
    nodes = list(
        workstation.nodes.filter(revision__number=workstation.active_revision)
        .select_related("parent", "supersedes")
        .order_by("level", "sort_order", "id")
    )
    if not nodes:
        raise ValidationError({"workstation": "The active workstation revision is empty."})
    return nodes


def _to_working_nodes(current_nodes):
    working = {
        str(node.occurrence_id): WorkingNode(
            occurrence_id=str(node.occurrence_id),
            original_node_id=node.id,
            parent_occurrence_id=(
                str(node.parent.occurrence_id) if node.parent_id else None
            ),
            source_company_item_id=node.source_company_item_id,
            source_project_item_id=node.source_project_item_id,
            supersedes_occurrence_id=(
                str(node.supersedes.occurrence_id) if node.supersedes_id else None
            ),
            name=node.name,
            assembly_type=node.assembly_type,
            role=node.role,
            is_active=node.is_active,
            quantity=node.quantity,
            unit=node.unit,
            level=node.level,
            sort_order=node.sort_order,
            required_skills=list(node.required_skills or []),
            provided_skills=list(node.provided_skills or []),
            custom_fields=dict(node.custom_fields or {}),
            approval_status=node.approval_status,
        )
        for node in current_nodes
    }
    if len(working) != len(current_nodes):
        raise ValidationError(
            {
                "workstation": (
                    "The active workstation contains duplicate occurrence identities."
                ),
            },
        )
    return working


def _id_to_occurrence(workstation):
    return {
        node_id: str(occurrence_id)
        for node_id, occurrence_id in workstation.nodes.values_list(
            "id",
            "occurrence_id",
        )
    }


def _resolve_node(operation, field, working, id_to_occurrence, *, required=True):
    raw_id = operation.get(field)
    if raw_id in (None, ""):
        if required:
            raise ValidationError({field: "This field is required."})
        return None
    try:
        node_id = int(raw_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError({field: "Enter a valid node ID."}) from exc
    occurrence_id = id_to_occurrence.get(node_id)
    if not occurrence_id or occurrence_id not in working:
        raise ValidationError({field: "Node is not part of the active workstation."})
    return occurrence_id


def _descendant_occurrences(working, root_occurrence):
    children = {}
    for node in working.values():
        children.setdefault(node.parent_occurrence_id, []).append(node.occurrence_id)
    descendants = []
    stack = list(children.get(root_occurrence, []))
    while stack:
        occurrence = stack.pop()
        descendants.append(occurrence)
        stack.extend(children.get(occurrence, []))
    return descendants


def _compatible_parent(working, parent_occurrence, field="parent_node"):
    parent = working[parent_occurrence]
    if not parent.is_active:
        raise ValidationError({field: "The target parent is excluded."})
    has_children = any(node.parent_occurrence_id == parent_occurrence for node in working.values())
    if parent.assembly_type != CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES and not has_children:
        raise ValidationError(
            {field: "Product occurrences can only be placed beneath an assembly."},
        )


def _project_source_tree(workstation, source_id, include_subtree):
    try:
        source_id = int(source_id)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"source_project_item": "Enter a valid source ID."}) from exc
    project_items = list(
        ProjectSolutionSpaceItem.objects.filter(project_id=workstation.project_id)
        .select_related("source_company_item")
        .order_by("level", "sort_order", "id")
    )
    by_id = {item.id: item for item in project_items}
    source = by_id.get(source_id)
    if not source:
        raise ValidationError(
            {"source_project_item": "Product Assembly is unavailable in this project."},
        )
    if source.assembly_type != CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS:
        raise ValidationError(
            {"source_project_item": "Select a Project Product Assembly."},
        )
    if not include_subtree:
        return [(source, 0)]

    children = {}
    for item in project_items:
        children.setdefault(item.parent_id, []).append(item)
    ordered = []
    active_path = set()
    visited = set()

    def visit(item, relative_level):
        if item.id in active_path or item.id in visited:
            raise ValidationError(
                {"source_project_item": "The Product Assembly hierarchy contains a cycle."},
            )
        active_path.add(item.id)
        visited.add(item.id)
        ordered.append((item, relative_level))
        for child in children.get(item.id, []):
            visit(child, relative_level + 1)
        active_path.remove(item.id)

    visit(source, 0)
    return ordered


def _add_source_nodes(
    workstation,
    working,
    operation,
    parent_occurrence,
    *,
    role,
    supersedes_occurrence=None,
    default_sort_order=None,
):
    source_tree = _project_source_tree(
        workstation,
        operation.get("source_project_item"),
        bool(operation.get("include_subtree", True)),
    )
    source_to_occurrence = {}
    added = []
    root_quantity = (
        _positive_decimal(operation["quantity"])
        if "quantity" in operation
        else None
    )
    for source, relative_level in source_tree:
        occurrence_id = str(uuid.uuid4())
        source_to_occurrence[source.id] = occurrence_id
        source_parent = (
            source_to_occurrence.get(source.parent_id)
            if relative_level
            else parent_occurrence
        )
        raw_quantity = (source.custom_fields or {}).get("quantity", 1)
        quantity = root_quantity if relative_level == 0 and root_quantity else _positive_decimal(raw_quantity)
        unit = str(
            operation.get("unit")
            if relative_level == 0 and operation.get("unit") is not None
            else (source.custom_fields or {}).get("unit") or ""
        )
        node = WorkingNode(
            occurrence_id=occurrence_id,
            original_node_id=None,
            parent_occurrence_id=source_parent,
            source_company_item_id=source.source_company_item_id,
            source_project_item_id=source.id,
            supersedes_occurrence_id=(
                supersedes_occurrence if relative_level == 0 else None
            ),
            name=source.assembly_name,
            assembly_type=source.assembly_type,
            role=role if relative_level == 0 else ProjectWorkstationNode.ROLE_ADDED,
            is_active=True,
            quantity=quantity,
            unit=unit,
            level=1,
            sort_order=(
                int(default_sort_order)
                if relative_level == 0 and default_sort_order is not None
                else source.sort_order
            ),
            required_skills=list(source.required_skills or []),
            provided_skills=list(source.provided_skills or []),
            custom_fields=dict(source.custom_fields or {}),
        )
        working[occurrence_id] = node
        added.append(occurrence_id)
    return added


def _validate_and_assign_levels(working):
    roots = [node for node in working.values() if node.parent_occurrence_id is None]
    if len(roots) != 1:
        raise ValidationError({"operations": "A workstation must have exactly one root."})
    children = {}
    for node in working.values():
        if node.parent_occurrence_id and node.parent_occurrence_id not in working:
            raise ValidationError({"operations": "A node has a missing parent."})
        children.setdefault(node.parent_occurrence_id, []).append(node.occurrence_id)

    seen = set()

    def visit(occurrence_id, level, active_path):
        if occurrence_id in active_path or occurrence_id in seen:
            raise ValidationError({"operations": "The resulting hierarchy contains a cycle."})
        if level > CompanySolutionSpaceItem.MAX_LEVEL:
            raise ValidationError(
                {"operations": "The resulting hierarchy exceeds the supported depth."},
            )
        active_path.add(occurrence_id)
        seen.add(occurrence_id)
        working[occurrence_id] = replace(working[occurrence_id], level=level)
        for child in children.get(occurrence_id, []):
            visit(child, level + 1, active_path)
        active_path.remove(occurrence_id)

    visit(roots[0].occurrence_id, 1, set())
    if len(seen) != len(working):
        raise ValidationError({"operations": "The resulting hierarchy is disconnected."})


def _public_node(node):
    return {
        "occurrence_id": node.occurrence_id,
        "original_node_id": node.original_node_id,
        "parent_occurrence_id": node.parent_occurrence_id,
        "source_company_item": node.source_company_item_id,
        "source_project_item": node.source_project_item_id,
        "supersedes_occurrence_id": node.supersedes_occurrence_id,
        "name": node.name,
        "assembly_type": node.assembly_type,
        "role": node.role,
        "is_active": node.is_active,
        "approval_status": node.approval_status,
        "quantity": str(node.quantity.normalize()),
        "unit": node.unit,
        "level": node.level,
        "sort_order": node.sort_order,
        "required_skills": node.required_skills,
        "provided_skills": node.provided_skills,
        "custom_fields": node.custom_fields,
    }


def _preview_nodes(nodes):
    """Expose reviewed hierarchy and totals without changing snapshot hashes."""
    rows = [_public_node(node) for node in nodes]
    by_id = {row["occurrence_id"]: row for row in rows}
    for row in rows:
        current, visited, names = row, set(), []
        quantity = Decimal(1)
        while current:
            identifier = current["occurrence_id"]
            if identifier in visited:
                raise ValidationError({"nodes": "Hierarchy contains a cycle."})
            visited.add(identifier)
            names.append(current["name"])
            quantity *= Decimal(current["quantity"]) if current["is_active"] else 0
            current = by_id.get(current["parent_occurrence_id"])
        row["effective_quantity"] = format(quantity, "f")
        row["path"] = " / ".join(reversed(names))
    return rows


def _content_and_hash(workstation, expected_revision, working):
    ordered = sorted(
        working.values(),
        key=lambda node: (node.level, node.sort_order, node.name, node.occurrence_id),
    )
    content = {
        "workstation_id": workstation.id,
        "source_revision": expected_revision,
        "nodes": [_public_node(node) for node in ordered],
    }
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return ordered, content, hashlib.sha256(encoded).hexdigest()


def preview_workstation_revision(*, workstation, operations, expected_revision):
    workstation.refresh_from_db(fields=["active_revision", "project"])
    if expected_revision != workstation.active_revision:
        raise WorkstationRevisionConflict(workstation.active_revision)
    if not isinstance(operations, list) or not operations:
        raise ValidationError({"operations": "Provide at least one revision operation."})

    current_nodes = _current_nodes(workstation)
    working = _to_working_nodes(current_nodes)
    before_nodes = _preview_nodes(working.values())
    id_to_occurrence = _id_to_occurrence(workstation)
    # Approval is a review of a saved composition, not a catalog property.
    # Keep approval changes separate from engineering changes so an API caller
    # cannot modify and approve an unseen composition in one request.
    approval_ops = [op for op in operations if isinstance(op, dict) and op.get("op") == "set_approval"]
    if approval_ops and len(approval_ops) != len(operations):
        raise ValidationError({"operations": "Save engineering changes before reviewing approval."})
    if not approval_ops:
        working = {key: replace(node, approval_status="open") for key, node in working.items()}
    summary = {
        "added": 0,
        "replaced": 0,
        "excluded": 0,
        "restored": 0,
        "moved": 0,
        "quantity_changed": 0,
        "approval_changed": 0,
    }

    for operation in operations:
        if not isinstance(operation, dict):
            raise ValidationError({"operations": "Every operation must be an object."})
        operation_name = operation.get("op")
        if operation_name == "set_approval":
            target_occurrence = _resolve_node(operation, "node", working, id_to_occurrence)
            approval = operation.get("approval_status")
            if approval not in dict(ProjectWorkstationNode.APPROVAL_CHOICES):
                raise ValidationError({"approval_status": "Choose Open, Under discussion or Agreed."})
            if not working[target_occurrence].is_active:
                raise ValidationError({"node": "Excluded parts cannot be approved."})
            if not isinstance(operation.get("include_subtree", False), bool):
                raise ValidationError({"include_subtree": "Enter a boolean."})
            affected = [target_occurrence]
            if operation.get("include_subtree", False):
                affected.extend(_descendant_occurrences(working, target_occurrence))
            for occurrence in affected:
                old = working[occurrence]
                if old.is_active and old.approval_status != approval:
                    working[occurrence] = replace(old, approval_status=approval)
                    summary["approval_changed"] += 1
        elif operation_name == "copy":
            for field_name in ("source_workstation", "expected_source_revision", "source_node"):
                if type(operation.get(field_name)) is not int or operation[field_name] < 1:
                    raise ValidationError({field_name: "Enter a positive integer."})
            parent_occurrence = _resolve_node(operation, "parent_node", working, id_to_occurrence)
            _compatible_parent(working, parent_occurrence)
            source_station = ProjectWorkstation.objects.filter(
                pk=operation.get("source_workstation"), project_id=workstation.project_id,
                status=ProjectWorkstation.STATUS_ACTIVE,
            ).first()
            if not source_station:
                raise ValidationError({"source_workstation": "Source workstation is unavailable in this project."})
            if operation.get("expected_source_revision") != source_station.active_revision:
                raise WorkstationRevisionConflict(source_station.active_revision)
            source_nodes = _to_working_nodes(_current_nodes(source_station))
            source_id_map = {node.original_node_id: key for key, node in source_nodes.items()}
            source_root = source_id_map.get(operation.get("source_node"))
            if not source_root or not source_nodes[source_root].is_active:
                raise ValidationError({"source_node": "Select an active occurrence in the current source revision."})
            selected = [source_root, *_descendant_occurrences(source_nodes, source_root)]
            selected = [key for key in selected if source_nodes[key].is_active]
            identities = {key: str(uuid.uuid4()) for key in selected}
            order = max((node.sort_order for node in working.values()
                         if node.parent_occurrence_id == parent_occurrence), default=-1) + 1
            for key in selected:
                source_node = source_nodes[key]
                custom = dict(source_node.custom_fields)
                custom.pop("_role_before_exclusion", None)
                custom["_copied_from"] = {"workstation": source_station.pk,
                                          "revision": source_station.active_revision,
                                          "occurrence": key}
                working[identities[key]] = replace(
                    source_node, occurrence_id=identities[key], original_node_id=None,
                    parent_occurrence_id=parent_occurrence if key == source_root else identities.get(source_node.parent_occurrence_id),
                    supersedes_occurrence_id=None, role=ProjectWorkstationNode.ROLE_ADDED,
                    approval_status="open", custom_fields=custom,
                    sort_order=order if key == source_root else source_node.sort_order,
                )
            summary["added"] += len(selected)
        elif operation_name == "add":
            parent_occurrence = _resolve_node(
                operation,
                "parent_node",
                working,
                id_to_occurrence,
            )
            _compatible_parent(working, parent_occurrence)
            siblings = [
                node.sort_order
                for node in working.values()
                if node.parent_occurrence_id == parent_occurrence
            ]
            added = _add_source_nodes(
                workstation,
                working,
                operation,
                parent_occurrence,
                role=ProjectWorkstationNode.ROLE_ADDED,
                default_sort_order=operation.get(
                    "sort_order",
                    max(siblings, default=-1) + 1,
                ),
            )
            summary["added"] += len(added)
        elif operation_name == "replace":
            target_occurrence = _resolve_node(
                operation,
                "node",
                working,
                id_to_occurrence,
            )
            target = working[target_occurrence]
            if not target.is_active:
                raise ValidationError({"node": "An excluded occurrence cannot be replaced."})
            if target.parent_occurrence_id:
                _compatible_parent(working, target.parent_occurrence_id)
            affected = [target_occurrence, *_descendant_occurrences(working, target_occurrence)]
            for occurrence in affected:
                old = working[occurrence]
                custom_fields = dict(old.custom_fields)
                custom_fields["_role_before_exclusion"] = old.role
                working[occurrence] = replace(
                    old,
                    role=ProjectWorkstationNode.ROLE_EXCLUDED,
                    is_active=False,
                    custom_fields=custom_fields,
                )
            added = _add_source_nodes(
                workstation,
                working,
                operation,
                target.parent_occurrence_id,
                role=ProjectWorkstationNode.ROLE_REPLACEMENT,
                supersedes_occurrence=target_occurrence,
                default_sort_order=target.sort_order,
            )
            summary["added"] += max(0, len(added) - 1)
            summary["replaced"] += 1
            summary["excluded"] += len(affected)
        elif operation_name in {"exclude", "restore"}:
            target_occurrence = _resolve_node(
                operation,
                "node",
                working,
                id_to_occurrence,
            )
            affected = [target_occurrence, *_descendant_occurrences(working, target_occurrence)]
            for occurrence in affected:
                old = working[occurrence]
                if operation_name == "exclude":
                    custom_fields = dict(old.custom_fields)
                    custom_fields.setdefault("_role_before_exclusion", old.role)
                    working[occurrence] = replace(
                        old,
                        role=ProjectWorkstationNode.ROLE_EXCLUDED,
                        is_active=False,
                        custom_fields=custom_fields,
                    )
                else:
                    custom_fields = dict(old.custom_fields)
                    restored_role = custom_fields.pop(
                        "_role_before_exclusion",
                        ProjectWorkstationNode.ROLE_BASELINE,
                    )
                    working[occurrence] = replace(
                        old,
                        role=restored_role,
                        is_active=True,
                        custom_fields=custom_fields,
                    )
            summary[operation_name + "d"] += len(affected)
        elif operation_name == "move":
            target_occurrence = _resolve_node(operation, "node", working, id_to_occurrence)
            parent_occurrence = _resolve_node(
                operation,
                "parent_node",
                working,
                id_to_occurrence,
                required=False,
            )
            if parent_occurrence:
                _compatible_parent(working, parent_occurrence)
            if target_occurrence == parent_occurrence or parent_occurrence in _descendant_occurrences(
                working,
                target_occurrence,
            ):
                raise ValidationError({"parent_node": "A node cannot move beneath itself."})
            target = working[target_occurrence]
            working[target_occurrence] = replace(
                target,
                parent_occurrence_id=parent_occurrence,
                sort_order=int(operation.get("sort_order", target.sort_order)),
            )
            summary["moved"] += 1
        elif operation_name == "set_quantity":
            target_occurrence = _resolve_node(operation, "node", working, id_to_occurrence)
            target = working[target_occurrence]
            working[target_occurrence] = replace(
                target,
                quantity=_positive_decimal(operation.get("quantity")),
                unit=str(operation.get("unit", target.unit)),
            )
            summary["quantity_changed"] += 1
        else:
            raise ValidationError({"op": f"Unsupported operation: {operation_name}."})

    _validate_and_assign_levels(working)
    ordered, _content, content_hash = _content_and_hash(
        workstation,
        expected_revision,
        working,
    )
    current_revision = workstation.revisions.select_related("snapshot").get(
        number=expected_revision,
    )
    return WorkstationRevisionPreview(
        expected_revision=expected_revision,
        resulting_revision=expected_revision + 1,
        source_hash=current_revision.snapshot.content_hash,
        content_hash=content_hash,
        summary=summary,
        nodes=_preview_nodes(ordered),
        _working_nodes=tuple(ordered),
        before_nodes=before_nodes,
    )


@transaction.atomic
def commit_workstation_revision(
    *,
    workstation,
    operations,
    expected_revision,
    actor,
    reason,
    idempotency_key,
):
    if not idempotency_key or not str(idempotency_key).strip():
        raise ValidationError({"idempotency_key": "This field is required."})
    if len(str(idempotency_key)) > 128:
        raise ValidationError({"idempotency_key": "Ensure this value has at most 128 characters."})

    source_ids = {op.get("source_workstation") for op in operations
                  if isinstance(op, dict) and op.get("op") == "copy"
                  and type(op.get("source_workstation")) is int}
    source_ids.discard(None)
    locked_stations = list(ProjectWorkstation.objects.select_for_update().filter(
        pk__in={workstation.pk, *source_ids}, project_id=workstation.project_id,
    ).select_related("project").order_by("pk"))
    locked = next(station for station in locked_stations if station.pk == workstation.pk)
    if locked.status != ProjectWorkstation.STATUS_ACTIVE:
        raise ValidationError({"workstation": "Archived workstations cannot be changed."})
    existing = locked.revisions.filter(idempotency_key=idempotency_key).first()
    if existing:
        preview = WorkstationRevisionPreview(
            expected_revision=max(1, existing.number - 1),
            resulting_revision=existing.number,
            source_hash=existing.source_hash,
            content_hash=existing.snapshot.content_hash,
            summary=dict(existing.summary or {}),
            nodes=list(existing.snapshot.content.get("nodes", [])),
            _working_nodes=tuple(),
        )
        return WorkstationRevisionResult(existing, False, preview)
    if expected_revision != locked.active_revision:
        raise WorkstationRevisionConflict(locked.active_revision)

    preview = preview_workstation_revision(
        workstation=locked,
        operations=operations,
        expected_revision=expected_revision,
    )
    revision = WorkstationRevision.objects.create(
        workstation=locked,
        number=preview.resulting_revision,
        reason=str(reason or "").strip(),
        source_hash=preview.source_hash,
        idempotency_key=str(idempotency_key),
        summary=preview.summary,
        created_by=actor,
    )
    created_by_occurrence = {}
    for level in sorted({node.level for node in preview._working_nodes}):
        level_nodes = [node for node in preview._working_nodes if node.level == level]
        model_nodes = [
            ProjectWorkstationNode(
                workstation=locked,
                revision=revision,
                occurrence_id=node.occurrence_id,
                parent=(
                    created_by_occurrence.get(node.parent_occurrence_id)
                    if node.parent_occurrence_id
                    else None
                ),
                source_company_item_id=node.source_company_item_id,
                source_project_item_id=node.source_project_item_id,
                name=node.name,
                assembly_type=node.assembly_type,
                role=node.role,
                is_active=node.is_active,
                quantity=node.quantity,
                unit=node.unit,
                level=node.level,
                sort_order=node.sort_order,
                required_skills=node.required_skills,
                provided_skills=node.provided_skills,
                custom_fields=node.custom_fields,
                approval_status=node.approval_status,
                created_by=actor,
            )
            for node in level_nodes
        ]
        ProjectWorkstationNode.objects.bulk_create(model_nodes)
        created_by_occurrence.update(
            {
                node.occurrence_id: model_node
                for node, model_node in zip(level_nodes, model_nodes)
            }
        )
    for node in preview._working_nodes:
        if node.supersedes_occurrence_id:
            created = created_by_occurrence[node.occurrence_id]
            created.supersedes = created_by_occurrence.get(node.supersedes_occurrence_id)
            created.save(update_fields=["supersedes"])

    content = {
        "workstation_id": locked.id,
        "source_revision": expected_revision,
        "nodes": preview.nodes,
    }
    WorkstationSnapshot.objects.create(
        revision=revision,
        content=content,
        content_hash=preview.content_hash,
    )
    locked.active_revision = revision.number
    locked.modified_by = actor
    locked.save(update_fields=["active_revision", "modified_by", "updated_at"])

    from aas_integration.services import mark_project_out_of_date

    mark_project_out_of_date(locked.project, getattr(actor, "id", None))
    return WorkstationRevisionResult(revision, True, preview)
