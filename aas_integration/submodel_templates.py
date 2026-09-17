import hashlib
import json
from copy import deepcopy
from typing import Any

from django.conf import settings

from .models import AASSubmodelFieldMapping


EDITABLE_MODEL_TYPES = frozenset({"Property", "MultiLanguageProperty", "Range"})
CHILD_COLLECTIONS = ("submodelElements", "value", "statements", "annotations")
REQUIRED_CARDINALITIES = frozenset({"one", "onetomany", "one_to_many", "1", "1..*"})
RESERVED_SUBMODEL_ID_SHORTS = frozenset(
    {"Nameplate", "TechnicalData", "HierarchicalStructures", "ProjectMetadata", "AssemblyMetadata", "HandoverMetadata"}
)


def model_type(value: dict) -> str:
    raw = value.get("modelType", "")
    if isinstance(raw, dict):
        return str(raw.get("name") or "")
    return str(raw or "")


def reference_values(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [
        str(key.get("value"))
        for key in value.get("keys", [])
        if isinstance(key, dict) and key.get("value")
    ]


def _is_required(element: dict) -> bool:
    for qualifier in element.get("qualifiers") or []:
        if not isinstance(qualifier, dict):
            continue
        qualifier_type = str(qualifier.get("type") or "").lower()
        if "cardinality" not in qualifier_type:
            continue
        value = str(qualifier.get("value") or "").replace("-", "_").lower()
        if value in REQUIRED_CARDINALITIES:
            return True
    return False


def flatten_submodel_fields(submodel: dict) -> list[dict]:
    fields: list[dict] = []

    def visit(
        elements: list,
        prefix: list[str],
        *,
        index_elements: bool = False,
    ) -> None:
        id_short_counts: dict[str, int] = {}
        for element in elements:
            if isinstance(element, dict):
                id_short = str(element.get("idShort") or "").strip()
                if id_short:
                    id_short_counts[id_short] = id_short_counts.get(id_short, 0) + 1
        for index, element in enumerate(elements):
            if not isinstance(element, dict):
                continue
            id_short = str(element.get("idShort") or "").strip()
            path = list(prefix)
            if index_elements or not id_short or id_short_counts.get(id_short, 0) > 1:
                path.append(f"${index}")
            if id_short:
                path.append(id_short)
            kind = model_type(element)
            semantic_ids = reference_values(element.get("semanticId"))
            field = {
                "element_path": ".".join(path),
                "element_id_short": id_short or f"Item {index + 1}",
                "model_type": kind,
                "semantic_id": semantic_ids[0] if semantic_ids else "",
                "value_type": str(element.get("valueType") or ""),
                "required": _is_required(element),
                "editable": kind in EDITABLE_MODEL_TYPES,
            }
            if kind in EDITABLE_MODEL_TYPES:
                fields.append(field)
                continue
            for key in CHILD_COLLECTIONS:
                value = element.get(key)
                if isinstance(value, list) and all(
                    isinstance(item, dict) for item in value
                ):
                    visit(
                        value,
                        path,
                        index_elements=(kind == "SubmodelElementList" and key == "value"),
                    )
            if not any(
                isinstance(element.get(key), list) and element.get(key)
                for key in CHILD_COLLECTIONS
            ):
                fields.append(field)

    visit(submodel.get("submodelElements") or [], [])
    return fields


def default_mapping(field: dict, template_id_short: str, sort_order: int) -> dict:
    del template_id_short
    key = field["element_id_short"].lower()
    source = AASSubmodelFieldMapping.Source.PROFILE_FIELD
    source_key = field["element_path"]
    if key in {"productname", "manufacturerproductdesignation"}:
        source = AASSubmodelFieldMapping.Source.PRODUCT_NAME
        source_key = ""
    elif key in {"articlenumber", "manufacturerproductroot"}:
        source = AASSubmodelFieldMapping.Source.ARTICLE_NUMBER
        source_key = ""
    elif key == "manufacturername":
        source = AASSubmodelFieldMapping.Source.MANUFACTURER_NAME
        source_key = ""
    elif not field["editable"]:
        source = AASSubmodelFieldMapping.Source.TEMPLATE_DEFAULT
        source_key = ""
    return {
        **field,
        "source": source,
        "source_key": source_key,
        "sort_order": sort_order,
    }


def extract_submodels_from_json(payload: bytes) -> list[dict]:
    return extract_template_package_from_json(payload)["submodels"]


def extract_template_package_from_json(payload: bytes) -> dict:
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("The JSON file could not be parsed.") from exc
    if isinstance(parsed, dict) and model_type(parsed) == "Submodel":
        return {
            "submodels": [parsed],
            "concept_descriptions": [],
            "supplementary_files": [],
        }
    if isinstance(parsed, dict) and isinstance(parsed.get("submodels"), list):
        return {
            "submodels": [
                item for item in parsed["submodels"] if isinstance(item, dict)
            ],
            "concept_descriptions": [
                item
                for item in parsed.get("conceptDescriptions", [])
                if isinstance(item, dict)
            ],
            "supplementary_files": [],
        }
    raise ValueError("The JSON file must contain a Submodel or an AAS environment with submodels.")


def extract_submodels_from_aasx(payload: bytes) -> list[dict]:
    return extract_template_package_from_aasx(payload)["submodels"]


def extract_template_package_from_aasx(payload: bytes) -> dict:
    from .adapters import read_aasx_package

    try:
        environment, supplementary_files = read_aasx_package(
            payload,
            max_entries=settings.AASX_PREVIEW_MAX_ENTRIES,
            max_decompressed_bytes=settings.AASX_PREVIEW_MAX_DECOMPRESSED_BYTES,
            max_compression_ratio=settings.AASX_PREVIEW_MAX_COMPRESSION_RATIO,
        )
    except Exception as exc:
        raise ValueError("The AASX package does not contain a supported AAS V3 model.") from exc
    return {
        "submodels": [
            item
            for item in environment.get("submodels", [])
            if isinstance(item, dict)
        ],
        "concept_descriptions": [
            item
            for item in environment.get("conceptDescriptions", [])
            if isinstance(item, dict)
        ],
        "supplementary_files": supplementary_files,
    }


def supplementary_file_paths(submodel: dict) -> set[str]:
    paths = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if model_type(value) == "File" and isinstance(value.get("value"), str):
            paths.add(value["value"])
        for item in value.values():
            visit(item)

    visit(submodel.get("submodelElements") or [])
    return paths


def validate_submodel_definition(submodel: dict, mappings: list[dict]) -> dict:
    errors = []
    warnings = []
    if model_type(submodel) != "Submodel":
        errors.append({"code": "invalid_model_type", "path": "modelType"})
    if not submodel.get("id"):
        errors.append({"code": "missing_identifier", "path": "id"})
    if not submodel.get("idShort"):
        errors.append({"code": "missing_id_short", "path": "idShort"})
    elif submodel.get("idShort") in RESERVED_SUBMODEL_ID_SHORTS:
        errors.append({"code": "reserved_submodel", "path": "idShort"})
    fields = {field["element_path"]: field for field in flatten_submodel_fields(submodel)}
    if not fields:
        warnings.append({"code": "no_mappable_fields", "path": "submodelElements"})
    seen_paths = set()
    for mapping in mappings:
        path = str(mapping.get("element_path") or "")
        if path in seen_paths:
            errors.append({"code": "duplicate_mapping", "path": path})
        seen_paths.add(path)
        if path not in fields:
            errors.append({"code": "unknown_element_path", "path": path})
        if (
            mapping.get("source")
            in {
                AASSubmodelFieldMapping.Source.PROFILE_FIELD,
                AASSubmodelFieldMapping.Source.CUSTOM_FIELD,
            }
            and not mapping.get("source_key")
        ):
            errors.append({"code": "missing_source_key", "path": path})
        if mapping.get("editable") and fields.get(path, {}).get("model_type") not in EDITABLE_MODEL_TYPES:
            errors.append({"code": "unsupported_editable_type", "path": path})

    if not errors:
        from .adapters import parse_aas_environment

        validation_id = str(submodel.get("id"))
        validation_submodel = deepcopy(submodel)

        def remove_empty_typed_placeholders(elements: list) -> None:
            for element in elements:
                if not isinstance(element, dict):
                    continue
                if (
                    model_type(element) == "Property"
                    and element.get("value") == ""
                    and element.get("valueType") not in {None, "", "xs:string"}
                ):
                    element.pop("value", None)
                for collection in CHILD_COLLECTIONS:
                    children = element.get(collection)
                    if isinstance(children, list):
                        remove_empty_typed_placeholders(children)

        remove_empty_typed_placeholders(
            validation_submodel.get("submodelElements") or []
        )
        environment = {
            "assetAdministrationShells": [
                {
                    "modelType": "AssetAdministrationShell",
                    "id": f"{validation_id}/validation-shell",
                    "idShort": "TemplateValidation",
                    "assetInformation": {
                        "assetKind": "Type",
                        "globalAssetId": f"{validation_id}/validation-asset",
                    },
                    "submodels": [
                        {
                            "type": "ModelReference",
                            "keys": [{"type": "Submodel", "value": validation_id}],
                        }
                    ],
                }
            ],
            "submodels": [validation_submodel],
            "conceptDescriptions": [],
        }
        try:
            parse_aas_environment(environment)
        except Exception as exc:
            errors.append(
                {
                    "code": "aas_sdk_validation_failed",
                    "path": "definition",
                    "message": str(exc)[:300],
                }
            )
    return {"valid": not errors, "errors": errors, "warnings": warnings}


def _find_element(elements: list, path: list[str]) -> dict | None:
    if not path:
        return None
    index_segment = path[0]
    if index_segment.startswith("$") and index_segment[1:].isdigit():
        index = int(index_segment[1:])
        if index >= len(elements) or not isinstance(elements[index], dict):
            return None
        element = elements[index]
        remaining = path[1:]
        id_short = str(element.get("idShort") or "")
        if remaining and id_short and remaining[0] == id_short:
            remaining = remaining[1:]
        if not remaining:
            return element
        for key in CHILD_COLLECTIONS:
            nested = element.get(key)
            if isinstance(nested, list):
                found = _find_element(nested, remaining)
                if found is not None:
                    return found
        return None
    for element in elements:
        if not isinstance(element, dict) or element.get("idShort") != path[0]:
            continue
        if len(path) == 1:
            return element
        for key in CHILD_COLLECTIONS:
            nested = element.get(key)
            if isinstance(nested, list):
                found = _find_element(nested, path[1:])
                if found is not None:
                    return found
    return None


def template_element_value(definition: dict, element_path: str) -> Any:
    element = _find_element(
        definition.get("submodelElements") or [],
        element_path.split("."),
    )
    if element is None:
        return None
    kind = model_type(element)
    if kind == "MultiLanguageProperty":
        value = element.get("value") or []
        return {
            str(item.get("language")): str(item.get("text"))
            for item in value
            if isinstance(item, dict)
            and item.get("language")
            and item.get("text") not in (None, "")
        }
    if kind == "Range":
        return {"min": element.get("min"), "max": element.get("max")}
    return element.get("value")


def apply_element_value(element: dict, value: Any) -> None:
    kind = model_type(element)
    if kind == "MultiLanguageProperty":
        if isinstance(value, dict):
            multilingual_value = [
                {"language": language, "text": str(text)}
                for language, text in sorted(value.items())
                if text not in (None, "")
            ]
        elif isinstance(value, list):
            multilingual_value = [
                {"language": str(item["language"]), "text": str(item["text"])}
                for item in value
                if isinstance(item, dict)
                and item.get("language")
                and item.get("text") not in (None, "")
            ]
        else:
            multilingual_value = []
        if multilingual_value:
            element["value"] = multilingual_value
        else:
            element.pop("value", None)
        return
    if kind == "Range":
        if isinstance(value, dict):
            for bound in ("min", "max"):
                bound_value = value.get(bound)
                if bound_value in (None, ""):
                    element.pop(bound, None)
                else:
                    element[bound] = str(bound_value)
        else:
            element.pop("min", None)
            element.pop("max", None)
        return
    if value in (None, "", [], {}):
        element.pop("value", None)
    else:
        element["value"] = str(value)


def has_mapped_value(model_type_name: str, value: Any) -> bool:
    if model_type_name == "MultiLanguageProperty":
        if isinstance(value, dict):
            return any(text not in (None, "") for text in value.values())
        if isinstance(value, list):
            return any(
                isinstance(item, dict) and item.get("text") not in (None, "")
                for item in value
            )
        return False
    if model_type_name == "Range":
        return isinstance(value, dict) and any(
            value.get(bound) not in (None, "") for bound in ("min", "max")
        )
    return value not in (None, "", [], {})


def render_registered_submodel(
    *,
    template,
    product,
    profile,
    aas_id: str,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    definition = deepcopy(template.definition)
    original_id = str(definition.get("id") or template.template_identifier)
    identity_hash = hashlib.sha256(template.template_identifier.encode("utf-8")).hexdigest()[:12]
    id_short_slug = "".join(
        character if character.isalnum() else "-" for character in template.id_short
    ).strip("-").lower() or "submodel"
    submodel_id = f"{aas_id}/submodels/{id_short_slug}-{identity_hash}"

    def replace_identifier(value):
        if isinstance(value, dict):
            return {key: replace_identifier(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_identifier(item) for item in value]
        return submodel_id if value == original_id else value

    definition = replace_identifier(definition)
    definition["id"] = submodel_id
    definition["kind"] = "Instance"
    stored = (profile.standard_fields or {}).get("registered_submodels", {}).get(
        template.template_identifier,
        {},
    )
    values = stored.get("values", {}) if isinstance(stored, dict) else {}
    custom_fields = product.custom_fields or {}
    bindings = []
    provenance = []
    validation = []
    supplier_owned = profile.origin == profile.Origin.PURCHASED
    for mapping in template.field_mappings.all():
        element = _find_element(
            definition.get("submodelElements") or [],
            mapping.element_path.split("."),
        )
        if element is None:
            validation.append(
                {
                    "level": "error",
                    "code": "registered_submodel_mapping_missing",
                    "path": f"{template.id_short}.{mapping.element_path}",
                }
            )
            continue
        if mapping.source == AASSubmodelFieldMapping.Source.PRODUCT_NAME:
            value = product.name
            source_field = "name"
        elif mapping.source == AASSubmodelFieldMapping.Source.ARTICLE_NUMBER:
            value = product.part_number or ""
            source_field = "part_number"
        elif mapping.source == AASSubmodelFieldMapping.Source.MANUFACTURER_NAME:
            value = profile.manufacturer_name
            source_field = "aas_profile.manufacturer_name"
        elif mapping.source == AASSubmodelFieldMapping.Source.CUSTOM_FIELD:
            value = custom_fields.get(mapping.source_key)
            source_field = f"custom_fields.{mapping.source_key}"
        elif mapping.source == AASSubmodelFieldMapping.Source.PROFILE_FIELD:
            value = values.get(mapping.source_key or mapping.element_path)
            source_field = (
                "aas_profile.standard_fields.registered_submodels."
                f"{identity_hash}."
                f"{mapping.source_key or mapping.element_path}"
            )
        else:
            value = element.get("value")
            source_field = f"template.{mapping.element_path}"
        if mapping.source != AASSubmodelFieldMapping.Source.TEMPLATE_DEFAULT:
            apply_element_value(element, value)
        if mapping.required and not has_mapped_value(mapping.model_type, value):
            validation.append(
                {
                    "level": "error",
                    "code": "registered_submodel_required_value_missing",
                    "path": f"{template.id_short}.{mapping.element_path}",
                }
            )
        bindings.append(
            {
                "company_product_id": product.id,
                "source_field": source_field[:255],
                "submodel_id": submodel_id,
                "element_path": mapping.element_path,
                "semantic_id": mapping.semantic_id,
            }
        )
        provenance.append(
            {
                "company_product_id": product.id,
                "field_path": source_field,
                "owner": "supplier" if supplier_owned else "links",
                "supplier": profile.supplier_name,
                "source_aas_id": profile.supplier_aas_id,
                "last_value": value,
            }
        )
    return definition, bindings, provenance, validation


def template_checksum(definition: dict) -> str:
    payload = json.dumps(
        definition,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
