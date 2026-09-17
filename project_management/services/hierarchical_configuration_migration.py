from copy import deepcopy

from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

from models.hierarchical_configurations import (
    AREA_OF_EXPERTISE_KEY,
    DOCUMENT_DISCIPLINES_KEY,
    flatten_nested_configurations,
    get_default_nested_configuration,
    resolve_nested_configurations,
)
from models.models import CompanyConfig, GlobalConfig
from project_management.models import Document, ECPMPConfig


LEGACY_DISCIPLINE_MAPPING = {
    "electrical": ("engineering", "electrical-engineering"),
    "mechanical": ("engineering", "mechanical-engineering"),
    "structural": ("engineering", "structural-engineering"),
    "architectural": ("engineering", "architecture"),
    "hse": ("quality-hse-compliance", "health-safety-environment"),
    "hydraulic": ("other", "legacy-hydraulic"),
    "plumbing": ("other", "legacy-plumbing"),
    "hvac": ("other", "legacy-hvac"),
    "other": ("other", "legacy-other"),
}

PREVIOUS_DEFAULT_DISCIPLINE_IDS = {
    "project-management-project-management",
    "project-controls",
    "planning-scheduling",
    "cost-control",
    "risk-change-management",
    "pmo-governance",
    "process-engineering",
    "mechanical-engineering",
    "piping-engineering",
    "electrical-engineering",
    "instrumentation-control",
    "civil-structural-engineering",
    "systems-engineering",
    "industrial-engineering",
    "construction-management",
    "site-management",
    "field-engineering",
    "erection-installation",
    "commissioning",
    "handover-close-out",
    "quality-qa-qc",
    "hse",
    "process-safety",
    "regulatory-compliance",
    "procurement",
    "subcontract-management",
    "contract-management",
    "claims-management",
    "supply-chain-management",
    "logistics",
    "materials-management",
    "operations",
    "maintenance-engineering",
    "reliability-asset-management",
    "project-information-management",
    "bim-digital-engineering",
    "it-digital-systems",
    "data-reporting",
    "finance-controlling",
    "commercial-management",
    "business-management",
    "document-control",
    "human-resources",
    "training-competency",
    "change-management",
    "communication",
}

PREVIOUS_DEFAULT_DISCIPLINE_GROUP_IDS = {
    "project-management",
    "engineering",
    "construction-site-management",
    "quality-hse-compliance",
    "procurement-contracts",
    "supply-chain-logistics",
    "operations-maintenance",
    "digital-information-systems",
    "finance-business",
    "support-organization",
    "other",
}

PREVIOUS_DEFAULT_DISCIPLINE_LABELS = {
    "Project Management",
    "Project Controls",
    "Planning & Scheduling",
    "Cost Control",
    "Risk & Change Management",
    "PMO / Governance",
    "Process Engineering",
    "Mechanical Engineering",
    "Piping Engineering",
    "Electrical Engineering",
    "Instrumentation & Control (I&C)",
    "Civil & Structural Engineering",
    "Systems Engineering",
    "Industrial Engineering",
    "Construction Management",
    "Site Management",
    "Field Engineering",
    "Erection / Installation",
    "Commissioning",
    "Handover & Close-out",
    "Quality (QA/QC)",
    "HSE",
    "Process Safety",
    "Regulatory & Compliance",
    "Procurement",
    "Subcontract Management",
    "Contract Management",
    "Claims Management",
    "Supply Chain Management",
    "Logistics",
    "Materials Management",
    "Operations",
    "Maintenance Engineering",
    "Reliability / Asset Management",
    "Project Information Management",
    "BIM / Digital Engineering",
    "IT / Digital Systems",
    "Data & Reporting",
    "Finance / Controlling",
    "Commercial Management",
    "Business Management",
    "Document Control",
    "Human Resources",
    "Training & Competency",
    "Change Management",
    "Communication",
}
PREVIOUS_MIGRATED_DISCIPLINE_IDS = {
    f"legacy-{slugify(label)}" for label in PREVIOUS_DEFAULT_DISCIPLINE_LABELS
} | {
    "legacy-architectural",
    "legacy-hydraulic",
    "legacy-hvac",
    "legacy-other",
    "legacy-plumbing",
}
PREVIOUS_DEFAULT_DISCIPLINE_LABELS = {
    label.casefold() for label in PREVIOUS_DEFAULT_DISCIPLINE_LABELS
}

LEGACY_DEFAULT_CODES = {
    "electrical": "EL",
    "mechanical": "ME",
    "hydraulic": "HY",
    "architectural": "AR",
    "structural": "ST",
    "plumbing": "PL",
    "hvac": "HV",
    "other": "OTH",
}


def split_discipline_value(value):
    normalized = str(value or "").strip()
    if not normalized:
        return "", ""
    if ":" not in normalized:
        return normalized, ""
    label, code = normalized.rsplit(":", 1)
    return label.strip(), code.strip().upper()


def _unique_code(label, requested_code, used_codes):
    base = requested_code or "".join(
        character for character in label.upper() if character.isalnum()
    )[:3]
    base = base or "CUS"
    candidate = base
    suffix = 2
    while candidate in used_codes:
        candidate = f"{base}{suffix}"
        suffix += 1
    used_codes.add(candidate)
    return candidate


def build_document_discipline_hierarchy(
    existing,
    legacy_values,
    document_values=(),
):
    hierarchy = get_default_nested_configuration(DOCUMENT_DISCIPLINES_KEY)
    groups_by_id = {group["id"]: group for group in hierarchy["groups"]}
    other_group = next(
        group for group in hierarchy["groups"] if group["id"] == "other"
    )
    children_by_label = {
        child["label"].casefold(): child
        for group in hierarchy["groups"]
        for child in group["children"]
    }
    used_ids = {
        child["id"]
        for group in hierarchy["groups"]
        for child in group["children"]
    }
    used_codes = {
        child["code"]
        for group in hierarchy["groups"]
        for child in group["children"]
    }

    current_default_ids = set(used_ids)
    current_default_group_ids = set(groups_by_id)
    used_group_ids = set(current_default_group_ids)
    used_group_labels = {group["label"].casefold() for group in hierarchy["groups"]}
    for group in (existing or {}).get("groups", []):
        group_id = group.get("id")
        group_label = str(group.get("label") or "").strip()
        if group_id in PREVIOUS_DEFAULT_DISCIPLINE_GROUP_IDS:
            target_group = groups_by_id.get(group_id, other_group)
        elif (
            group_id
            and group_label
            and group_id not in used_group_ids
            and group_label.casefold() not in used_group_labels
        ):
            target_group = deepcopy(group)
            target_group.pop("is_default", None)
            target_group.pop("translations", None)
            target_group["children"] = []
            hierarchy["groups"].insert(-1, target_group)
            used_group_ids.add(group_id)
            used_group_labels.add(group_label.casefold())
        else:
            target_group = other_group

        for child in group.get("children", []):
            child_id = child.get("id")
            if (
                not child_id
                or child_id in current_default_ids
                or child_id in PREVIOUS_DEFAULT_DISCIPLINE_IDS
                or child_id in PREVIOUS_MIGRATED_DISCIPLINE_IDS
            ):
                continue
            label = str(child.get("label") or "").strip()
            code = str(child.get("code") or "").strip().upper()
            if not label or not code or label.casefold() in children_by_label:
                continue
            preserved = deepcopy(child)
            preserved.pop("is_default", None)
            preserved.pop("translations", None)
            preserved["code"] = _unique_code(label, code, used_codes)
            target_group["children"].append(preserved)
            used_ids.add(child_id)
            children_by_label[label.casefold()] = preserved

    def append_legacy_value(raw_value, *, include_retired):
        label, requested_code = split_discipline_value(raw_value)
        if not label or label.casefold() in children_by_label:
            return
        mapping = LEGACY_DISCIPLINE_MAPPING.get(label.casefold())
        if mapping and mapping[0] != "other":
            return
        if (
            not include_retired
            and label.casefold() in PREVIOUS_DEFAULT_DISCIPLINE_LABELS
        ):
            return

        base_id = mapping[1] if mapping else f"legacy-{slugify(label) or 'custom'}"
        child_id = base_id
        suffix = 2
        while child_id in used_ids:
            child_id = f"{base_id}-{suffix}"
            suffix += 1
        used_ids.add(child_id)

        code = _unique_code(
            label,
            requested_code or LEGACY_DEFAULT_CODES.get(label.casefold(), ""),
            used_codes,
        )
        child = {"id": child_id, "label": label, "code": code}
        other_group["children"].append(child)
        children_by_label[label.casefold()] = child

    for raw_value in legacy_values:
        append_legacy_value(raw_value, include_retired=False)
    for raw_value in document_values:
        append_legacy_value(raw_value, include_retired=True)

    return hierarchy


def build_discipline_lookup(hierarchy):
    groups = {group["id"]: group for group in hierarchy["groups"]}
    lookup = {}
    for group in hierarchy["groups"]:
        for child in group["children"]:
            lookup[child["label"].casefold()] = (group, child)

    for legacy_label, (group_id, child_id) in LEGACY_DISCIPLINE_MAPPING.items():
        group = groups[group_id]
        child = next(
            (item for item in group["children"] if item["id"] == child_id),
            None,
        )
        if child is None and group_id == "other":
            child = lookup.get(legacy_label, (None, None))[1]
        if child is not None:
            lookup[legacy_label] = (group, child)
    return lookup


class HierarchicalConfigurationMigrator:
    def __init__(self, *, apply=False, batch_size=500):
        self.apply = apply
        self.batch_size = batch_size
        self.summary = {
            "global_configs": 0,
            "company_configs": 0,
            "project_configs": 0,
            "documents": 0,
            "unmatched_documents": 0,
        }

    def inventory(self):
        configured = set()
        for model in (GlobalConfig, CompanyConfig, ECPMPConfig):
            for values in model.objects.values_list("document_disciplines", flat=True):
                configured.update(values or [])
        documents = set(
            Document.objects.exclude(discipline__isnull=True)
            .exclude(discipline="")
            .values_list("discipline", flat=True)
            .distinct()
        )
        return {
            "configured_values": sorted(configured),
            "document_values": sorted(documents),
        }

    def run(self):
        self._migrate_configurations(GlobalConfig, "global_configs")
        self._migrate_configurations(CompanyConfig, "company_configs")
        self._migrate_configurations(ECPMPConfig, "project_configs")
        self._migrate_documents()
        return self.summary

    def _migrate_configurations(self, model, summary_key):
        for configuration in model.objects.iterator(chunk_size=self.batch_size):
            legacy_values = list(configuration.document_disciplines or [])
            document_values = self._document_values_for_configuration(configuration)
            current_nested = deepcopy(configuration.nested_configurations or {})
            current_discipline = current_nested.get(DOCUMENT_DISCIPLINES_KEY)
            discipline_hierarchy = build_document_discipline_hierarchy(
                current_discipline,
                legacy_values,
                document_values,
            )
            # Canonical defaults may be renamed by a newer specification. Replace
            # them before validation so the migration can upgrade stored defaults.
            current_nested[AREA_OF_EXPERTISE_KEY] = get_default_nested_configuration(
                AREA_OF_EXPERTISE_KEY
            )
            current_nested[DOCUMENT_DISCIPLINES_KEY] = discipline_hierarchy
            nested = resolve_nested_configurations(current_nested)
            flattened = flatten_nested_configurations(nested)

            self.summary[summary_key] += 1
            if not self.apply:
                continue
            configuration.nested_configurations = nested
            configuration.document_disciplines = flattened[DOCUMENT_DISCIPLINES_KEY]
            configuration.save(
                update_fields=[
                    "nested_configurations",
                    "document_disciplines",
                ]
            )

    def _document_values_for_configuration(self, configuration):
        queryset = Document.objects.all()
        if isinstance(configuration, ECPMPConfig):
            queryset = queryset.filter(folder__project_id=configuration.project_id)
        elif configuration.user_id:
            filters = Q(folder__freelancer_id=configuration.user_id) | Q(
                folder__project__user_id=configuration.user_id
            )
            if isinstance(configuration, CompanyConfig):
                filters &= Q(folder__project__isnull=True) | Q(
                    folder__project__solution=configuration.solution
                )
            queryset = queryset.filter(filters)
        elif configuration.company_profile_id:
            filters = Q(
                folder__company_profile_id=configuration.company_profile_id
            ) | Q(
                folder__project__company_profile_id=configuration.company_profile_id
            ) | Q(
                folder__project__user__company_id=configuration.company_profile_id
            )
            if isinstance(configuration, CompanyConfig):
                filters &= Q(folder__project__isnull=True) | Q(
                    folder__project__solution=configuration.solution
                )
            queryset = queryset.filter(filters)
        else:
            return []
        return list(
            queryset.exclude(discipline__isnull=True)
            .exclude(discipline="")
            .values_list("discipline", flat=True)
            .distinct()
        )

    def _migrate_documents(self):
        default_hierarchy = get_default_nested_configuration(
            DOCUMENT_DISCIPLINES_KEY
        )
        all_values = list(
            Document.objects.exclude(discipline__isnull=True)
            .exclude(discipline="")
            .values_list("discipline", flat=True)
            .distinct()
        )
        hierarchy = build_document_discipline_hierarchy(
            default_hierarchy,
            [],
            all_values,
        )
        lookup = build_discipline_lookup(hierarchy)
        configured_codes = self._configured_codes()
        default_child_ids = {
            child["id"]
            for group in default_hierarchy["groups"]
            for child in group["children"]
        }

        pending = []
        queryset = Document.objects.exclude(discipline__isnull=True).exclude(discipline="")
        for document in queryset.iterator(chunk_size=self.batch_size):
            original_label, _ = split_discipline_value(document.discipline)
            match = lookup.get(original_label.casefold())
            if match is None:
                self.summary["unmatched_documents"] += 1
                continue
            group, child = match
            snapshot_code = child["code"]
            if child["id"] not in default_child_ids:
                snapshot_code = (
                    document.discipline_code
                    or configured_codes.get(original_label.casefold())
                    or LEGACY_DEFAULT_CODES.get(original_label.casefold())
                    or child["code"]
                )
            document.discipline = child["label"]
            document.discipline_group = group["label"]
            document.discipline_group_id = group["id"]
            document.discipline_option_id = child["id"]
            document.discipline_code = snapshot_code
            pending.append(document)
            self.summary["documents"] += 1

            if self.apply and len(pending) >= self.batch_size:
                self._bulk_update_documents(pending)
                pending = []

        if self.apply and pending:
            self._bulk_update_documents(pending)

    def _configured_codes(self):
        codes = dict(LEGACY_DEFAULT_CODES)
        for model in (GlobalConfig, CompanyConfig, ECPMPConfig):
            for values in model.objects.values_list("document_disciplines", flat=True):
                for value in values or []:
                    label, code = split_discipline_value(value)
                    if label and code:
                        codes.setdefault(label.casefold(), code)
        return codes

    @transaction.atomic
    def _bulk_update_documents(self, documents):
        Document.objects.bulk_update(
            documents,
            [
                "discipline",
                "discipline_group",
                "discipline_group_id",
                "discipline_option_id",
                "discipline_code",
            ],
            batch_size=self.batch_size,
        )
