from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError

from project_management.models import (
    ProjectWorkstation,
    Requirement,
    WorkstationComplianceRule,
)
from .compliance_properties import aas_properties


def numeric(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("A finite numeric value is required.")
    if not result.is_finite():
        raise ValidationError("A finite numeric value is required.")
    return result


def requirement_ids(workstation):
    parents = dict(
        Requirement.objects.filter(project_id=workstation.project_id).values_list(
            "id", "parent_id"
        )
    )
    allowed = set()
    for candidate in parents:
        cursor, visited = candidate, set()
        while cursor in parents and cursor not in visited:
            if cursor == workstation.source_requirement_id:
                allowed.add(candidate)
                break
            visited.add(cursor)
            cursor = parents[cursor]
    return allowed


def evidence(workstation, rule, actor):
    node = workstation.nodes.filter(
        revision__number=workstation.active_revision,
        occurrence_id=rule.occurrence,
        is_active=True,
    ).first()
    fields = node.custom_fields if node and isinstance(node.custom_fields, dict) else {}
    bound = rule.requirement_id in requirement_ids(workstation)
    identity = (
        [node.source_company_item_id, node.source_project_item_id] if node else None
    )
    source_bound = identity == rule.definition.get("source_identity")
    actual = fields.get(rule.definition["property"]) if bound and source_bound else None
    source_hash, source_access = None, True
    if rule.definition["source"] == "aas":
        properties, source_hash = aas_properties(workstation, node, actor)
        source_access = properties is not None
        actual = (
            (properties or {}).get(rule.definition["property"])
            if bound and source_bound
            else None
        )
    requirement = (
        Requirement.objects.filter(pk=rule.requirement_id)
        .values("name", "custom_fields", "skill", "status", "parent_id", "project_id")
        .first()
    )
    return {
        "revision": workstation.active_revision,
        "actual": actual,
        "requirement": requirement,
        "definition": rule.definition,
        "requirement_bound": bound,
        "source_hash": source_hash,
        "source_access": source_access,
        "source_bound": source_bound,
    }


def representation(workstation, rule, actor):
    current = evidence(workstation, rule, actor)
    last = rule.evaluations.order_by("-id").first()
    result = None
    if last:
        actual = last.evidence.get("actual") or {}
        if not isinstance(actual, dict):
            actual = {}
        result = {
            "outcome": last.outcome,
            "reason": last.reason,
            "actual": actual.get("value"),
            "unit": actual.get("unit"),
            "stale": last.rule_version != rule.version or last.evidence != current,
            "evaluated_at": last.created_at,
        }
        if not current["source_access"]:
            result.update(
                outcome="unknown",
                reason="The source is unavailable or access was removed.",
                actual=None,
                unit=None,
                stale=True,
            )
    history = [
        {
            "id": item.pk,
            "rule_version": item.rule_version,
            "outcome": item.outcome,
            "reason": item.reason,
            "definition": item.evidence.get("definition"),
            "evaluated_at": item.created_at,
        }
        for item in rule.evaluations.order_by("-id")
    ]
    for entry, item in zip(history, rule.evaluations.order_by("-id")):
        recorded = item.evidence.get("actual")
        entry.update(
            actual=recorded.get("value") if isinstance(recorded, dict) else None,
            unit=recorded.get("unit") if isinstance(recorded, dict) else None,
            revision=item.evidence.get("revision"),
            evaluator=item.created_by_id,
        )
    if not current["source_access"]:
        history = []
    return {
        "id": rule.pk,
        "title": rule.title,
        "version": rule.version,
        "archived": rule.archived,
        "requirement": rule.requirement_id,
        "occurrence": str(rule.occurrence),
        **rule.definition,
        "result": result,
        "history": history,
    }


@transaction.atomic
def compliance(workstation, actor, data=None, occurrence=None):
    workstation = ProjectWorkstation.objects.select_for_update().get(pk=workstation.pk)
    if data is None:
        rows = [
            representation(workstation, rule, actor)
            for rule in workstation.compliance_rules.order_by("id")
        ]
        properties, snapshot = {}, []
        if occurrence:
            try:
                occurrence = UUID(str(occurrence))
            except ValueError:
                raise ValidationError("A valid occurrence ID is required.")
            node = get_object_or_404(
                workstation.nodes,
                revision__number=workstation.active_revision,
                occurrence_id=occurrence,
                is_active=True,
            )
            properties, _ = aas_properties(workstation, node, actor)
            if isinstance(node.custom_fields, dict):
                snapshot = [
                    {
                        **(value if isinstance(value, dict) else {"value": value}),
                        "id": key,
                        "label": key,
                    }
                    for key, value in node.custom_fields.items()
                ]
        requirements = list(
            Requirement.objects.filter(pk__in=requirement_ids(workstation)).values(
                "id", "name"
            )
        )
        return {
            "rules": [row for row in rows if not row["archived"]],
            "archived_rules": [row for row in rows if row["archived"]],
            "requirements": requirements,
            "properties": {
                "aas": list((properties or {}).values()),
                "snapshot": snapshot,
            },
        }, 200
    if not isinstance(data, Mapping) or not isinstance(data.get("action"), str):
        raise ValidationError("A valid compliance action is required.")
    for field in ("id", "version", "requirement"):
        if field in data and (type(data[field]) is not int or data[field] < 1):
            if field == "version" and data.get("action") in {
                "update",
                "archive",
                "evaluate",
            }:
                return {"detail": "The rule changed. Reload before continuing."}, 409
            raise ValidationError({field: "A positive integer is required."})
    rule = None
    if data.get("action") in {"update", "archive", "evaluate"}:
        rule = get_object_or_404(
            workstation.compliance_rules, pk=data.get("id"), archived=False
        )
        if data.get("version") != rule.version:
            return {"detail": "The rule changed. Reload before continuing."}, 409
    if data.get("action") == "archive":
        rule.archived = True
        rule.version += 1
        rule.definition = {**rule.definition, "editor": actor.pk}
        rule.save(update_fields=["archived", "version", "definition", "updated_at"])
        return representation(workstation, rule, actor), 200
    if data.get("action") in {"create", "update"}:
        kind = data.get("value_type")
        if (
            not isinstance(kind, str)
            or not isinstance(data.get("operator"), str)
            or data.get("source") not in ("snapshot", "aas")
            or kind not in {"number", "text", "boolean"}
            or data.get("operator")
            not in ({"gte", "lte", "eq", "range"} if kind == "number" else {"eq"})
        ):
            raise ValidationError("Unsupported rule definition.")
        if kind == "number":
            numeric(data.get("expected"))
        elif not isinstance(data.get("expected"), str if kind == "text" else bool):
            raise ValidationError("Expected value must have the selected type.")
        if data["operator"] == "range" and numeric(data.get("upper")) < numeric(
            data["expected"]
        ):
            raise ValidationError(
                "The upper bound must not be less than the lower bound."
            )
        requirement = get_object_or_404(
            Requirement, pk=data.get("requirement"), project=workstation.project
        )
        if requirement.pk not in requirement_ids(workstation):
            raise ValidationError(
                "Select a requirement within the workstation requirement tree."
            )
        try:
            occurrence = UUID(str(data.get("occurrence")))
        except (ValueError, TypeError):
            raise ValidationError("A valid occurrence ID is required.")
        node = get_object_or_404(
            workstation.nodes,
            revision__number=workstation.active_revision,
            occurrence_id=occurrence,
            is_active=True,
        )
        if (
            not isinstance(data.get("property"), str)
            or not data["property"]
            or not isinstance(data.get("unit"), str)
        ):
            raise ValidationError("A property and explicit unit are required.")
        if data["source"] == "aas":
            properties, _ = aas_properties(workstation, node, actor)
            if data["property"] not in (properties or {}):
                raise ValidationError("Select an accessible scalar AAS property.")
        if not isinstance(data.get("title"), str):
            raise ValidationError("A title is required.")
        title = data["title"].strip()
        if not title or len(title) > 255:
            raise ValidationError("A title of at most 255 characters is required.")
        definition = {
            key: data[key]
            for key in (
                "source",
                "property",
                "value_type",
                "operator",
                "expected",
                "unit",
            )
        }
        definition["source_identity"] = [
            node.source_company_item_id,
            node.source_project_item_id,
        ]
        definition["editor"] = actor.pk
        if data["operator"] == "range":
            definition["upper"] = data["upper"]
        if rule:
            rule.requirement = requirement
            rule.occurrence = node.occurrence_id
            rule.title = title
            rule.definition = definition
            rule.version += 1
            rule.save()
        else:
            rule = WorkstationComplianceRule.objects.create(
                workstation=workstation,
                requirement=requirement,
                occurrence=node.occurrence_id,
                title=title,
                definition=definition,
                created_by=actor,
            )
        return representation(workstation, rule, actor), (
            201 if data["action"] == "create" else 200
        )
    if data.get("action") == "evaluate":
        rule = get_object_or_404(
            workstation.compliance_rules, pk=data.get("id"), archived=False
        )
        if data.get("version") != rule.version:
            return {"detail": "The rule changed. Reload before evaluating."}, 409
        observed = evidence(workstation, rule, actor)
        actual = observed["actual"]
        outcome, reason = "unknown", "The occurrence or property value is missing."
        if not observed["requirement_bound"]:
            reason = "The requirement is outside the workstation requirement tree."
        elif not observed["source_bound"]:
            reason = "The occurrence source changed. Review and rebind the rule."
        elif actual is not None and not isinstance(actual, dict):
            reason = "The recorded property needs a value and an explicit unit."
        elif isinstance(actual, dict) and "unit" not in actual:
            reason = "The recorded unit is missing."
        elif isinstance(actual, dict) and actual.get("unit") != rule.definition["unit"]:
            reason = "The recorded unit does not exactly match the rule unit."
        elif isinstance(actual, dict) and actual.get("unit") == rule.definition["unit"]:
            try:
                kind = rule.definition["value_type"]
                declared = actual.get("value_type")
                numeric_types = {
                    "xs:decimal",
                    "xs:double",
                    "xs:float",
                    "xs:integer",
                    "xs:int",
                    "xs:long",
                    "xs:short",
                    "xs:byte",
                    "xs:unsignedInt",
                    "xs:unsignedLong",
                    "xs:nonNegativeInteger",
                    "xs:positiveInteger",
                }
                allowed_types = (
                    numeric_types
                    if kind == "number"
                    else {"xs:string"} if kind == "text" else {"xs:boolean"}
                )
                if declared is not None and (not isinstance(declared, str) or declared not in allowed_types):
                    raise ValidationError(
                        "The recorded value has an incompatible declared type."
                    )
                if kind == "number":
                    value, expected = numeric(actual.get("value")), numeric(
                        rule.definition["expected"]
                    )
                else:
                    value, expected = actual.get("value"), rule.definition["expected"]
                    if not isinstance(value, str if kind == "text" else bool):
                        raise ValidationError(
                            "The recorded value has an incompatible type."
                        )
                operator = rule.definition["operator"]
                matched = {
                    "gte": value >= expected,
                    "lte": value <= expected,
                    "eq": value == expected,
                }.get(operator)
                if operator == "range":
                    matched = expected <= value <= numeric(rule.definition["upper"])
                outcome = "meets" if matched else "does_not_meet"
                reason = (
                    "Recorded rule evaluated without conversion or inferred tolerance."
                )
            except ValidationError as exc:
                reason = str(exc.detail[0])
        if evidence(workstation, rule, actor) != observed:
            return {
                "detail": "The recorded sources changed during evaluation. Reload and evaluate again."
            }, 409
        rule.evaluations.create(
            rule_version=rule.version,
            evidence=observed,
            outcome=outcome,
            reason=reason,
            created_by=actor,
        )
        return representation(workstation, rule, actor), 200
    raise ValidationError("Unsupported compliance action.")
