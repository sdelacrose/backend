import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from project_management.models import (
    BillOfMaterialPart,
    BomGenerationRun,
    BomPlanOperation,
    ProjectWorkstation,
    ProjectWorkstationNode,
    WorkstationRevision,
    WorkstationSnapshot,
)


logger = logging.getLogger(__name__)


class BomPlanConflict(Exception):
    pass


class StaleBomPlanConflict(BomPlanConflict):
    def __init__(self, message, reason):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class BomApplyResult:
    plan: BomGenerationRun
    created: bool


@dataclass(frozen=True)
class QueuedBomPlanResult:
    plan: BomGenerationRun
    created: bool


ENGINEERING_FIELDS = (
    "source_snapshot_id",
    "name",
    "parent_lineage",
    "level",
    "sort_order",
    "engineering_quantity",
    "engineering_unit",
    "purchasable",
    "engineering_detached",
)


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _target_content(project):
    return [
        {
            "id": row.id,
            "parent_id": row.parent_id,
            "name": row.name,
            "part_number": row.part_number,
            "status": row.status,
            "sort_order": row.sort_order,
            "level": row.level,
            "custom_fields": row.custom_fields,
            "source_workstation_id": row.source_workstation_id,
            "source_snapshot_id": row.source_workstation_snapshot_id,
            "source_occurrence_id": row.source_occurrence_id,
            "engineering_quantity": row.engineering_quantity,
            "engineering_unit": row.engineering_unit,
            "is_purchasable": row.is_purchasable,
            "engineering_detached": row.engineering_detached,
        }
        for row in BillOfMaterialPart.objects.filter(project=project)
        .select_related("parent")
        .order_by("id")
    ]


def calculate_target_hash(project):
    return _hash(_target_content(project))


def _normalized_workstation_ids(workstation_ids):
    normalized_ids = []
    for raw_id in workstation_ids or []:
        try:
            workstation_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"workstations": "Enter valid workstation IDs."}) from exc
        if workstation_id not in normalized_ids:
            normalized_ids.append(workstation_id)
    if not normalized_ids:
        raise ValidationError({"workstations": "Select at least one workstation."})
    return normalized_ids


def _available_workstations(project, normalized_ids):
    workstations = list(
        ProjectWorkstation.objects.filter(
            project=project,
            id__in=normalized_ids,
            status=ProjectWorkstation.STATUS_ACTIVE,
        ).select_related("project")
    )
    by_id = {workstation.id: workstation for workstation in workstations}
    if set(normalized_ids) != set(by_id):
        raise ValidationError({"workstations": "A selected workstation is unavailable."})
    return by_id


def estimate_bom_plan_node_count(*, project, workstation_ids):
    normalized_ids = _normalized_workstation_ids(workstation_ids)
    _available_workstations(project, normalized_ids)
    node_count = ProjectWorkstationNode.objects.filter(
        workstation_id__in=normalized_ids,
        workstation__project=project,
        workstation__status=ProjectWorkstation.STATUS_ACTIVE,
        revision__number=F("workstation__active_revision"),
        is_active=True,
    ).count()
    return normalized_ids, node_count


def _normalized_workstation(workstation, snapshot, root_sort_order):
    nodes = list(
        workstation.nodes.filter(
            revision=snapshot.revision,
            is_active=True,
        ).order_by("level", "sort_order", "id")
    )
    if not nodes:
        raise ValidationError({"workstations": f"Workstation {workstation.id} is empty."})
    pending = [node.name for node in nodes if node.approval_status != "agreed"]
    if pending:
        raise ValidationError({"workstations": (
            f"{workstation.name}: all active parts and assemblies must be Agreed before copying to BoM. "
            f"Awaiting agreement: {', '.join(pending[:20])}"
            + (f" and {len(pending) - 20} more." if len(pending) > 20 else ".")
        )})
    by_occurrence = {str(node.occurrence_id): node for node in nodes}
    if len(by_occurrence) != len(nodes):
        raise ValidationError(
            {"workstations": f"Workstation {workstation.id} has duplicate occurrences."},
        )
    active_occurrences = set(by_occurrence)
    children = {}
    for node in nodes:
        parent_occurrence = str(node.parent.occurrence_id) if node.parent_id else None
        if parent_occurrence and parent_occurrence not in active_occurrences:
            raise ValidationError(
                {"workstations": f"Workstation {workstation.id} has an active orphan."},
            )
        children.setdefault(parent_occurrence, []).append(node)
    roots = children.get(None, [])
    if len(roots) != 1:
        raise ValidationError({"workstations": "Every workstation must have one active root."})

    normalized = []

    def visit(node, parent_lineage, cumulative_quantity, level):
        occurrence = str(node.occurrence_id)
        quantity = cumulative_quantity * Decimal(node.quantity)
        node_children = sorted(
            children.get(occurrence, []),
            key=lambda item: (item.sort_order, item.id),
        )
        lineage = f"{workstation.id}:{occurrence}"
        normalized.append(
            {
                "source_workstation_id": workstation.id,
                "source_snapshot_id": snapshot.id,
                "source_occurrence_id": occurrence,
                "lineage": lineage,
                "parent_lineage": parent_lineage,
                "name": workstation.name if level == 1 else node.name,
                "level": level,
                "sort_order": root_sort_order if level == 1 else node.sort_order,
                "engineering_quantity": str(quantity.normalize()),
                "engineering_unit": node.unit,
                "purchasable": not node_children,
                "engineering_detached": False,
            }
        )
        for child in node_children:
            visit(child, lineage, quantity, level + 1)

    visit(roots[0], None, Decimal("1"), 1)
    return normalized


def incomplete_workstation_bom_ids(project):
    """Read-only comparison against the same engineering tree used by BoM planning."""
    incomplete = []
    for workstation in ProjectWorkstation.objects.filter(project=project, status="active"):
        snapshot = WorkstationSnapshot.objects.filter(
            revision__workstation=workstation, revision__number=workstation.active_revision,
        ).select_related("revision").first()
        if snapshot is None:
            incomplete.append(workstation.id)
            continue
        try:
            expected = _normalized_workstation(workstation, snapshot, 0)
        except ValidationError:
            incomplete.append(workstation.id)
            continue
        actual = list(BillOfMaterialPart.objects.filter(
            project=project, source_workstation=workstation, engineering_detached=False,
        ).select_related("parent"))
        expected_values = {
            item["source_occurrence_id"]: (
                item["source_snapshot_id"], item["parent_lineage"],
                Decimal(item["engineering_quantity"]), item["engineering_unit"], item["purchasable"],
            ) for item in expected
        }
        actual_values = {
            str(item.source_occurrence_id): (
                item.source_workstation_snapshot_id,
                f"{item.parent.source_workstation_id}:{item.parent.source_occurrence_id}" if item.parent_id else None,
                item.engineering_quantity, item.engineering_unit, item.is_purchasable,
            ) for item in actual
        }
        if len(actual) != len(expected) or actual_values != expected_values:
            incomplete.append(workstation.id)
    return incomplete


def _before_values(row):
    return {
        "source_snapshot_id": row.source_workstation_snapshot_id,
        "name": row.name,
        "parent_lineage": (
            f"{row.parent.source_workstation_id}:{row.parent.source_occurrence_id}"
            if row.parent_id and row.parent.source_workstation_id and row.parent.source_occurrence_id
            else None
        ),
        "level": row.level,
        "sort_order": row.sort_order,
        "engineering_quantity": (
            str(row.engineering_quantity.normalize())
            if row.engineering_quantity is not None
            else None
        ),
        "engineering_unit": row.engineering_unit,
        "purchasable": row.is_purchasable,
        "engineering_detached": row.engineering_detached,
    }


@transaction.atomic
def create_bom_plan(
    *,
    project,
    workstation_ids,
    actor,
    idempotency_key,
    expected_source_snapshots=None,
    expected_target_hash=None,
    internal_run=False,
):
    started = time.monotonic()
    started_at = timezone.now()
    if not idempotency_key or not str(idempotency_key).strip():
        raise ValidationError({"idempotency_key": "This field is required."})
    existing_run = BomGenerationRun.objects.filter(
        project=project,
        idempotency_key=str(idempotency_key),
        internal_run=internal_run,
    ).first()
    if existing_run:
        return existing_run
    normalized_ids = _normalized_workstation_ids(workstation_ids)
    by_id = _available_workstations(project, normalized_ids)

    snapshots = []
    expected_rows = []
    for root_order, workstation_id in enumerate(normalized_ids):
        workstation = by_id[workstation_id]
        revision = workstation.revisions.select_related("snapshot").get(
            number=workstation.active_revision,
        )
        snapshot = revision.snapshot
        snapshots.append(snapshot)
    # Existing BoM list endpoints order the whole tree by sort_order. A unique
    # preorder keeps each workstation section contiguous while preserving the
    # sibling order inherited from the workstation snapshot.

    source_hash = _hash(
        sorted(
            [
                {
                    "workstation": workstation_id,
                    "snapshot": snapshot.id,
                    "content_hash": snapshot.content_hash,
                }
                for workstation_id, snapshot in zip(normalized_ids, snapshots)
            ],
            key=lambda value: value["workstation"],
        ),
    )
    target_hash = calculate_target_hash(project)
    if expected_source_snapshots is not None:
        expected_source_hash = _hash(
            sorted(expected_source_snapshots, key=lambda value: value["workstation"]),
        )
        if source_hash != expected_source_hash:
            raise StaleBomPlanConflict(
                "The selected workstation revisions changed while the plan was queued.",
                "source_hash_changed",
            )
    if expected_target_hash is not None and target_hash != expected_target_hash:
        raise StaleBomPlanConflict(
            "The Project BoM changed while the plan was queued.",
            "target_hash_changed",
        )
    for root_order, (workstation_id, snapshot) in enumerate(zip(normalized_ids, snapshots)):
        expected_rows.extend(_normalized_workstation(by_id[workstation_id], snapshot, root_order))
    for display_order, expected_row in enumerate(expected_rows):
        expected_row["sort_order"] = display_order
    run = BomGenerationRun.objects.create(
        project=project,
        source_hash=source_hash,
        target_hash=target_hash,
        idempotency_key=str(idempotency_key),
        internal_run=internal_run,
        created_by=actor,
        started_at=started_at,
    )
    run.workstation_snapshots.set(snapshots)

    existing_rows = list(
        BillOfMaterialPart.objects.filter(
            project=project,
            source_workstation__isnull=False,
        ).select_related("parent")
    )
    existing_by_lineage = {}
    existing_rows_by_lineage = {}
    for row in existing_rows:
        if not row.source_occurrence_id:
            continue
        lineage = f"{row.source_workstation_id}:{row.source_occurrence_id}"
        existing_rows_by_lineage.setdefault(lineage, []).append(row)
        existing_by_lineage.setdefault(lineage, row)

    expected_lineages = {row["lineage"] for row in expected_rows}
    operations = []
    summary = {key: 0 for key in ("add", "update", "unchanged", "detach", "conflict")}
    operation_order = 0
    for after in expected_rows:
        before_row = existing_by_lineage.get(after["lineage"])
        duplicate_rows = existing_rows_by_lineage.get(after["lineage"], [])
        if len(duplicate_rows) > 1:
            operation = BomPlanOperation.OPERATION_CONFLICT
            before = {
                "duplicate_targets": [
                    {"id": row.id, **_before_values(row)}
                    for row in sorted(duplicate_rows, key=lambda item: item.id)
                ],
            }
            before_row = None
        elif before_row is None:
            operation = BomPlanOperation.OPERATION_ADD
            before = {}
        else:
            before = _before_values(before_row)
            operation = (
                BomPlanOperation.OPERATION_UNCHANGED
                if all(before.get(field) == after.get(field) for field in ENGINEERING_FIELDS)
                else BomPlanOperation.OPERATION_UPDATE
            )
        summary[operation] += 1
        operations.append(
            BomPlanOperation(
                run=run,
                operation=operation,
                source_workstation_id=after["source_workstation_id"],
                source_snapshot_id=after["source_snapshot_id"],
                source_occurrence_id=after["source_occurrence_id"],
                target_bom_part=before_row,
                before_values=before,
                after_values=after,
                resolution_state="unresolved" if operation == "conflict" else "",
                sort_order=operation_order,
            )
        )
        operation_order += 1
    for lineage, row in existing_by_lineage.items():
        if lineage in expected_lineages or row.engineering_detached:
            continue
        summary["detach"] += 1
        operations.append(
            BomPlanOperation(
                run=run,
                operation=BomPlanOperation.OPERATION_DETACH,
                source_workstation=row.source_workstation,
                source_snapshot=row.source_workstation_snapshot,
                source_occurrence_id=row.source_occurrence_id,
                target_bom_part=row,
                before_values=_before_values(row),
                after_values={"engineering_detached": True},
                resolution_state="confirmation_required",
                sort_order=operation_order,
            )
        )
        operation_order += 1
    BomPlanOperation.objects.bulk_create(operations)
    duration_ms = max(0, int((time.monotonic() - started) * 1000))
    run.summary = summary
    run.node_count = len(expected_rows)
    run.duration_ms = duration_ms
    run.completed_at = timezone.now()
    run.metrics = {
        "planning": {
            "mode": "synchronous",
            "duration_ms": duration_ms,
            "node_count": len(expected_rows),
            "operation_counts": summary,
        },
        "stale_plan_rejections": 0,
    }
    run.save(
        update_fields=[
            "summary",
            "metrics",
            "node_count",
            "duration_ms",
            "completed_at",
            "updated_at",
        ]
    )
    logger.info(
        "BoM plan completed",
        extra={
            "run_id": run.id,
            "project_id": project.id,
            "source_hash": run.source_hash,
            "target_hash": run.target_hash,
            "duration_ms": duration_ms,
            "node_count": len(expected_rows),
            "operation_counts": summary,
        },
    )
    return run


@transaction.atomic
def create_queued_bom_plan(*, project, workstation_ids, actor, idempotency_key):
    if not idempotency_key or not str(idempotency_key).strip():
        raise ValidationError({"idempotency_key": "This field is required."})
    existing = BomGenerationRun.objects.filter(
        project=project,
        idempotency_key=str(idempotency_key),
    ).first()
    if existing:
        return QueuedBomPlanResult(plan=existing, created=False)
    normalized_ids, node_count = estimate_bom_plan_node_count(
        project=project,
        workstation_ids=workstation_ids,
    )
    revisions = list(
        WorkstationRevision.objects.filter(
            workstation_id__in=normalized_ids,
            workstation__project=project,
            workstation__status=ProjectWorkstation.STATUS_ACTIVE,
            number=F("workstation__active_revision"),
        ).select_related("snapshot", "workstation")
    )
    revisions_by_workstation = {revision.workstation_id: revision for revision in revisions}
    if set(revisions_by_workstation) != set(normalized_ids):
        raise ValidationError({"workstations": "A selected workstation revision is unavailable."})
    source_snapshots = [
        {
            "workstation": workstation_id,
            "snapshot": revisions_by_workstation[workstation_id].snapshot.id,
            "content_hash": revisions_by_workstation[workstation_id].snapshot.content_hash,
        }
        for workstation_id in normalized_ids
    ]
    target_hash = calculate_target_hash(project)
    plan = BomGenerationRun.objects.create(
        project=project,
        source_hash=_hash(sorted(source_snapshots, key=lambda value: value["workstation"])),
        target_hash=target_hash,
        status=BomGenerationRun.STATUS_QUEUED,
        request_payload={
            "workstations": normalized_ids,
            "source_snapshots": source_snapshots,
            "target_hash": target_hash,
        },
        metrics={
            "planning": {"mode": "asynchronous", "node_count": node_count},
            "stale_plan_rejections": 0,
        },
        node_count=node_count,
        idempotency_key=str(idempotency_key),
        created_by=actor,
    )
    plan.workstation_snapshots.set(
        [revisions_by_workstation[value].snapshot for value in normalized_ids],
    )
    return QueuedBomPlanResult(plan=plan, created=True)


def process_queued_bom_plan(run_id):
    started = time.monotonic()
    claimed_at = timezone.now()
    claim_token = uuid.uuid4()
    claim_expires_at = claimed_at + timedelta(
        seconds=settings.PROCESS_BOM_RUNNING_TIMEOUT_SECONDS,
    )
    claimed = BomGenerationRun.objects.filter(
        pk=run_id,
    ).filter(
        Q(status=BomGenerationRun.STATUS_QUEUED)
        | Q(
            status=BomGenerationRun.STATUS_RUNNING,
            claim_expires_at__lt=claimed_at,
        )
        | Q(
            status=BomGenerationRun.STATUS_RUNNING,
            claim_expires_at__isnull=True,
        )
    ).update(
        status=BomGenerationRun.STATUS_RUNNING,
        started_at=claimed_at,
        completed_at=None,
        claim_token=claim_token,
        claim_expires_at=claim_expires_at,
    )
    queued = BomGenerationRun.objects.select_related("project", "created_by").get(pk=run_id)
    if not claimed:
        return queued
    try:
        materialized = create_bom_plan(
            project=queued.project,
            workstation_ids=queued.request_payload.get("workstations", []),
            actor=queued.created_by,
            idempotency_key=f"__bom_worker__:{queued.id}",
            expected_source_snapshots=queued.request_payload.get("source_snapshots", []),
            expected_target_hash=queued.request_payload.get("target_hash"),
            internal_run=True,
        )
        with transaction.atomic():
            locked = BomGenerationRun.objects.select_for_update().get(pk=queued.pk)
            if (
                locked.status != BomGenerationRun.STATUS_RUNNING
                or locked.claim_token != claim_token
            ):
                return locked
            materialized.operations.update(run=locked)
            locked.workstation_snapshots.set(materialized.workstation_snapshots.all())
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            locked.source_hash = materialized.source_hash
            locked.target_hash = materialized.target_hash
            locked.status = BomGenerationRun.STATUS_PREVIEW
            locked.summary = materialized.summary
            locked.duration_ms = duration_ms
            locked.completed_at = timezone.now()
            locked.metrics = {
                **(locked.metrics or {}),
                **(materialized.metrics or {}),
                "planning": {
                    **(materialized.metrics or {}).get("planning", {}),
                    "mode": "asynchronous",
                    "duration_ms": duration_ms,
                },
            }
            locked.error_details = {}
            locked.claim_token = None
            locked.claim_expires_at = None
            locked.save(
                update_fields=[
                    "source_hash",
                    "target_hash",
                    "status",
                    "summary",
                    "duration_ms",
                    "completed_at",
                    "metrics",
                    "error_details",
                    "claim_token",
                    "claim_expires_at",
                    "updated_at",
                ]
            )
            materialized.delete()
        logger.info(
            "Asynchronous BoM plan completed",
            extra={"run_id": queued.id, "project_id": queued.project_id, "duration_ms": duration_ms},
        )
    except StaleBomPlanConflict as exc:
        stale = BomGenerationRun.objects.get(pk=queued.pk)
        metrics = dict(stale.metrics or {})
        metrics["stale_plan_rejections"] = int(metrics.get("stale_plan_rejections", 0)) + 1
        metrics["last_stale_reason"] = exc.reason
        BomGenerationRun.objects.filter(
            pk=queued.pk,
            status=BomGenerationRun.STATUS_RUNNING,
            claim_token=claim_token,
        ).update(
            status=BomGenerationRun.STATUS_STALE,
            completed_at=timezone.now(),
            error_details={"code": exc.reason},
            metrics=metrics,
            claim_token=None,
            claim_expires_at=None,
        )
        logger.info(
            "Asynchronous BoM plan became stale",
            extra={"run_id": queued.id, "project_id": queued.project_id, "reason": exc.reason},
        )
    except Exception:
        failed = BomGenerationRun.objects.get(pk=queued.pk)
        metrics = dict(failed.metrics or {})
        metrics["failure_count"] = int(metrics.get("failure_count", 0)) + 1
        BomGenerationRun.objects.filter(
            pk=queued.pk,
            status=BomGenerationRun.STATUS_RUNNING,
            claim_token=claim_token,
        ).update(
            status=BomGenerationRun.STATUS_FAILED,
            completed_at=timezone.now(),
            error_details={"code": "bom_plan_generation_failed"},
            metrics=metrics,
            claim_token=None,
            claim_expires_at=None,
        )
        logger.exception("Asynchronous BoM plan failed", extra={"run_id": queued.id})
    return BomGenerationRun.objects.get(pk=queued.pk)


def dispatch_bom_plan_run(run_id):
    from project_management.tasks import process_bom_generation_run

    process_bom_generation_run.apply_async(
        args=[run_id],
        queue=settings.PROCESS_BOM_QUEUE,
    )


@transaction.atomic
def resolve_bom_plan_conflicts(*, plan, resolutions):
    locked = BomGenerationRun.objects.select_for_update().get(pk=plan.pk)
    if locked.status != BomGenerationRun.STATUS_PREVIEW:
        raise BomPlanConflict("Only a preview plan can be resolved.")
    by_id = {
        int(value["operation"]): value
        for value in resolutions
        if value.get("operation") is not None
    }
    conflicts = list(
        locked.operations.select_for_update()
        .filter(operation=BomPlanOperation.OPERATION_CONFLICT)
        .order_by("sort_order", "id")
    )
    if set(by_id) != {operation.id for operation in conflicts}:
        raise BomPlanConflict("Resolve every conflict in this plan exactly once.")
    for operation in conflicts:
        resolution = by_id[operation.id]
        decision = resolution.get("decision")
        if decision == "keep_existing":
            candidate_ids = [
                candidate.get("id")
                for candidate in operation.before_values.get("duplicate_targets", [])
                if candidate.get("id")
            ]
            operation.target_bom_part_id = min(candidate_ids) if candidate_ids else None
            operation.resolution_state = "resolved_keep_existing"
            operation.save(update_fields=["target_bom_part", "resolution_state"])
            continue
        if decision != "use_workstation":
            raise BomPlanConflict("Choose a supported resolution for every conflict.")
        target_id = resolution.get("target_bom_part")
        candidate_ids = [
            candidate.get("id")
            for candidate in operation.before_values.get("duplicate_targets", [])
        ]
        if target_id not in candidate_ids:
            raise BomPlanConflict("Choose one of the conflicting BoM rows as the target.")
        after_values = {
            **operation.after_values,
            "detach_duplicate_target_ids": [
                candidate_id for candidate_id in candidate_ids if candidate_id != target_id
            ],
        }
        operation.target_bom_part_id = target_id
        operation.after_values = after_values
        operation.resolution_state = "resolved_use_workstation"
        operation.save(
            update_fields=["target_bom_part", "after_values", "resolution_state"],
        )
    return locked


def _verify_source_hash(plan):
    snapshots = list(
        plan.workstation_snapshots.select_related("revision__workstation").order_by(
            "revision__workstation_id",
        )
    )
    for snapshot in snapshots:
        workstation = snapshot.revision.workstation
        if workstation.status != "active" or workstation.nodes.filter(
            revision=snapshot.revision, is_active=True,
        ).exclude(approval_status="agreed").exists():
            return False
        if workstation.active_revision != snapshot.revision.number:
            return False
        if snapshot.content_hash != snapshot.revision.snapshot.content_hash:
            return False
    selected_ids = [snapshot.revision.workstation_id for snapshot in snapshots]
    by_workstation = {snapshot.revision.workstation_id: snapshot for snapshot in snapshots}
    calculated = _hash(
        [
            {
                "workstation": workstation_id,
                "snapshot": by_workstation[workstation_id].id,
                "content_hash": by_workstation[workstation_id].content_hash,
            }
            for workstation_id in selected_ids
        ],
    )
    return calculated == plan.source_hash


@transaction.atomic
def _apply_bom_plan_atomic(*, plan, actor, expected_target_hash, confirm_detach=False):
    locked = BomGenerationRun.objects.select_for_update().select_related("project").get(pk=plan.pk)
    list(ProjectWorkstation.objects.select_for_update().filter(
        id__in=locked.workstation_snapshots.values_list("revision__workstation_id", flat=True),
    ).order_by("id"))
    if locked.status == BomGenerationRun.STATUS_APPLIED:
        return BomApplyResult(locked, False)
    if locked.status != BomGenerationRun.STATUS_PREVIEW:
        raise BomPlanConflict("Only a preview plan can be applied.")
    if expected_target_hash != locked.target_hash or calculate_target_hash(locked.project) != locked.target_hash:
        raise StaleBomPlanConflict(
            "The Project BoM changed after this preview.",
            "target_hash_changed",
        )
    if not _verify_source_hash(locked):
        raise StaleBomPlanConflict(
            "A workstation changed after this preview.",
            "source_hash_changed",
        )
    operations = list(
        locked.operations.select_related("target_bom_part", "source_workstation", "source_snapshot").order_by(
            "sort_order",
            "id",
        )
    )
    if any(
        operation.operation == BomPlanOperation.OPERATION_CONFLICT
        and operation.resolution_state == "unresolved"
        for operation in operations
    ):
        raise BomPlanConflict("The plan contains unresolved conflicts.")
    if not confirm_detach and any(operation.operation == BomPlanOperation.OPERATION_DETACH for operation in operations):
        raise BomPlanConflict("Detach operations require explicit confirmation.")

    created_by_lineage = {}
    for operation in operations:
        if (
            operation.operation == BomPlanOperation.OPERATION_CONFLICT
            and operation.resolution_state == "resolved_keep_existing"
        ):
            target = operation.target_bom_part
            lineage = operation.after_values.get("lineage")
            if target and lineage:
                created_by_lineage[lineage] = target
            continue
        if operation.operation == BomPlanOperation.OPERATION_DETACH:
            target = operation.target_bom_part
            if target:
                target.engineering_detached = True
                target.modified_by = actor
                target.save(update_fields=["engineering_detached", "modified_by", "updated_at"])
            continue
        after = operation.after_values
        lineage = after["lineage"]
        parent = created_by_lineage.get(after.get("parent_lineage"))
        target = operation.target_bom_part
        if operation.operation == BomPlanOperation.OPERATION_ADD:
            target = BillOfMaterialPart.objects.create(
                project=locked.project,
                parent=parent,
                name=after["name"],
                level=after["level"],
                sort_order=after["sort_order"],
                source_workstation_id=after["source_workstation_id"],
                source_workstation_snapshot_id=after["source_snapshot_id"],
                source_occurrence_id=after["source_occurrence_id"],
                engineering_quantity=after["engineering_quantity"],
                engineering_unit=after["engineering_unit"],
                is_purchasable=after["purchasable"],
                engineering_detached=False,
                created_by=actor,
                modified_by=actor,
            )
            operation.target_bom_part = target
            operation.save(update_fields=["target_bom_part"])
        elif operation.operation in {
            BomPlanOperation.OPERATION_UPDATE,
            BomPlanOperation.OPERATION_CONFLICT,
        }:
            for duplicate_id in after.get("detach_duplicate_target_ids", []):
                BillOfMaterialPart.objects.filter(
                    project=locked.project,
                    id=duplicate_id,
                    source_workstation_id=operation.source_workstation_id,
                    source_occurrence_id=operation.source_occurrence_id,
                ).update(
                    engineering_detached=True,
                    modified_by=actor,
                    updated_at=timezone.now(),
                )
            target.parent = parent
            target.name = after["name"]
            target.level = after["level"]
            target.sort_order = after["sort_order"]
            target.source_workstation_snapshot_id = after["source_snapshot_id"]
            target.engineering_quantity = after["engineering_quantity"]
            target.engineering_unit = after["engineering_unit"]
            target.is_purchasable = after["purchasable"]
            target.engineering_detached = False
            target.modified_by = actor
            target.save(
                update_fields=[
                    "parent",
                    "name",
                    "level",
                    "sort_order",
                    "source_workstation_snapshot",
                    "engineering_quantity",
                    "engineering_unit",
                    "is_purchasable",
                    "engineering_detached",
                    "modified_by",
                    "updated_at",
                ],
            )
        if target:
            created_by_lineage[lineage] = target

    locked.status = BomGenerationRun.STATUS_APPLIED
    locked.applied_by = actor
    locked.applied_at = timezone.now()
    locked.save(update_fields=["status", "applied_by", "applied_at", "updated_at"])

    from aas_integration.services import mark_project_out_of_date

    mark_project_out_of_date(locked.project, getattr(actor, "id", None))
    return BomApplyResult(locked, True)


def apply_bom_plan(*, plan, actor, expected_target_hash, confirm_detach=False):
    started = time.monotonic()
    try:
        result = _apply_bom_plan_atomic(
            plan=plan,
            actor=actor,
            expected_target_hash=expected_target_hash,
            confirm_detach=confirm_detach,
        )
    except StaleBomPlanConflict as exc:
        stale = BomGenerationRun.objects.get(pk=plan.pk)
        metrics = dict(stale.metrics or {})
        metrics["stale_plan_rejections"] = int(metrics.get("stale_plan_rejections", 0)) + 1
        metrics["last_stale_reason"] = exc.reason
        BomGenerationRun.objects.filter(pk=stale.pk).update(
            status=BomGenerationRun.STATUS_STALE,
            metrics=metrics,
        )
        logger.warning(
            "Stale BoM plan rejected",
            extra={
                "run_id": stale.id,
                "project_id": stale.project_id,
                "source_hash": stale.source_hash,
                "target_hash": stale.target_hash,
                "stale_reason": exc.reason,
            },
        )
        raise BomPlanConflict(str(exc)) from exc

    if result.created:
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        metrics = dict(result.plan.metrics or {})
        metrics["apply"] = {
            "duration_ms": duration_ms,
            "operation_counts": result.plan.summary,
        }
        BomGenerationRun.objects.filter(pk=result.plan.pk).update(metrics=metrics)
        result.plan.metrics = metrics
        logger.info(
            "BoM plan applied",
            extra={
                "run_id": result.plan.id,
                "project_id": result.plan.project_id,
                "duration_ms": duration_ms,
                "node_count": result.plan.node_count,
                "operation_counts": result.plan.summary,
            },
        )
    return result
