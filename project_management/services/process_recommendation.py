import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from project_management.models import (
    CompanySolutionSpaceItem,
    ProcessAssemblyRecommendationRun,
    Requirement,
)
from project_management.services.workstation_composition import create_project_workstation


TOKEN_PATTERN = re.compile(r"[\w-]+", re.UNICODE)
POLICY_VERSION = "1"
REVIEWED_STATUSES = {"approved", "closed", "reviewed"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecommendationCandidate:
    process_assembly_id: int
    name: str
    score: Decimal
    evidence: list[dict]
    missing_evidence: list[dict]
    grounded_decisive_evidence: bool
    conflicts: list[str]


@dataclass(frozen=True)
class RecommendationDecision:
    automatic: bool
    candidate_id: int | None
    reason: str
    margin: Decimal | None


def _tokens(*values):
    return {
        token.casefold()
        for value in values
        for token in TOKEN_PATTERN.findall(str(value or ""))
        if len(token) > 1
    }


def decide_automatic_selection(
    candidates,
    *,
    minimum_score=Decimal("0.85"),
    minimum_margin=Decimal("0.15"),
):
    if not candidates:
        return RecommendationDecision(False, None, "no_candidates", None)
    ordered = sorted(candidates, key=lambda candidate: (-candidate.score, candidate.process_assembly_id))
    top = ordered[0]
    if top.score < minimum_score:
        return RecommendationDecision(False, top.process_assembly_id, "below_score_threshold", None)
    if not top.grounded_decisive_evidence:
        return RecommendationDecision(False, top.process_assembly_id, "missing_decisive_evidence", None)
    if top.conflicts:
        return RecommendationDecision(False, top.process_assembly_id, "hard_conflict", None)
    margin = top.score - ordered[1].score if len(ordered) > 1 else top.score
    if margin < minimum_margin:
        return RecommendationDecision(False, top.process_assembly_id, "ambiguous_margin", margin)
    return RecommendationDecision(True, top.process_assembly_id, "policy_thresholds_met", margin)


def rank_process_assemblies(project):
    requirements = list(
        Requirement.objects.filter(
            project=project,
            status__in=REVIEWED_STATUSES,
        ).order_by("id")
    )
    catalog = list(
        CompanySolutionSpaceItem.objects.filter(
            company_profile_id=project.company_profile_id,
            assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
            parent__isnull=True,
        ).order_by("id")
    )
    input_data = {
        "project": project.id,
        "requirements": [
            {
                "id": requirement.id,
                "name": requirement.name,
                "object_number": requirement.object_number,
                "skill": requirement.skill,
                "status": requirement.status,
                "custom_fields": requirement.custom_fields,
            }
            for requirement in requirements
        ],
        "catalog": [
            {
                "id": item.id,
                "name": item.assembly_name,
                "required_skills": item.required_skills,
                "provided_skills": item.provided_skills,
                "custom_fields": item.custom_fields,
            }
            for item in catalog
        ],
    }
    input_hash = hashlib.sha256(
        json.dumps(input_data, sort_keys=True, separators=(",", ":"), default=str).encode(),
    ).hexdigest()
    requirement_tokens = {}
    all_requirement_tokens = set()
    for requirement in requirements:
        tokens = _tokens(
            requirement.name,
            requirement.object_number,
            requirement.skill,
            json.dumps(requirement.custom_fields or {}, sort_keys=True),
        )
        requirement_tokens[requirement.id] = tokens
        all_requirement_tokens.update(tokens)
    candidates = []
    for item in catalog:
        candidate_tokens = _tokens(
            item.assembly_name,
            *list(item.required_skills or []),
            *list(item.provided_skills or []),
            json.dumps(item.custom_fields or {}, sort_keys=True),
        )
        evidence = []
        missing_evidence = []
        matched_all = set()
        for requirement in requirements:
            matched = sorted(requirement_tokens[requirement.id] & candidate_tokens)
            if matched:
                evidence.append(
                    {
                        "requirement": requirement.id,
                        "requirement_name": requirement.name,
                        "matched_tokens": matched,
                    }
                )
                matched_all.update(matched)
            else:
                missing_evidence.append(
                    {
                        "requirement": requirement.id,
                        "requirement_name": requirement.name,
                    }
                )
        score = (
            Decimal(len(matched_all)) / Decimal(len(all_requirement_tokens))
            if all_requirement_tokens
            else Decimal("0")
        )
        conflicts = [str(value) for value in (item.custom_fields or {}).get("hard_conflicts", [])]
        candidates.append(
            RecommendationCandidate(
                process_assembly_id=item.id,
                name=item.assembly_name,
                score=score.quantize(Decimal("0.000001")),
                evidence=evidence,
                missing_evidence=missing_evidence,
                grounded_decisive_evidence=bool(requirements and not missing_evidence),
                conflicts=conflicts,
            )
        )
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.name.casefold(), candidate.process_assembly_id))
    return candidates, input_hash


def _serialize_candidate(candidate):
    return {
        "process_assembly_id": candidate.process_assembly_id,
        "name": candidate.name,
        "score": str(candidate.score),
        "evidence": candidate.evidence,
        "missing_evidence": candidate.missing_evidence,
        "grounded_decisive_evidence": candidate.grounded_decisive_evidence,
        "conflicts": candidate.conflicts,
    }


@transaction.atomic
def create_process_recommendation_run(project, actor):
    started = time.monotonic()
    candidates, input_hash = rank_process_assemblies(project)
    existing = ProcessAssemblyRecommendationRun.objects.filter(
        project=project,
        input_hash=input_hash,
        policy_version=POLICY_VERSION,
    ).first()
    if existing:
        return existing
    minimum_score = Decimal(str(settings.PROCESS_RECOMMENDATION_MINIMUM_SCORE))
    minimum_margin = Decimal(str(settings.PROCESS_RECOMMENDATION_MINIMUM_MARGIN))
    decision = decide_automatic_selection(
        candidates,
        minimum_score=minimum_score,
        minimum_margin=minimum_margin,
    )
    tenant_policy_enabled = (
        project.company_profile_id
        in settings.AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_TENANT_IDS
    )
    automatic_selection_enabled = (
        settings.AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED
        and tenant_policy_enabled
    )
    run = ProcessAssemblyRecommendationRun.objects.create(
        project=project,
        input_hash=input_hash,
        policy_version=POLICY_VERSION,
        policy={
            "minimum_score": str(minimum_score),
            "minimum_margin": str(minimum_margin),
            "requires_grounded_decisive_evidence": True,
            "rejects_hard_conflicts": True,
            "feature_enabled": settings.AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED,
            "tenant_policy_enabled": tenant_policy_enabled,
        },
        ranked_candidates=[_serialize_candidate(candidate) for candidate in candidates],
        selection_outcome=(
            ProcessAssemblyRecommendationRun.OUTCOME_AUTOMATIC
            if decision.automatic and automatic_selection_enabled
            else ProcessAssemblyRecommendationRun.OUTCOME_REVIEW
        ),
        decision_reason=(
            decision.reason
            if automatic_selection_enabled
            else (
                "tenant_policy_disabled"
                if settings.AUTO_PROCESS_ASSEMBLY_RECOMMENDATION_ENABLED
                else "automatic_selection_disabled"
            )
        ),
        selected_candidate_id=(
            decision.candidate_id
            if decision.automatic and automatic_selection_enabled
            else None
        ),
        created_by=actor,
    )
    if run.selection_outcome == ProcessAssemblyRecommendationRun.OUTCOME_AUTOMATIC:
        selected = CompanySolutionSpaceItem.objects.get(
            id=run.selected_candidate_id,
            company_profile_id=project.company_profile_id,
        )
        result = create_project_workstation(
            project=project,
            source=selected,
            actor=actor,
            idempotency_key=f"process-recommendation-{run.id}-{selected.id}",
        )
        run.created_workstation = result.workstation
        run.save(update_fields=["created_workstation", "updated_at"])
    logger.info(
        "Process Assembly recommendation completed",
        extra={
            "run_id": run.id,
            "project_id": project.id,
            "input_hash": run.input_hash,
            "candidate_count": len(candidates),
            "selection_outcome": run.selection_outcome,
            "decision_reason": run.decision_reason,
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        },
    )
    return run


@transaction.atomic
def override_process_recommendation(*, run, candidate_id, actor, reason):
    reason = str(reason or "").strip()
    if not reason:
        raise ValidationError({"reason": "Explain why this alternative was selected."})
    locked = ProcessAssemblyRecommendationRun.objects.select_for_update().select_related("project").get(pk=run.pk)
    candidate_ids = {
        int(candidate["process_assembly_id"])
        for candidate in locked.ranked_candidates
    }
    if int(candidate_id) not in candidate_ids:
        raise ValidationError({"candidate": "Select a ranked Process Assembly candidate."})
    selected = CompanySolutionSpaceItem.objects.filter(
        id=candidate_id,
        company_profile_id=locked.project.company_profile_id,
        assembly_type=CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES,
        parent__isnull=True,
    ).first()
    if not selected:
        raise ValidationError({"candidate": "The Process Assembly is unavailable."})
    history = list(locked.override_history or [])
    history.append(
        {
            "actor": actor.id,
            "reason": reason,
            "previous_candidate": locked.selected_candidate_id,
            "candidate": selected.id,
            "policy_version": locked.policy_version,
        }
    )
    result = create_project_workstation(
        project=locked.project,
        source=selected,
        actor=actor,
        idempotency_key=f"process-recommendation-{locked.id}-{selected.id}",
    )
    previous_workstation = locked.created_workstation
    if previous_workstation and previous_workstation.id != result.workstation.id:
        previous_workstation.status = previous_workstation.STATUS_ARCHIVED
        previous_workstation.modified_by = actor
        previous_workstation.save(update_fields=["status", "modified_by", "updated_at"])
    if result.workstation.status != result.workstation.STATUS_ACTIVE:
        result.workstation.status = result.workstation.STATUS_ACTIVE
        result.workstation.modified_by = actor
        result.workstation.save(update_fields=["status", "modified_by", "updated_at"])
    locked.selection_outcome = ProcessAssemblyRecommendationRun.OUTCOME_MANUAL
    locked.decision_reason = "manual_override"
    locked.selected_candidate = selected
    locked.created_workstation = result.workstation
    locked.override_history = history
    locked.save(
        update_fields=[
            "selection_outcome",
            "decision_reason",
            "selected_candidate",
            "created_workstation",
            "override_history",
            "updated_at",
        ],
    )
    logger.info(
        "Process Assembly recommendation overridden",
        extra={
            "run_id": locked.id,
            "project_id": locked.project_id,
            "selection_outcome": locked.selection_outcome,
            "candidate_id": selected.id,
        },
    )
    return locked
