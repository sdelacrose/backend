from dataclasses import dataclass

from django.conf import settings


@dataclass(frozen=True)
class SemanticProfileDefinition:
    key: str
    version: str
    semantic_ids: tuple[str, ...]
    family_prefixes: tuple[str, ...] = ()
    required_element_semantic_ids: tuple[str, ...] = ()


def configured_semantic_profiles():
    definitions = []
    for value in getattr(settings, "AAS_SEMANTIC_PROFILE_DEFINITIONS", []):
        if not isinstance(value, dict):
            continue
        try:
            definitions.append(
                SemanticProfileDefinition(
                    key=str(value["key"]),
                    version=str(value["version"]),
                    semantic_ids=tuple(str(item) for item in value["semantic_ids"]),
                    family_prefixes=tuple(
                        str(item) for item in value.get("family_prefixes", [])
                    ),
                    required_element_semantic_ids=tuple(
                        str(item)
                        for item in value.get(
                            "required_element_semantic_ids",
                            [],
                        )
                    ),
                )
            )
        except (KeyError, TypeError):
            continue
    return tuple(definitions)


def _element_semantic_ids(elements):
    values = set()
    for element in elements or []:
        if not isinstance(element, dict):
            continue
        values.update(str(item) for item in element.get("semantic_ids", []))
        values.update(_element_semantic_ids(element.get("children", [])))
    return values


def classify_semantic_profile(submodel, definitions=None):
    definitions = (
        configured_semantic_profiles() if definitions is None else definitions
    )
    result = dict(submodel)
    semantic_ids = {
        str(value) for value in submodel.get("semantic_ids", []) if value
    }
    if not semantic_ids:
        result["semantic_profile"] = {
            "status": "missing_semantic_id",
            "key": None,
            "version": None,
            "supported_versions": [],
            "missing_required_semantic_ids": [],
        }
        return result

    matched = next(
        (
            definition
            for definition in definitions
            if semantic_ids.intersection(definition.semantic_ids)
        ),
        None,
    )
    if matched:
        available_element_ids = _element_semantic_ids(submodel.get("elements", []))
        missing = sorted(
            set(matched.required_element_semantic_ids) - available_element_ids
        )
        result["semantic_profile"] = {
            "status": "invalid_required_semantics" if missing else "recognized",
            "key": matched.key,
            "version": matched.version,
            "supported_versions": [matched.version],
            "missing_required_semantic_ids": missing,
        }
        return result

    family_matches = [
        definition
        for definition in definitions
        if any(
            semantic_id.startswith(prefix)
            for semantic_id in semantic_ids
            for prefix in definition.family_prefixes
        )
    ]
    if family_matches:
        result["semantic_profile"] = {
            "status": "version_mismatch",
            "key": family_matches[0].key,
            "version": None,
            "supported_versions": sorted(
                {definition.version for definition in family_matches}
            ),
            "missing_required_semantic_ids": [],
        }
        return result

    result["semantic_profile"] = {
        "status": "generic",
        "key": None,
        "version": None,
        "supported_versions": [],
        "missing_required_semantic_ids": [],
    }
    return result
