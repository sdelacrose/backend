"""Purchase eligibility follows the current reviewed engineering composition."""
from django.core.exceptions import ValidationError
from django.conf import settings
from project_management.models import BillOfMaterialPart, ProjectWorkstation


def purchase_block_reason(part, cache=None):
    if not settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED:
        return ""
    if not part.source_workstation_id or not part.source_occurrence_id:
        return "Review required: add and agree this part in Engineering Desk, then copy it to BoM."
    cache = {} if cache is None else cache
    if part.source_workstation_id not in cache:
        station = part.source_workstation
        nodes = list(station.nodes.filter(revision__number=station.active_revision, is_active=True))
        revision = station.revisions.filter(number=station.active_revision).select_related("snapshot").first()
        cache[station.id] = (station, nodes, revision.snapshot.id if revision and hasattr(revision, "snapshot") else None)
    station, nodes, current_snapshot_id = cache[part.source_workstation_id]
    if station.project_id != part.project_id or station.status != "active" or part.engineering_detached:
        return "This part is no longer included in the active engineering BoM."
    if any(node.approval_status != "agreed" for node in nodes):
        return "Review required: all active parts and assemblies in this workstation must be Agreed."
    node = next((node for node in nodes if node.occurrence_id == part.source_occurrence_id), None)
    if not node:
        return "This part is no longer included in the active engineering revision."
    if not current_snapshot_id or part.source_workstation_snapshot_id != current_snapshot_id:
        return "Engineering has changed. Preview and copy the agreed revision to BoM again."
    if not part.is_purchasable or any(child.parent_id == node.id for child in nodes):
        return "Select an agreed purchasable part, not an assembly."
    return ""


def validate_purchase_items(*, project, items, lock=False):
    if not settings.PROJECT_WORKSTATION_COMPOSITION_ENABLED:
        return
    if not project:
        raise ValidationError("A project BoM is required for purchasing.")
    if not isinstance(items, list) or not items:
        raise ValidationError("At least one project BoM part is required for purchasing.")
    ids = []
    for item in items:
        try:
            ids.append(int(item.get("bom_part_id")))
        except (TypeError, ValueError, AttributeError):
            raise ValidationError("Every cart item must reference a project BoM part.")
    parts = list(BillOfMaterialPart.objects.filter(project=project, id__in=ids).select_related(
        "source_workstation", "source_workstation_snapshot",
    ))
    if set(ids) != {part.id for part in parts}:
        raise ValidationError("A cart item is unavailable in this project's BoM.")
    if lock:
        # Same lock as engineering revision commits; re-read after obtaining it.
        list(ProjectWorkstation.objects.select_for_update().filter(
            id__in=[part.source_workstation_id for part in parts],
        ).order_by("id"))
        parts = list(BillOfMaterialPart.objects.select_for_update(of=("self",)).filter(
            project=project, id__in=ids,
        ).select_related("source_workstation", "source_workstation_snapshot").order_by("id"))
        if set(ids) != {part.id for part in parts}:
            raise ValidationError("A cart item is unavailable in this project's BoM.")
    cache = {}
    errors = [f"{part.name}: {reason}" for part in parts if (reason := purchase_block_reason(part, cache))]
    if errors:
        raise ValidationError(errors)
