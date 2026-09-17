"""Atomic, project-scoped workbench edits using the existing revision engine."""
import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models import ProjectWorkstation
from project_management.services.workstation_revision import (
    WorkstationRevisionConflict, commit_workstation_revision, preview_workstation_revision,
)


@transaction.atomic
def workbench_edit(*, source, payload, actor):
    targets = payload["targets"]
    ids = [item["id"] for item in targets]
    if len(set(ids)) != len(ids):
        raise ValidationError({"targets": "Select each workstation only once."})
    stations = {row.pk: row for row in ProjectWorkstation.objects.select_for_update().filter(
        pk__in={source.pk, *ids}, project_id=source.project_id,
        status=ProjectWorkstation.STATUS_ACTIVE,
    ).order_by("pk")}
    if len(stations) != len({source.pk, *ids}):
        raise ValidationError({"targets": "All workstations must be active and belong to this project."})
    source = stations[source.pk]
    fingerprint = hashlib.sha256(json.dumps(
        {key: value for key, value in payload.items() if key not in {"commit", "idempotency_key"}},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    key = "workbench:" + hashlib.sha256(payload["idempotency_key"].encode()).hexdigest()
    existing = [stations[item["id"]].revisions.filter(idempotency_key=key).first() for item in targets]
    if any(existing):
        if not all(existing) or any(row.summary.get("workbench_request") != fingerprint for row in existing):
            raise ValidationError({"idempotency_key": "This request key was already used for different changes."})
        return [{"id": item["id"], "name": stations[item["id"]].name,
                 "resulting_revision": revision.number, "summary": revision.summary,
                 "nodes": revision.snapshot.content["nodes"], "before_nodes": []}
                for item, revision in zip(targets, existing)]
    if source.active_revision != payload["expected_revision"]:
        raise WorkstationRevisionConflict(source.active_revision)
    original = source.nodes.filter(revision__number=source.active_revision,
                                   pk=payload["source_node"], is_active=True).first()
    if not original:
        raise ValidationError({"source_node": "Select an active occurrence in the current source revision."})
    plans = []
    for item in targets:
        station = stations[item["id"]]
        if payload["mode"] == "copy":
            operations = [{"op": "copy", "source_workstation": source.pk,
                           "expected_source_revision": source.active_revision,
                           "source_node": original.pk, "parent_node": item.get("parent_node")}]
        else:
            candidates = station.nodes.filter(revision__number=station.active_revision, is_active=True)
            # Catalog identity, never a display-name match.
            if original.source_project_item_id:
                candidates = candidates.filter(source_project_item_id=original.source_project_item_id)
            elif original.source_company_item_id:
                candidates = candidates.filter(source_company_item_id=original.source_company_item_id,
                                               source_project_item__isnull=True)
            else:
                raise ValidationError({"source_node": "This occurrence has no catalog identity to match."})
            matches = list(candidates.order_by("level", "sort_order", "pk"))
            if not matches:
                raise ValidationError({"targets": f"{station.name}: no active occurrence matches this catalog identity."})
            if len(matches) > 100:
                raise ValidationError({"targets": f"{station.name}: more than 100 matching occurrences; narrow the operation."})
            operations = [{"op": "replace", "node": row.pk,
                           "source_project_item": payload.get("replacement"),
                           "include_subtree": True, "quantity": str(row.quantity), "unit": row.unit}
                          for row in matches]
        preview = preview_workstation_revision(workstation=station, operations=operations,
                                               expected_revision=item["expected_revision"])
        plans.append((station, item, operations, preview))
    # Preview every target before committing any. The surrounding transaction and
    # deterministic locks prevent partial updates when one target has changed.
    results = []
    for station, item, operations, preview in sorted(plans, key=lambda plan: plan[0].pk == source.pk):
        if payload["commit"]:
            result = commit_workstation_revision(workstation=station, operations=operations,
                expected_revision=item["expected_revision"], actor=actor,
                reason=payload["reason"], idempotency_key=key)
            result.revision.summary = {**result.revision.summary, "workbench_request": fingerprint}
            result.revision.save(update_fields=["summary"])
        results.append({"id": station.pk, "name": station.name,
                        "resulting_revision": preview.resulting_revision,
                        "summary": preview.summary, "nodes": preview.nodes,
                        "before_nodes": preview.before_nodes})
    return results
