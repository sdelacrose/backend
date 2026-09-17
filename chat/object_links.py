import re
from typing import Dict, List, Optional, Tuple

from django.db.models import CharField, Q, Value
from django.db.models.functions import Coalesce, Concat

from companyprofileapis.models import Opportunity
from ecpmpapp.utils import require_any_permission_multiple_modules
from project_management.models import (
    ActionItem,
    Milestone,
    Project,
    ProjectPhase,
    Requirement,
)
from project_management.models.document import Document


OBJECT_TYPE_DOCUMENT = "document"
OBJECT_TYPE_ACTION_ITEM = "action_item"
OBJECT_TYPE_PHASE = "phase"
OBJECT_TYPE_PROJECT = "project"
OBJECT_TYPE_OPPORTUNITY = "opportunity"
OBJECT_TYPE_REQUIREMENT = "requirement"

OBJECT_TYPE_CHOICES = {
    OBJECT_TYPE_DOCUMENT,
    OBJECT_TYPE_ACTION_ITEM,
    OBJECT_TYPE_PHASE,
    OBJECT_TYPE_PROJECT,
    OBJECT_TYPE_OPPORTUNITY,
    OBJECT_TYPE_REQUIREMENT,
}

OBJECT_TYPE_ALIASES = {
    "document": OBJECT_TYPE_DOCUMENT,
    "documents": OBJECT_TYPE_DOCUMENT,
    "doc": OBJECT_TYPE_DOCUMENT,
    "docs": OBJECT_TYPE_DOCUMENT,
    "file": OBJECT_TYPE_DOCUMENT,
    "files": OBJECT_TYPE_DOCUMENT,
    "ai": OBJECT_TYPE_ACTION_ITEM,
    "actionitem": OBJECT_TYPE_ACTION_ITEM,
    "actionitems": OBJECT_TYPE_ACTION_ITEM,
    "action_item": OBJECT_TYPE_ACTION_ITEM,
    "action_items": OBJECT_TYPE_ACTION_ITEM,
    "phase": OBJECT_TYPE_PHASE,
    "phases": OBJECT_TYPE_PHASE,
    "face": OBJECT_TYPE_PHASE,
    "faces": OBJECT_TYPE_PHASE,
    "project_phase": OBJECT_TYPE_PHASE,
    "project_phases": OBJECT_TYPE_PHASE,
    "milestone": OBJECT_TYPE_PHASE,
    "milestones": OBJECT_TYPE_PHASE,
    "project": OBJECT_TYPE_PROJECT,
    "projects": OBJECT_TYPE_PROJECT,
    "property": OBJECT_TYPE_PROJECT,
    "properties": OBJECT_TYPE_PROJECT,
    "o": OBJECT_TYPE_OPPORTUNITY,
    "opp": OBJECT_TYPE_OPPORTUNITY,
    "opportunity": OBJECT_TYPE_OPPORTUNITY,
    "opportunities": OBJECT_TYPE_OPPORTUNITY,
    "r": OBJECT_TYPE_REQUIREMENT,
    "req": OBJECT_TYPE_REQUIREMENT,
    "requirement": OBJECT_TYPE_REQUIREMENT,
    "requirements": OBJECT_TYPE_REQUIREMENT,
}

OBJECT_TYPE_LABELS = {
    OBJECT_TYPE_DOCUMENT: "Document",
    OBJECT_TYPE_ACTION_ITEM: "Action Item",
    OBJECT_TYPE_PHASE: "Phase",
    OBJECT_TYPE_PROJECT: "Project",
    OBJECT_TYPE_OPPORTUNITY: "Opportunity",
    OBJECT_TYPE_REQUIREMENT: "Requirement",
}

CANONICAL_PREFIX_BY_OBJECT_TYPE = {
    OBJECT_TYPE_DOCUMENT: "doc",
    OBJECT_TYPE_ACTION_ITEM: "ai",
    OBJECT_TYPE_PHASE: "phase",
    OBJECT_TYPE_PROJECT: "project",
    OBJECT_TYPE_OPPORTUNITY: "o",
    OBJECT_TYPE_REQUIREMENT: "r",
}

LINKED_OBJECT_REF_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])/?([A-Za-z][A-Za-z0-9_]*)-(\d+)\b",
    re.IGNORECASE,
)


def normalize_linked_object_type(raw_value: object) -> Optional[str]:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(raw_value or "").strip().lower()).strip("_")
    if not normalized:
        return None

    alias = OBJECT_TYPE_ALIASES.get(normalized)
    if alias:
        return alias

    compact = normalized.replace("_", "")
    return OBJECT_TYPE_ALIASES.get(compact)


def normalize_linked_objects(raw_value: object) -> List[Dict[str, int]]:
    if raw_value in (None, ""):
        return []

    if not isinstance(raw_value, list):
        raise ValueError("Linked objects must be a list.")

    normalized: List[Dict[str, int]] = []
    seen: set[Tuple[str, int]] = set()

    for index, raw_item in enumerate(raw_value, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Linked object #{index} must be an object.")

        object_type = normalize_linked_object_type(
            raw_item.get("object_type") or raw_item.get("type") or raw_item.get("kind")
        )
        if object_type not in OBJECT_TYPE_CHOICES:
            raise ValueError(
                f"Linked object #{index} has an unsupported type. Supported types are: "
                "document, action_item, phase, project, opportunity, requirement."
            )

        raw_object_id = raw_item.get("object_id")
        if raw_object_id in (None, ""):
            raw_object_id = raw_item.get("id")

        try:
            object_id = int(raw_object_id)
        except (TypeError, ValueError):
            raise ValueError(f"Linked object #{index} has an invalid object_id.")

        if object_id <= 0:
            raise ValueError(f"Linked object #{index} must use a positive object_id.")

        dedupe_key = (object_type, object_id)
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        normalized.append(
            {
                "object_type": object_type,
                "object_id": object_id,
            }
        )

    return normalized


def normalize_linked_object_refs(raw_value: object) -> List[Dict[str, int]]:
    if raw_value in (None, ""):
        return []

    if isinstance(raw_value, str):
        refs = [raw_value]
    elif isinstance(raw_value, list):
        refs = raw_value
    else:
        return []

    normalized: List[Dict[str, int]] = []
    seen: set[Tuple[str, int]] = set()

    for raw_ref in refs:
        token = str(raw_ref or "").strip()
        if not token:
            continue

        token = token.lstrip("/")
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)-(\d+)", token)
        if not match:
            continue

        object_type = normalize_linked_object_type(match.group(1))
        if object_type not in OBJECT_TYPE_CHOICES:
            continue

        object_id = int(match.group(2))
        if object_id <= 0:
            continue

        dedupe_key = (object_type, object_id)
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        normalized.append(
            {
                "object_type": object_type,
                "object_id": object_id,
            }
        )

    return normalized


def extract_linked_objects_from_text(raw_text: object) -> List[Dict[str, int]]:
    text = str(raw_text or "")
    if not text.strip():
        return []

    extracted: List[Dict[str, int]] = []
    seen: set[Tuple[str, int]] = set()

    for match in LINKED_OBJECT_REF_TOKEN_PATTERN.finditer(text):
        object_type = normalize_linked_object_type(match.group(1))
        if object_type not in OBJECT_TYPE_CHOICES:
            continue

        try:
            object_id = int(match.group(2))
        except (TypeError, ValueError):
            continue

        if object_id <= 0:
            continue

        dedupe_key = (object_type, object_id)
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        extracted.append(
            {
                "object_type": object_type,
                "object_id": object_id,
            }
        )

    return extracted


def merge_linked_objects(*collections: object) -> List[Dict[str, int]]:
    merged: List[Dict[str, int]] = []
    seen: set[Tuple[str, int]] = set()

    for collection in collections:
        if not collection:
            continue
        try:
            normalized_collection = normalize_linked_objects(collection)
        except ValueError:
            continue

        for item in normalized_collection:
            dedupe_key = (item["object_type"], item["object_id"])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            merged.append(item)

    return merged


def filter_accessible_linked_objects(linked_objects: object, user) -> List[Dict[str, int]]:
    try:
        normalized = normalize_linked_objects(linked_objects)
    except ValueError:
        return []

    if not normalized:
        return []

    try:
        cards = resolve_linked_objects(normalized, user)
    except Exception:
        return []

    filtered: List[Dict[str, int]] = []
    seen: set[Tuple[str, int]] = set()
    for card in cards:
        if not card.get("can_access"):
            continue

        object_type = normalize_linked_object_type(card.get("object_type"))
        if object_type not in OBJECT_TYPE_CHOICES:
            continue

        try:
            object_id = int(card.get("object_id"))
        except (TypeError, ValueError):
            continue

        if object_id <= 0:
            continue

        dedupe_key = (object_type, object_id)
        if dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        filtered.append(
            {
                "object_type": object_type,
                "object_id": object_id,
            }
        )

    return filtered


def _has_company_projects_access(user, project: Project) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False
    if getattr(user, "is_freelancer", False):
        return False
    if not getattr(user, "company_id", None):
        return False

    project_owner_company_id = getattr(getattr(project, "user", None), "company_id", None)
    if not project_owner_company_id or project_owner_company_id != user.company_id:
        return False

    return require_any_permission_multiple_modules(
        user,
        {"projects": ["view", "full_access", "view_all_access"]},
        "company",
        {"project": project.id},
    )


def _has_project_module_access(user, modules_actions: Dict[str, List[str]], project: Project) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    if project.user_id == user.id:
        return True

    is_team_member = project.team_members.filter(team_member=user).exists()
    has_project_scope_access = require_any_permission_multiple_modules(
        user,
        modules_actions,
        "project",
        {"project": project.id},
    )
    if has_project_scope_access:
        return True

    return _has_company_projects_access(user, project)


def _can_access_project(user, project: Project) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    if project.user_id == user.id:
        return True

    is_team_member = project.team_members.filter(team_member=user).exists()
    if is_team_member and getattr(user, "is_freelancer", False):
        return True

    if is_team_member and require_any_permission_multiple_modules(
        user,
        {"projects": ["view_assigned_created"]},
        "company",
        {"project": project.id},
    ):
        return True

    return _has_project_module_access(
        user,
        {"project": ["view", "full_access"]},
        project,
    ) or _has_project_module_access(
        user,
        {"projects": ["view", "full_access", "view_all_access"]},
        project,
    )


def _can_access_phase(user, milestone: Milestone) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    return _has_project_module_access(
        user,
        {"phases_management": ["view", "full_access"]},
        milestone.project,
    ) or _has_project_module_access(
        user,
        {"milestone_management": ["view", "full_access"]},
        milestone.project,
    )


def _can_access_legacy_milestone(user, milestone: Milestone) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    return _has_project_module_access(
        user,
        {"phases_management": ["view", "full_access"]},
        milestone.project,
    ) or _has_project_module_access(
        user,
        {"milestone_management": ["view", "full_access"]},
        milestone.project,
    )


def _can_access_legacy_project_phase(user, phase: ProjectPhase) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    return _has_project_module_access(
        user,
        {"phases_management": ["view", "full_access"]},
        phase.project,
    )


def _can_access_document(user, document: Document) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    folder = getattr(document, "folder", None)
    if not folder:
        return False

    if folder.project_id:
        return _has_project_module_access(
            user,
            {"documents": ["view", "full_access", "view_created"]},
            folder.project,
        )

    if folder.company_profile_id:
        if getattr(user, "is_freelancer", False):
            return False
        if folder.company_profile_id != getattr(user, "company_id", None):
            return False
        return require_any_permission_multiple_modules(
            user,
            {"documents": ["view", "full_access", "view_created"]},
            "company",
            {"project": None},
        )

    if folder.freelancer_id:
        return int(folder.freelancer_id) == int(getattr(user, "id", -1))

    return False


def _can_access_action_item(user, action_item: ActionItem) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    scoped_action_item = _effective_action_item_scope(action_item)

    def _is_assigned_or_created(target_action_item: ActionItem) -> bool:
        return target_action_item.assignees.filter(id=user.id).exists() or int(
            getattr(target_action_item, "created_by_id", -1)
        ) == int(user.id)

    if scoped_action_item.milestone_id:
        project = scoped_action_item.milestone.project
        has_general_access = _has_project_module_access(
            user,
            {"action_item": ["view", "full_access"]},
            project,
        )
        if has_general_access:
            return True

        has_assigned_created_access = _has_project_module_access(
            user,
            {"action_item": ["view_assigned_created"]},
            project,
        )
        if not has_assigned_created_access:
            return False

        return _is_assigned_or_created(action_item) or (
            scoped_action_item.id != action_item.id
            and _is_assigned_or_created(scoped_action_item)
        )

    if scoped_action_item.company_profile_id:
        if getattr(user, "is_freelancer", False):
            return False
        user_company_id = getattr(user, "company_id", None)
        if not user_company_id:
            return False
        if int(scoped_action_item.company_profile_id) != int(user_company_id):
            return False
        has_general_access = require_any_permission_multiple_modules(
            user,
            {"action_item": ["view", "full_access"]},
            "company",
            {"project": None},
        )
        if has_general_access:
            return True

        has_assigned_created_access = require_any_permission_multiple_modules(
            user,
            {"action_item": ["view_assigned_created"]},
            "company",
            {"project": None},
        )
        if not has_assigned_created_access:
            return False

        return _is_assigned_or_created(action_item) or (
            scoped_action_item.id != action_item.id
            and _is_assigned_or_created(scoped_action_item)
        )

    if scoped_action_item.freelancer_id:
        return int(scoped_action_item.freelancer_id) == int(getattr(user, "id", -1))

    return False


def _can_access_opportunity(user, opportunity: Opportunity) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    has_full_access = require_any_permission_multiple_modules(
        user,
        {"opportunity_offers": ["full_access"]},
        "company",
        {},
    )
    has_view_access = require_any_permission_multiple_modules(
        user,
        {"opportunity_offers": ["view"]},
        "company",
        {},
    )
    has_view_created_access = require_any_permission_multiple_modules(
        user,
        {"opportunity_offers": ["view_created"]},
        "company",
        {},
    )

    if not (has_full_access or has_view_access or has_view_created_access):
        return False

    if getattr(user, "is_freelancer", False):
        if int(getattr(opportunity, "freelancer_id", -1)) != int(getattr(user, "id", -2)):
            return False
        if has_full_access or has_view_access:
            return True
        return int(getattr(opportunity, "created_by_id", -1)) == int(getattr(user, "id", -2))

    if int(getattr(opportunity, "company_profile_id", -1)) != int(getattr(user, "company_id", -2)):
        return False
    if has_full_access or has_view_access:
        return True
    return int(getattr(opportunity, "created_by_id", -1)) == int(getattr(user, "id", -2))


def _can_access_requirement(user, requirement: Requirement) -> bool:
    if not user or getattr(user, "is_anonymous", False):
        return False

    project = getattr(requirement, "project", None)
    if not project:
        return False

    if int(getattr(project, "user_id", -1)) == int(getattr(user, "id", -2)):
        return True

    if project.team_members.filter(team_member=user).exists():
        return True

    if getattr(user, "is_freelancer", False):
        return False

    user_company_id = getattr(user, "company_id", None)
    if not user_company_id:
        return False

    project_owner_company_id = getattr(getattr(project, "user", None), "company_id", None)
    return int(getattr(project, "company_profile_id", -1)) == int(user_company_id) or int(
        project_owner_company_id or -1
    ) == int(user_company_id)


def _effective_action_item_scope(action_item: ActionItem) -> ActionItem:
    current = action_item
    seen_ids = set()

    while current and current.id not in seen_ids:
        if (
            getattr(current, "milestone_id", None)
            or getattr(current, "company_profile_id", None)
            or getattr(current, "freelancer_id", None)
        ):
            return current

        seen_ids.add(current.id)
        current = getattr(current, "parent_action_item", None)

    return action_item


def _placeholder_card(object_type: str, object_id: int) -> Dict[str, object]:
    canonical_prefix = CANONICAL_PREFIX_BY_OBJECT_TYPE.get(object_type)
    return {
        "object_type": object_type,
        "object_id": object_id,
        "type_label": OBJECT_TYPE_LABELS.get(object_type, "Object"),
        "title": "Restricted object",
        "status": None,
        "project_name": None,
        "can_access": False,
        "target_url": None,
        "is_placeholder": True,
        "placeholder_text": "You do not have access to this object.",
        "ref_token": f"{canonical_prefix}-{object_id}" if canonical_prefix else None,
    }


def _missing_card(object_type: str, object_id: int) -> Dict[str, object]:
    canonical_prefix = CANONICAL_PREFIX_BY_OBJECT_TYPE.get(object_type)
    return {
        "object_type": object_type,
        "object_id": object_id,
        "type_label": OBJECT_TYPE_LABELS.get(object_type, "Object"),
        "title": "Object unavailable",
        "status": None,
        "project_name": None,
        "can_access": False,
        "target_url": None,
        "is_placeholder": True,
        "placeholder_text": "This linked object is no longer available.",
        "ref_token": f"{canonical_prefix}-{object_id}" if canonical_prefix else None,
    }


def _project_link(project_id: int) -> str:
    return f"/projects?id={project_id}"


def _phase_link(milestone: Milestone) -> str:
    return f"/project-milestone-management?phase_id={milestone.id}&project_id={milestone.project_id}"


def _legacy_milestone_link(milestone: Milestone) -> str:
    return f"/project-milestone-management?phase_id={milestone.id}&project_id={milestone.project_id}"


def _action_item_link(action_item: ActionItem) -> str:
    scoped_action_item = _effective_action_item_scope(action_item)

    if scoped_action_item.milestone_id:
        project_id = getattr(scoped_action_item.milestone, "project_id", None)
        if project_id:
            return f"/project-action-items?id={action_item.id}&project_id={project_id}"
        return f"/project-action-items?id={action_item.id}"
    return f"/company-action-items?id={action_item.id}"


def _document_link(document: Document) -> str:
    folder = getattr(document, "folder", None)
    if folder and folder.project_id:
        return f"/project-document-management?id={document.id}&project_id={folder.project_id}"
    return f"/company-documents?id={document.id}"


def _document_display_name(document: Document) -> str:
    file_name = (document.file_name or "").strip()
    full_document_number = (getattr(document, "full_document_number", "") or "").strip()

    if full_document_number and file_name:
        return f"{full_document_number}_{file_name}"
    if full_document_number:
        return full_document_number
    if file_name:
        return file_name
    return f"Document #{document.id}"


def _opportunity_link(opportunity_id: int) -> str:
    return f"/opportunities?id={opportunity_id}"


def _requirement_link(requirement: Requirement) -> str:
    return f"/project-system-requirements/technical-requirements?id={requirement.id}&project_id={requirement.project_id}"


def _card_for_project(user, project: Project) -> Dict[str, object]:
    if not _can_access_project(user, project):
        return _placeholder_card(OBJECT_TYPE_PROJECT, project.id)

    return {
        "object_type": OBJECT_TYPE_PROJECT,
        "object_id": project.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_PROJECT],
        "title": (project.project_name or "").strip() or f"Project #{project.id}",
        "status": (project.project_status or "").strip() or None,
        "project_name": (project.project_name or "").strip() or None,
        "can_access": True,
        "target_url": _project_link(project.id),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_PROJECT]}-{project.id}",
    }


def _card_for_phase(user, milestone: Milestone) -> Dict[str, object]:
    if not _can_access_phase(user, milestone):
        return _placeholder_card(OBJECT_TYPE_PHASE, milestone.id)

    return {
        "object_type": OBJECT_TYPE_PHASE,
        "object_id": milestone.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_PHASE],
        "title": (milestone.name or "").strip()
        or (milestone.phase_title or "").strip()
        or f"Phase #{milestone.id}",
        "status": (milestone.status or "").strip() or None,
        "project_name": (getattr(milestone.project, "project_name", "") or "").strip()
        or None,
        "can_access": True,
        "target_url": _phase_link(milestone),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_PHASE]}-{milestone.id}",
    }


def _card_for_legacy_milestone(user, milestone: Milestone) -> Dict[str, object]:
    if not _can_access_legacy_milestone(user, milestone):
        return _placeholder_card(OBJECT_TYPE_PHASE, milestone.id)

    return {
        "object_type": OBJECT_TYPE_PHASE,
        "object_id": milestone.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_PHASE],
        "title": (milestone.name or "").strip()
        or (milestone.phase_title or "").strip()
        or f"Phase #{milestone.id}",
        "status": (milestone.status or "").strip() or None,
        "project_name": (getattr(milestone.project, "project_name", "") or "").strip()
        or None,
        "can_access": True,
        "target_url": _legacy_milestone_link(milestone),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_PHASE]}-{milestone.id}",
    }


def _card_for_legacy_project_phase(user, phase: ProjectPhase) -> Dict[str, object]:
    if not _can_access_legacy_project_phase(user, phase):
        return _placeholder_card(OBJECT_TYPE_PHASE, phase.id)

    return {
        "object_type": OBJECT_TYPE_PHASE,
        "object_id": phase.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_PHASE],
        "title": (phase.name or "").strip() or f"Phase #{phase.id}",
        "status": (phase.status or "").strip() or None,
        "project_name": (getattr(phase.project, "project_name", "") or "").strip()
        or None,
        "can_access": True,
        "target_url": f"/project-milestone-management?phase_id={phase.id}&project_id={phase.project_id}",
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_PHASE]}-{phase.id}",
    }


def _card_for_action_item(user, action_item: ActionItem) -> Dict[str, object]:
    scoped_action_item = _effective_action_item_scope(action_item)
    project_name = None
    if getattr(scoped_action_item, "milestone_id", None) and getattr(
        scoped_action_item,
        "milestone",
        None,
    ):
        project_name = (
            getattr(getattr(scoped_action_item.milestone, "project", None), "project_name", "")
            or ""
        ).strip() or None

    if not _can_access_action_item(user, action_item):
        return _placeholder_card(OBJECT_TYPE_ACTION_ITEM, action_item.id)

    return {
        "object_type": OBJECT_TYPE_ACTION_ITEM,
        "object_id": action_item.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_ACTION_ITEM],
        "title": (action_item.name or "").strip() or f"Action Item #{action_item.id}",
        "status": (action_item.status or "").strip() or None,
        "project_name": project_name,
        "can_access": True,
        "target_url": _action_item_link(action_item),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_ACTION_ITEM]}-{action_item.id}",
    }


def _card_for_document(user, document: Document) -> Dict[str, object]:
    folder = getattr(document, "folder", None)
    project_name = None
    if folder and getattr(folder, "project_id", None) and getattr(folder, "project", None):
        project_name = (getattr(folder.project, "project_name", "") or "").strip() or None

    if not _can_access_document(user, document):
        return _placeholder_card(OBJECT_TYPE_DOCUMENT, document.id)

    return {
        "object_type": OBJECT_TYPE_DOCUMENT,
        "object_id": document.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_DOCUMENT],
        "title": _document_display_name(document),
        "status": (document.status or "").strip() or None,
        "project_name": project_name,
        "can_access": True,
        "target_url": _document_link(document),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_DOCUMENT]}-{document.id}",
    }


def _card_for_opportunity(user, opportunity: Opportunity) -> Dict[str, object]:
    if not _can_access_opportunity(user, opportunity):
        return _placeholder_card(OBJECT_TYPE_OPPORTUNITY, opportunity.id)

    return {
        "object_type": OBJECT_TYPE_OPPORTUNITY,
        "object_id": opportunity.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_OPPORTUNITY],
        "title": (opportunity.title or "").strip() or f"Opportunity #{opportunity.id}",
        "status": (opportunity.status or "").strip() or None,
        "project_name": None,
        "can_access": True,
        "target_url": _opportunity_link(opportunity.id),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_OPPORTUNITY]}-{opportunity.id}",
    }


def _card_for_requirement(user, requirement: Requirement) -> Dict[str, object]:
    if not _can_access_requirement(user, requirement):
        return _placeholder_card(OBJECT_TYPE_REQUIREMENT, requirement.id)

    return {
        "object_type": OBJECT_TYPE_REQUIREMENT,
        "object_id": requirement.id,
        "type_label": OBJECT_TYPE_LABELS[OBJECT_TYPE_REQUIREMENT],
        "title": (requirement.name or "").strip() or f"Requirement #{requirement.id}",
        "status": None,
        "project_name": (getattr(requirement.project, "project_name", "") or "").strip() or None,
        "sequence": requirement.get_sequence(),
        "object_number": (requirement.object_number or "").strip() or None,
        "can_access": True,
        "target_url": _requirement_link(requirement),
        "is_placeholder": False,
        "placeholder_text": None,
        "ref_token": f"{CANONICAL_PREFIX_BY_OBJECT_TYPE[OBJECT_TYPE_REQUIREMENT]}-{requirement.id}",
    }


def resolve_linked_objects(linked_objects: object, user) -> List[Dict[str, object]]:
    normalized = normalize_linked_objects(linked_objects)
    if not normalized:
        return []

    by_type: Dict[str, set[int]] = {
        OBJECT_TYPE_DOCUMENT: set(),
        OBJECT_TYPE_ACTION_ITEM: set(),
        OBJECT_TYPE_PHASE: set(),
        OBJECT_TYPE_PROJECT: set(),
        OBJECT_TYPE_OPPORTUNITY: set(),
        OBJECT_TYPE_REQUIREMENT: set(),
    }

    for item in normalized:
        by_type[item["object_type"]].add(item["object_id"])

    documents = {
        item.id: item
        for item in Document.objects.filter(id__in=by_type[OBJECT_TYPE_DOCUMENT]).select_related(
            "folder",
            "folder__project",
        )
    }
    action_items = {
        item.id: item
        for item in ActionItem.objects.filter(id__in=by_type[OBJECT_TYPE_ACTION_ITEM]).select_related(
            "milestone",
            "milestone__project",
            "assignee",
            "created_by",
            "parent_action_item",
            "parent_action_item__milestone",
            "parent_action_item__milestone__project",
            "parent_action_item__assignee",
            "parent_action_item__created_by",
            "parent_action_item__company_profile",
            "parent_action_item__freelancer",
        )
    }
    phases = {
        item.id: item
        for item in Milestone.objects.filter(id__in=by_type[OBJECT_TYPE_PHASE]).select_related(
            "project",
            "project__user",
        )
    }
    legacy_project_phases = {
        item.id: item
        for item in ProjectPhase.objects.filter(id__in=by_type[OBJECT_TYPE_PHASE]).select_related(
            "project",
            "project__user",
        )
    }
    projects = {
        item.id: item
        for item in Project.objects.filter(id__in=by_type[OBJECT_TYPE_PROJECT]).select_related("user")
    }
    opportunities = {
        item.id: item
        for item in Opportunity.objects.filter(id__in=by_type[OBJECT_TYPE_OPPORTUNITY]).select_related(
            "company_profile",
            "freelancer",
            "created_by",
        )
    }
    requirements = {
        item.id: item
        for item in Requirement.objects.filter(id__in=by_type[OBJECT_TYPE_REQUIREMENT]).select_related(
            "project",
            "project__user",
        )
    }

    cards: List[Dict[str, object]] = []
    for item in normalized:
        object_type = item["object_type"]
        object_id = item["object_id"]

        if object_type == OBJECT_TYPE_DOCUMENT:
            document = documents.get(object_id)
            cards.append(_card_for_document(user, document) if document else _missing_card(object_type, object_id))
        elif object_type == OBJECT_TYPE_ACTION_ITEM:
            action_item = action_items.get(object_id)
            cards.append(
                _card_for_action_item(user, action_item)
                if action_item
                else _missing_card(object_type, object_id)
            )
        elif object_type == OBJECT_TYPE_PHASE:
            phase = phases.get(object_id)
            if phase:
                cards.append(_card_for_phase(user, phase))
            else:
                legacy_milestone = legacy_project_phases.get(object_id)
                cards.append(
                    _card_for_legacy_project_phase(user, legacy_milestone)
                    if legacy_milestone
                    else _missing_card(object_type, object_id)
                )
        elif object_type == OBJECT_TYPE_PROJECT:
            project = projects.get(object_id)
            cards.append(_card_for_project(user, project) if project else _missing_card(object_type, object_id))
        elif object_type == OBJECT_TYPE_OPPORTUNITY:
            opportunity = opportunities.get(object_id)
            cards.append(
                _card_for_opportunity(user, opportunity)
                if opportunity
                else _missing_card(object_type, object_id)
            )
        elif object_type == OBJECT_TYPE_REQUIREMENT:
            requirement = requirements.get(object_id)
            cards.append(
                _card_for_requirement(user, requirement)
                if requirement
                else _missing_card(object_type, object_id)
            )

    return cards


def _normalize_search_types(raw_types: object) -> List[str]:
    if raw_types in (None, ""):
        return [
            OBJECT_TYPE_DOCUMENT,
            OBJECT_TYPE_ACTION_ITEM,
            OBJECT_TYPE_PHASE,
            OBJECT_TYPE_PROJECT,
            OBJECT_TYPE_OPPORTUNITY,
            OBJECT_TYPE_REQUIREMENT,
        ]

    if not isinstance(raw_types, list):
        return [
            OBJECT_TYPE_DOCUMENT,
            OBJECT_TYPE_ACTION_ITEM,
            OBJECT_TYPE_PHASE,
            OBJECT_TYPE_PROJECT,
            OBJECT_TYPE_OPPORTUNITY,
            OBJECT_TYPE_REQUIREMENT,
        ]

    normalized: List[str] = []
    seen = set()
    for item in raw_types:
        object_type = normalize_linked_object_type(item)
        if object_type not in OBJECT_TYPE_CHOICES:
            continue
        if object_type in seen:
            continue
        seen.add(object_type)
        normalized.append(object_type)

    if not normalized:
        return [
            OBJECT_TYPE_DOCUMENT,
            OBJECT_TYPE_ACTION_ITEM,
            OBJECT_TYPE_PHASE,
            OBJECT_TYPE_PROJECT,
            OBJECT_TYPE_OPPORTUNITY,
            OBJECT_TYPE_REQUIREMENT,
        ]

    return normalized


def _company_owned_projects_q(company_id) -> Q:
    return Q(company_profile_id=company_id) | Q(user__company_id=company_id)


def _company_owned_projects_relation_q(company_id) -> Q:
    return Q(project__company_profile_id=company_id) | Q(project__user__company_id=company_id)


def _accessible_projects_q(user, company_id=None) -> Q:
    if company_id:
        scope = _company_owned_projects_q(company_id)
    elif getattr(user, "company_id", None) and not getattr(user, "is_freelancer", False):
        scope = _company_owned_projects_q(user.company_id)
    else:
        scope = Q(user_id=getattr(user, "id", None))

    if user:
        scope |= Q(team_members__team_member=user)

    return scope


def _accessible_projects_relation_q(user, company_id=None) -> Q:
    if company_id:
        scope = _company_owned_projects_relation_q(company_id)
    elif getattr(user, "company_id", None) and not getattr(user, "is_freelancer", False):
        scope = _company_owned_projects_relation_q(user.company_id)
    else:
        scope = Q(project__user_id=getattr(user, "id", None))

    if user:
        scope |= Q(project__team_members__team_member=user)

    return scope


def _room_project_ids(room, user=None) -> List[int]:
    if getattr(room, "project_id", None):
        return [int(room.project_id)]

    company_id = getattr(room, "company_id", None)
    if company_id or user:
        return list(
            Project.objects.filter(_accessible_projects_q(user, company_id=company_id))
            .values_list("id", flat=True)
            .distinct()
        )

    return []


def _is_numeric_query(query: str) -> Optional[int]:
    query = str(query or "").strip()
    if not query:
        return None
    if not query.isdigit():
        return None
    return int(query)


def _query_tokens(query: str) -> List[str]:
    raw_query = str(query or "").strip()
    if not raw_query:
        return []

    return [token for token in re.split(r"[\s\-_]+", raw_query) if token]


def _looks_like_sequence_query(query: str) -> bool:
    normalized = str(query or "").strip()
    return bool(normalized) and bool(re.fullmatch(r"\d+(?:\.\d+)*", normalized))


def _build_token_filter(fields: List[str], tokens: List[str]) -> Q:
    if not fields or not tokens:
        return Q()

    combined_filter = Q()
    for token in tokens:
        token_filter = Q()
        for field in fields:
            token_filter |= Q(**{f"{field}__icontains": token})
        combined_filter &= token_filter

    return combined_filter


def _scoped_requirements_queryset(user, room):
    requirements_qs = Requirement.objects.select_related("project", "project__user")

    if getattr(room, "project_id", None):
        return requirements_qs.filter(project_id=room.project_id)

    company_id = getattr(room, "company_id", None)
    return requirements_qs.filter(
        _accessible_projects_relation_q(user, company_id=company_id)
    ).distinct()


def search_linked_objects(user, room, query: str = "", object_types: object = None, limit: int = 20):
    normalized_types = _normalize_search_types(object_types)
    query = str(query or "").strip()
    query_id = _is_numeric_query(query)
    query_tokens = _query_tokens(query)

    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 50))

    project_ids = _room_project_ids(room, user)
    cards: List[Dict[str, object]] = []

    if OBJECT_TYPE_PROJECT in normalized_types:
        projects_qs = Project.objects.select_related("user")
        if getattr(room, "project_id", None):
            projects_qs = projects_qs.filter(id=room.project_id)
        else:
            projects_qs = projects_qs.filter(
                _accessible_projects_q(user, company_id=getattr(room, "company_id", None))
            ).distinct()

        if query:
            projects_qs = projects_qs.filter(
                Q(project_name__icontains=query)
                | _build_token_filter(["project_name"], query_tokens)
                | (Q(id=query_id) if query_id is not None else Q())
            )

        for project in projects_qs.order_by("-id")[: limit * 2]:
            card = _card_for_project(user, project)
            if card.get("can_access"):
                cards.append(card)
            if len(cards) >= limit:
                return cards

    if (
        OBJECT_TYPE_OPPORTUNITY in normalized_types
        and not getattr(room, "project_id", None)
    ):
        opportunities_qs = Opportunity.objects.select_related(
            "company_profile",
            "freelancer",
            "created_by",
        )
        if getattr(room, "company_id", None):
            opportunities_qs = opportunities_qs.filter(company_profile_id=room.company_id)
        elif getattr(user, "is_freelancer", False):
            opportunities_qs = opportunities_qs.filter(freelancer=user)
        elif getattr(user, "company_id", None):
            opportunities_qs = opportunities_qs.filter(company_profile_id=user.company_id)
        else:
            opportunities_qs = opportunities_qs.none()

        if query:
            opportunities_qs = opportunities_qs.filter(
                Q(title__icontains=query)
                | Q(details__icontains=query)
                | Q(type_of_service__icontains=query)
                | _build_token_filter(["title", "details", "type_of_service"], query_tokens)
                | (Q(id=query_id) if query_id is not None else Q())
            )

        for opportunity in opportunities_qs.order_by("-id")[: limit * 2]:
            card = _card_for_opportunity(user, opportunity)
            if card.get("can_access"):
                cards.append(card)
            if len(cards) >= limit:
                return cards

    if OBJECT_TYPE_PHASE in normalized_types:
        phase_search_count = 0
        phases_qs = Milestone.objects.select_related("project", "project__user")

        if getattr(room, "project_id", None):
            phases_qs = phases_qs.filter(project_id=room.project_id)
        else:
            phases_qs = phases_qs.filter(
                _accessible_projects_relation_q(
                    user, company_id=getattr(room, "company_id", None)
                )
            ).distinct()

        if query:
            phases_qs = phases_qs.filter(
                Q(name__icontains=query)
                | Q(phase_title__icontains=query)
                | _build_token_filter(["name", "phase_title"], query_tokens)
                | (Q(id=query_id) if query_id is not None else Q())
            )

        for phase in phases_qs.order_by("-id")[: limit * 2]:
            card = _card_for_phase(user, phase)
            if card.get("can_access"):
                cards.append(card)
                phase_search_count += 1
            if len(cards) >= limit:
                return cards

        if query_id is not None and phase_search_count == 0:
            fallback_phase = (
                Milestone.objects.select_related("project", "project__user")
                .filter(id=query_id)
                .first()
            )
            if fallback_phase:
                card = _card_for_phase(user, fallback_phase)
                if card.get("can_access"):
                    cards.append(card)
                    if len(cards) >= limit:
                        return cards

    if OBJECT_TYPE_ACTION_ITEM in normalized_types:
        action_items_qs = ActionItem.objects.select_related(
            "milestone",
            "milestone__project",
            "milestone__project__user",
            "assignee",
            "created_by",
            "parent_action_item",
            "parent_action_item__milestone",
            "parent_action_item__milestone__project",
            "parent_action_item__milestone__project__user",
            "parent_action_item__assignee",
            "parent_action_item__created_by",
            "parent_action_item__company_profile",
            "parent_action_item__freelancer",
        )
        if project_ids:
            action_items_filter = Q(milestone__project_id__in=project_ids) | Q(
                parent_action_item__milestone__project_id__in=project_ids
            )
            if getattr(room, "company_id", None):
                action_items_filter |= Q(company_profile_id=room.company_id) | Q(
                    parent_action_item__company_profile_id=room.company_id
                )
            action_items_qs = action_items_qs.filter(action_items_filter)
        elif getattr(room, "company_id", None):
            action_items_qs = action_items_qs.filter(
                Q(company_profile_id=room.company_id)
                | Q(parent_action_item__company_profile_id=room.company_id)
            )

        if query:
            action_items_qs = action_items_qs.filter(
                Q(name__icontains=query)
                | Q(notes__icontains=query)
                | _build_token_filter(
                    ["name", "notes", "milestone__name", "milestone__phase_title"],
                    query_tokens,
                )
                | (Q(id=query_id) if query_id is not None else Q())
            )

        for action_item in action_items_qs.order_by("-id")[: limit * 2]:
            card = _card_for_action_item(user, action_item)
            if card.get("can_access"):
                cards.append(card)
            if len(cards) >= limit:
                return cards

    if OBJECT_TYPE_REQUIREMENT in normalized_types:
        requirements_qs = _scoped_requirements_queryset(user, room)
        sequence_query = query if _looks_like_sequence_query(query) else ""

        sequence_cards: List[Dict[str, object]] = []
        if sequence_query:
            exact_sequence_cards: List[Dict[str, object]] = []
            prefix_sequence_cards: List[Dict[str, object]] = []

            for requirement in requirements_qs.order_by("project_id", "sort_order", "id"):
                card = _card_for_requirement(user, requirement)
                if not card.get("can_access"):
                    continue

                sequence = str(card.get("sequence") or "").strip()
                if not sequence:
                    continue

                if sequence == sequence_query:
                    exact_sequence_cards.append(card)
                elif sequence.startswith(f"{sequence_query}."):
                    prefix_sequence_cards.append(card)

            sequence_cards = exact_sequence_cards + prefix_sequence_cards

        filtered_requirements_qs = requirements_qs
        if query:
            filtered_requirements_qs = filtered_requirements_qs.filter(
                Q(name__icontains=query)
                | Q(object_number__icontains=query)
                | Q(skill__icontains=query)
                | _build_token_filter(["name", "object_number", "skill"], query_tokens)
                | (Q(id=query_id) if query_id is not None else Q())
            )

        requirement_cards: List[Dict[str, object]] = []
        seen_requirement_keys = set()

        for card in sequence_cards:
            dedupe_key = (card.get("object_type"), card.get("object_id"))
            if dedupe_key in seen_requirement_keys:
                continue
            seen_requirement_keys.add(dedupe_key)
            requirement_cards.append(card)
            if len(cards) + len(requirement_cards) >= limit:
                break

        if len(cards) + len(requirement_cards) < limit:
            for requirement in filtered_requirements_qs.order_by("-id")[: limit * 4]:
                card = _card_for_requirement(user, requirement)
                if not card.get("can_access"):
                    continue

                dedupe_key = (card.get("object_type"), card.get("object_id"))
                if dedupe_key in seen_requirement_keys:
                    continue

                seen_requirement_keys.add(dedupe_key)
                requirement_cards.append(card)
                if len(cards) + len(requirement_cards) >= limit:
                    break

        cards.extend(requirement_cards)
        if len(cards) >= limit:
            return cards[:limit]

    if OBJECT_TYPE_DOCUMENT in normalized_types:
        documents_qs = Document.objects.select_related(
            "folder",
            "folder__project",
            "folder__project__user",
        )
        if project_ids:
            documents_filter = Q(folder__project_id__in=project_ids)
            if getattr(room, "company_id", None):
                documents_filter |= Q(folder__company_profile_id=room.company_id)
            documents_qs = documents_qs.filter(documents_filter)
        elif getattr(room, "company_id", None):
            documents_qs = documents_qs.filter(folder__company_profile_id=room.company_id)

        if query:
            documents_qs = documents_qs.annotate(
                linked_display_name=Concat(
                    Coalesce("full_document_number", Value("")),
                    Value("_"),
                    Coalesce("file_name", Value("")),
                    output_field=CharField(),
                )
            ).filter(
                Q(file_name__icontains=query)
                | Q(full_document_number__icontains=query)
                | Q(document_type__icontains=query)
                | Q(identification_number__icontains=query)
                | Q(linked_display_name__icontains=query)
                | _build_token_filter(
                    [
                        "file_name",
                        "full_document_number",
                        "document_type",
                        "identification_number",
                        "linked_display_name",
                    ],
                    query_tokens,
                )
                | (Q(id=query_id) if query_id is not None else Q())
            )

        for document in documents_qs.order_by("-id")[: limit * 2]:
            card = _card_for_document(user, document)
            if card.get("can_access"):
                cards.append(card)
            if len(cards) >= limit:
                return cards

    return cards[:limit]
