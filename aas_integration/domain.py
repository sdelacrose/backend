import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from project_management.models import (
    AvailableProductPart,
    BillOfMaterialPart,
    CompanySolutionSpaceItem,
    PartsListConfig,
    Project,
    ProjectSolutionSpaceItem,
    SolutionSpaceConfig,
)

from .models import AASSubmodelTemplate, FestoHandover, ProductAASProfile
from .profiles import STANDARD_PRODUCT_AAS_FIELDS, is_product_aas_column
from .submodel_templates import render_registered_submodel


HIERARCHICAL_STRUCTURES_SEMANTIC_ID = "https://admin-shell.io/idta/HierarchicalStructures/1/1/Submodel"
HIERARCHICAL_ENTRY_NODE_SEMANTIC_ID = "https://admin-shell.io/idta/HierarchicalStructures/EntryNode/1/0"
HIERARCHICAL_NODE_SEMANTIC_ID = "https://admin-shell.io/idta/HierarchicalStructures/Node/1/0"
HIERARCHICAL_HAS_PART_SEMANTIC_ID = "https://admin-shell.io/idta/HierarchicalStructures/HasPart/1/0"
TECHNICAL_DATA_SEMANTIC_ID = "https://admin-shell.io/idta/TechnicalData/1/2/Submodel"
DIGITAL_NAMEPLATE_SEMANTIC_ID = "https://admin-shell.io/idta/nameplate/3/0/Nameplate"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_json_default).encode("utf-8")


def source_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _id_short(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9_]", "_", value or "").strip("_")
    if not candidate or not candidate[0].isalpha():
        candidate = f"A_{candidate}" if candidate else fallback
    return candidate[:128]


def _reference(value: str, key_type: str = "GlobalReference") -> dict[str, Any]:
    return {"type": "ExternalReference", "keys": [{"type": key_type, "value": value}]}


def _property(
    id_short: str,
    value: Any,
    semantic_id: str = "",
    value_type: str = "xs:string",
) -> dict[str, Any]:
    element = {
        "modelType": "Property",
        "idShort": _id_short(id_short, "Property"),
        "valueType": value_type,
        "value": "" if value is None else str(value),
    }
    if semantic_id:
        element["semanticId"] = _reference(semantic_id)
    return element


def _entity_reference(submodel_id: str, entity_path: list[str]) -> dict[str, Any]:
    return {
        "type": "ModelReference",
        "keys": [
            {"type": "Submodel", "value": submodel_id},
            *({"type": "Entity", "value": id_short} for id_short in entity_path),
        ],
    }


def _column_map(columns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(column.get("key")): column for column in columns if column.get("key")}


def _display_name(default_name: str, fields: dict[str, Any]) -> list[dict[str, str]]:
    values = [("en", default_name)]
    values.extend(
        (language, fields.get(f"name_{language}"))
        for language in ("de", "fr")
        if fields.get(f"name_{language}")
    )
    return [{"language": language, "text": str(text)} for language, text in values]


@dataclass(frozen=True)
class ValidationMessage:
    level: str
    code: str
    message: str
    path: str = ""


@dataclass
class AASDocument:
    shell: dict[str, Any]
    submodels: list[dict[str, Any]]
    concept_descriptions: list[dict[str, Any]] = field(default_factory=list)
    supplementary_files: list[dict[str, Any]] = field(default_factory=list)
    bindings: list[dict[str, Any]] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    validation: list[ValidationMessage] = field(default_factory=list)

    def as_environment(self) -> dict[str, Any]:
        return {
            "assetAdministrationShells": [self.shell],
            "submodels": self.submodels,
            "conceptDescriptions": self.concept_descriptions,
        }

    def as_repository_environment(self) -> dict[str, Any]:
        environment = self.as_environment()
        environment["conceptDescriptions"] = []
        return environment

    def as_revision_environment(self, revision_aas_id: str) -> dict[str, Any]:
        current_aas_id = self.shell["id"]

        def replace_identity(value):
            if isinstance(value, dict):
                return {key: replace_identity(item) for key, item in value.items()}
            if isinstance(value, list):
                return [replace_identity(item) for item in value]
            if isinstance(value, str) and value.startswith(current_aas_id):
                return revision_aas_id + value[len(current_aas_id):]
            return value

        environment = replace_identity(deepcopy(self.as_environment()))
        revision = revision_aas_id.rsplit("/", 1)[-1]
        for submodel in environment["submodels"]:
            submodel.setdefault("submodelElements", []).append(
                _property("PublicationRevision", revision)
            )
        return environment

    def as_revision_repository_environment(self, revision_aas_id: str) -> dict[str, Any]:
        environment = self.as_revision_environment(revision_aas_id)
        environment["conceptDescriptions"] = []
        return environment

    @property
    def hash(self) -> str:
        files = [
            {
                "path": item["path"],
                "content_type": item.get("content_type") or "",
                "sha256": hashlib.sha256(item["content"]).hexdigest(),
            }
            for item in sorted(
                self.supplementary_files,
                key=lambda value: value["path"],
            )
        ]
        return source_hash({"environment": self.as_environment(), "files": files})


class ProductAASMapper:
    def map(self, product: AvailableProductPart, aas_id: str) -> AASDocument:
        config = PartsListConfig.objects.filter(company_profile=product.company_profile, project__isnull=True).first()
        columns = _column_map(config.available_product_columns if config else [])
        technical_id = f"{aas_id}/submodels/technical-data"
        nameplate_id = f"{aas_id}/submodels/nameplate"
        custom_fields = product.custom_fields or {}
        profile = getattr(product, "aas_profile", None)
        origin = profile.origin if profile else ProductAASProfile.Origin.UNCLASSIFIED
        supplier_owned = origin == ProductAASProfile.Origin.PURCHASED
        supplier = (
            (profile.supplier_name if profile else "")
            or custom_fields.get("supplier")
            or ""
        )
        manufacturer = (
            (profile.manufacturer_name if profile else "")
            or custom_fields.get("manufacturer")
            or supplier
        )
        article_number = product.part_number or custom_fields.get("article_number") or ""
        supplier_aas_id = (
            (profile.supplier_aas_id if profile else "")
            or custom_fields.get("supplier_aas_id")
            or custom_fields.get("aas_id")
            or ""
        )
        global_asset_id = (
            (profile.global_asset_id if profile else "")
            or custom_fields.get("global_asset_id")
            or custom_fields.get("asset_id")
            or aas_id
        )
        nameplate_semantics = {
            field.key: field.semantic_id for field in STANDARD_PRODUCT_AAS_FIELDS
        }

        technical_elements = []
        technical_id_shorts = set()
        bindings = [
            {"company_product_id": product.id, "source_field": "name", "submodel_id": nameplate_id, "element_path": "ProductName", "semantic_id": ""},
            {"company_product_id": product.id, "source_field": "part_number", "submodel_id": nameplate_id, "element_path": "ArticleNumber", "semantic_id": ""},
            {"company_product_id": product.id, "source_field": "aas_profile.manufacturer_name", "submodel_id": nameplate_id, "element_path": "ManufacturerName", "semantic_id": nameplate_semantics["ManufacturerName"]},
        ]
        provenance = [
            {"company_product_id": product.id, "field_path": "name", "owner": "supplier" if supplier_owned else "links", "supplier": supplier, "source_aas_id": supplier_aas_id, "last_value": product.name},
            {"company_product_id": product.id, "field_path": "part_number", "owner": "supplier" if supplier_owned else "links", "supplier": supplier, "source_aas_id": supplier_aas_id, "last_value": article_number},
            {"company_product_id": product.id, "field_path": "aas_profile.manufacturer_name", "owner": "supplier" if supplier_owned else "links", "supplier": supplier, "source_aas_id": supplier_aas_id, "last_value": manufacturer},
        ]
        for key in sorted(custom_fields):
            if key.endswith("_manual_override"):
                continue
            if key in {
                "supplier",
                "manufacturer",
                "article_number",
                "global_asset_id",
                "asset_id",
                "aas_id",
                "name_de",
                "name_fr",
            }:
                continue
            column = columns.get(key)
            if not column or not is_product_aas_column(column):
                continue
            semantic_id = column.get("idta_semantic_id", "")
            value_type = {
                "number": "xs:decimal",
                "date": "xs:date",
            }.get(column.get("type"), "xs:string")
            element = _property(key, custom_fields[key], semantic_id, value_type)
            technical_elements.append(element)
            technical_id_shorts.add(element["idShort"].casefold())
            bindings.append({"company_product_id": product.id, "source_field": f"custom_fields.{key}", "submodel_id": technical_id, "element_path": key, "semantic_id": semantic_id})
            provenance.append({
                "company_product_id": product.id,
                "field_path": f"custom_fields.{key}",
                "owner": "supplier" if supplier_owned else ("manual" if custom_fields.get(f"{key}_manual_override") else "links"),
                "supplier": supplier,
                "source_aas_id": supplier_aas_id,
                "last_value": custom_fields[key],
                "manual_override": bool(custom_fields.get(f"{key}_manual_override")),
            })

        supplier_import = (
            (profile.standard_fields or {}).get("supplier_import", {})
            if profile
            else {}
        )
        supplier_fields = (
            supplier_import.get("technical_fields", [])
            if isinstance(supplier_import, dict)
            else []
        )
        for field in sorted(
            (item for item in supplier_fields if isinstance(item, dict)),
            key=lambda item: str(item.get("key") or "").casefold(),
        ):
            key = str(field.get("key") or "").strip()
            value = field.get("value")
            if not key or value in (None, ""):
                continue
            id_short = _id_short(key, "SupplierField")
            if id_short.casefold() in technical_id_shorts:
                continue
            semantic_id = str(field.get("semantic_id") or "").strip()
            value_type = str(field.get("value_type") or "xs:string")
            if value_type not in {"xs:string", "xs:decimal", "xs:integer", "xs:boolean"}:
                value_type = "xs:string"
            element = _property(id_short, value, semantic_id, value_type)
            label = str(field.get("label") or "").strip()
            if label and label != id_short:
                element["displayName"] = [{"language": "en", "text": label}]
            technical_elements.append(element)
            technical_id_shorts.add(id_short.casefold())
            source_field = (
                "aas_profile.standard_fields.supplier_import.technical_fields."
                f"{key}"
            )
            bindings.append({
                "company_product_id": product.id,
                "source_field": source_field,
                "submodel_id": technical_id,
                "element_path": id_short,
                "semantic_id": semantic_id,
            })
            provenance.append({
                "company_product_id": product.id,
                "field_path": source_field,
                "owner": "supplier",
                "supplier": supplier,
                "source_aas_id": supplier_aas_id,
                "last_value": value,
                "manual_override": False,
            })

        shell = {
            "modelType": "AssetAdministrationShell",
            "id": aas_id,
            "idShort": _id_short(product.name, "Product"),
            "displayName": _display_name(product.name, custom_fields),
            "assetInformation": {"assetKind": "Type", "globalAssetId": global_asset_id},
            "submodels": [
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": nameplate_id}]},
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": technical_id}]},
            ],
        }
        nameplate = {
            "modelType": "Submodel",
            "id": nameplate_id,
            "idShort": "Nameplate",
            "kind": "Instance",
            "semanticId": _reference(DIGITAL_NAMEPLATE_SEMANTIC_ID),
            "submodelElements": [
                _property("ProductName", product.name, nameplate_semantics["ProductName"]),
                _property("ArticleNumber", article_number, nameplate_semantics["ArticleNumber"]),
                _property("ManufacturerName", manufacturer, nameplate_semantics["ManufacturerName"]),
            ],
        }
        technical = {
            "modelType": "Submodel",
            "id": technical_id,
            "idShort": "TechnicalData",
            "kind": "Instance",
            "semanticId": _reference(TECHNICAL_DATA_SEMANTIC_ID),
            "submodelElements": technical_elements,
        }
        validation = []
        if origin == ProductAASProfile.Origin.UNCLASSIFIED:
            validation.append(
                ValidationMessage(
                    "error",
                    "unclassified_product_origin",
                    "Classify the product as manufactured or purchased before publishing its AAS.",
                    "ProductOrigin",
                )
            )
        if not article_number:
            validation.append(ValidationMessage("warning", "missing_article_number", "The company product has no article number.", "Nameplate.ArticleNumber"))
        for key, column in columns.items():
            if (
                is_product_aas_column(column)
                and key in custom_fields
                and custom_fields[key] not in (None, "")
                and not column.get("idta_semantic_id")
            ):
                validation.append(ValidationMessage("warning", "missing_semantic_id", f"No semantic ID is configured for {column.get('label', key)}.", f"TechnicalData.{key}"))
        registered_submodels = []
        concept_descriptions = {}
        supplementary_files = {}
        if profile:
            templates = (
                AASSubmodelTemplate.objects.filter(
                    company=product.company_profile,
                    status=AASSubmodelTemplate.Status.ACTIVE,
                    product_assignments__profile=profile,
                )
                .prefetch_related("field_mappings", "supplementary_files")
                .order_by("name", "template_identifier")
            )
            for template in templates:
                if template.profile_keys and profile.profile_key not in template.profile_keys:
                    continue
                rendered, template_bindings, template_provenance, template_validation = (
                    render_registered_submodel(
                        template=template,
                        product=product,
                        profile=profile,
                        aas_id=aas_id,
                    )
                )
                shell["submodels"].append(
                    {
                        "type": "ModelReference",
                        "keys": [{"type": "Submodel", "value": rendered["id"]}],
                    }
                )
                registered_submodels.append(rendered)
                bindings.extend(template_bindings)
                provenance.extend(template_provenance)
                validation.extend(
                    ValidationMessage(
                        item["level"],
                        item["code"],
                        item["code"],
                        item.get("path", ""),
                    )
                    for item in template_validation
                )
                for concept_description in template.concept_descriptions or []:
                    identifier = str(concept_description.get("id") or "")
                    if identifier:
                        concept_descriptions[identifier] = concept_description
                for stored_file in template.supplementary_files.all():
                    with stored_file.file.open("rb") as source:
                        content = source.read()
                    existing = supplementary_files.get(stored_file.path)
                    if existing and existing["sha256"] != stored_file.sha256:
                        validation.append(
                            ValidationMessage(
                                "error",
                                "supplementary_file_path_conflict",
                                "Assigned submodels contain different files at the same AASX path.",
                                f"{template.id_short}.{stored_file.path}",
                            )
                        )
                        continue
                    supplementary_files[stored_file.path] = {
                        "path": stored_file.path,
                        "content_type": stored_file.content_type,
                        "content": content,
                        "sha256": stored_file.sha256,
                    }
        return AASDocument(
            shell=shell,
            submodels=[nameplate, technical, *registered_submodels],
            concept_descriptions=list(concept_descriptions.values()),
            supplementary_files=list(supplementary_files.values()),
            bindings=bindings,
            provenance=provenance,
            validation=validation,
        )


class ProjectBOMAASMapper:
    def map(self, project: Project, aas_id: str) -> AASDocument:
        config = PartsListConfig.objects.filter(project=project).first()
        columns = _column_map(config.bom_columns if config else [])
        rows = list(
            BillOfMaterialPart.objects.filter(project=project)
            .select_related("parent", "available_product")
            .order_by("level", "sort_order", "id")
        )
        submodel_id = f"{aas_id}/submodels/hierarchical-bom"
        metadata_id = f"{aas_id}/submodels/project-metadata"
        nodes: dict[int, dict[str, Any]] = {}
        node_paths: dict[int, list[str]] = {}
        bindings = []
        provenance = []
        validation = []

        for row in rows:
            id_short = f"Part_{row.get_sequence().replace('.', '_')}"
            parent_path = node_paths.get(row.parent_id, ["EntryNode"])
            node_paths[row.id] = [*parent_path, id_short]
            statements = [
                _property("HierarchyNumber", row.get_sequence()),
                _property("PartName", row.name),
                _property("PartNumber", row.part_number or ""),
                _property("Status", row.status or ""),
            ]
            standard_fields = (
                ("hierarchy", "HierarchyNumber", row.get_sequence(), "links"),
                ("name", "PartName", row.name, "links"),
                ("part_number", "PartNumber", row.part_number or "", "supplier" if (row.custom_fields or {}).get("supplier") else "links"),
                ("status", "Status", row.status or "", "links"),
            )
            for source_field, element_name, value, owner in standard_fields:
                bindings.append({"bom_part_id": row.id, "source_field": source_field, "submodel_id": submodel_id, "element_path": f"{'.'.join(node_paths[row.id])}.{element_name}", "semantic_id": ""})
                provenance.append({
                    "bom_part_id": row.id,
                    "field_path": source_field,
                    "owner": owner,
                    "supplier": (row.custom_fields or {}).get("supplier", ""),
                    "source_aas_id": (row.custom_fields or {}).get("supplier_aas_id") or (row.custom_fields or {}).get("aas_id") or "",
                    "last_value": value,
                    "manual_override": False,
                })
            for key in sorted((row.custom_fields or {}).keys()):
                if key.endswith("_manual_override"):
                    continue
                value = row.custom_fields[key]
                column = columns.get(key, {})
                semantic_id = column.get("idta_semantic_id", "")
                statements.append(_property(key, value, semantic_id))
                bindings.append({"bom_part_id": row.id, "source_field": f"custom_fields.{key}", "submodel_id": submodel_id, "element_path": f"{'.'.join(node_paths[row.id])}.{_id_short(key, 'Property')}", "semantic_id": semantic_id})
                provenance.append({
                    "bom_part_id": row.id,
                    "field_path": f"custom_fields.{key}",
                    "owner": "links" if key == "quantity" else ("manual" if row.custom_fields.get(f"{key}_manual_override") else ("supplier" if row.custom_fields.get("supplier") else "links")),
                    "supplier": row.custom_fields.get("supplier", ""),
                    "source_aas_id": row.custom_fields.get("supplier_aas_id") or row.custom_fields.get("aas_id") or "",
                    "last_value": value,
                    "manual_override": bool(row.custom_fields.get(f"{key}_manual_override")),
                })
                if value not in (None, "") and not semantic_id:
                    validation.append(ValidationMessage("warning", "missing_semantic_id", f"No semantic ID is configured for {column.get('label', key)}.", f"BOM.{row.get_sequence()}.{key}"))

            entity = {
                "modelType": "Entity",
                "idShort": id_short,
                "entityType": "CoManagedEntity",
                "semanticId": _reference(HIERARCHICAL_NODE_SEMANTIC_ID),
                "statements": statements,
            }
            if row.available_product_id and hasattr(row.available_product, "aas_identity"):
                entity["entityType"] = "SelfManagedEntity"
                entity["globalAssetId"] = row.available_product.aas_identity.logical_aas_id
            nodes[row.id] = entity

        entry_node = {
            "modelType": "Entity",
            "idShort": "EntryNode",
            "entityType": "SelfManagedEntity",
            "globalAssetId": aas_id,
            "semanticId": _reference(HIERARCHICAL_ENTRY_NODE_SEMANTIC_ID),
            "statements": [],
        }
        for row in rows:
            parent = nodes.get(row.parent_id) if row.parent_id else entry_node
            if parent is None:
                validation.append(
                    ValidationMessage(
                        "error",
                        "invalid_parent",
                        "A BOM item references a parent outside this project.",
                        f"BOM.{row.get_sequence()}",
                    )
                )
                continue
            child = nodes[row.id]
            parent["statements"].append(child)
            parent_path = node_paths.get(row.parent_id, ["EntryNode"])
            parent["statements"].append(
                {
                    "modelType": "RelationshipElement",
                    "idShort": f"HasPart_{child['idShort']}",
                    "semanticId": _reference(HIERARCHICAL_HAS_PART_SEMANTIC_ID),
                    "first": _entity_reference(submodel_id, parent_path),
                    "second": _entity_reference(submodel_id, node_paths[row.id]),
                }
            )

        shell = {
            "modelType": "AssetAdministrationShell",
            "id": aas_id,
            "idShort": _id_short(project.project_name, "ProjectBOM"),
            "displayName": [{"language": "en", "text": project.project_name}],
            "assetInformation": {"assetKind": "Instance", "globalAssetId": aas_id},
            "submodels": [
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": submodel_id}]},
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": metadata_id}]},
            ],
        }
        submodel = {
            "modelType": "Submodel",
            "id": submodel_id,
            "idShort": "HierarchicalStructures",
            "kind": "Instance",
            "semanticId": _reference(HIERARCHICAL_STRUCTURES_SEMANTIC_ID),
            "submodelElements": [entry_node],
        }
        metadata = {
            "modelType": "Submodel",
            "id": metadata_id,
            "idShort": "ProjectMetadata",
            "kind": "Instance",
            "submodelElements": [
                _property("ProjectName", project.project_name),
                _property("ProjectDescription", project.project_description or ""),
                _property("ProjectStatus", project.project_status),
                _property("ProjectStartDate", project.project_start_date),
                _property("ProjectEndDate", project.project_end_date),
                _property("SourceUpdatedAt", project.updated_at.isoformat()),
            ],
        }
        if not rows:
            validation.append(ValidationMessage("error", "empty_bom", "The project BOM is empty.", "BOM"))
        return AASDocument(shell=shell, submodels=[submodel, metadata], bindings=bindings, provenance=provenance, validation=validation)


class SolutionSpaceAASMapper:
    """Map one selected Solution Space assembly and its descendants."""

    def map(
        self,
        assembly: CompanySolutionSpaceItem | ProjectSolutionSpaceItem,
        aas_id: str,
    ) -> AASDocument:
        is_project = isinstance(assembly, ProjectSolutionSpaceItem)
        if is_project:
            rows = list(
                ProjectSolutionSpaceItem.objects.filter(project=assembly.project)
                .select_related(
                    "parent",
                    "source_company_item__aas_product__aas_profile",
                    "source_company_item__aas_product__aas_identity",
                )
                .order_by("level", "sort_order", "id")
            )
            config = SolutionSpaceConfig.objects.filter(project=assembly.project).first()
        else:
            rows = list(
                CompanySolutionSpaceItem.objects.filter(
                    company_profile=assembly.company_profile,
                )
                .select_related(
                    "parent",
                    "aas_product__aas_profile",
                    "aas_product__aas_identity",
                )
                .order_by("level", "sort_order", "id")
            )
            config = SolutionSpaceConfig.objects.filter(
                company_profile=assembly.company_profile,
                project__isnull=True,
            ).first()

        rows_by_parent: dict[int | None, list[Any]] = {}
        for row in rows:
            rows_by_parent.setdefault(row.parent_id, []).append(row)
        subtree = []
        pending = [assembly.id]
        rows_by_id = {row.id: row for row in rows}
        while pending:
            row_id = pending.pop(0)
            row = rows_by_id.get(row_id)
            if row is None:
                continue
            subtree.append(row)
            pending[0:0] = [child.id for child in rows_by_parent.get(row.id, [])]

        columns = _column_map(config.columns if config else [])
        submodel_id = f"{aas_id}/submodels/hierarchical-structures"
        metadata_id = f"{aas_id}/submodels/assembly-metadata"
        nodes: dict[int, dict[str, Any]] = {}
        node_paths: dict[int, list[str]] = {}
        validation: list[ValidationMessage] = []

        for row in subtree:
            hierarchy = row.get_sequence()
            id_short = f"Assembly_{hierarchy.replace('.', '_')}"
            parent_path = node_paths.get(row.parent_id, [])
            node_paths[row.id] = [*parent_path, id_short]
            custom_fields = row.custom_fields or {}
            statements = [
                _property("HierarchyNumber", hierarchy),
                _property("AssemblyName", row.assembly_name),
                _property("AssemblyType", row.assembly_type or ""),
                _property(
                    "RequiredSkills",
                    ", ".join(str(value) for value in (row.required_skills or [])),
                ),
                _property(
                    "ProvidedSkills",
                    ", ".join(str(value) for value in (row.provided_skills or [])),
                ),
                _property("YearOfConstruction", row.year_of_construction or ""),
            ]
            for key in sorted(custom_fields):
                value = custom_fields[key]
                column = columns.get(key, {})
                semantic_id = column.get("idta_semantic_id", "")
                statements.append(_property(key, value, semantic_id))
                if value not in (None, "") and not semantic_id:
                    validation.append(
                        ValidationMessage(
                            "warning",
                            "missing_semantic_id",
                            f"No semantic ID is configured for {column.get('label', key)}.",
                            f"Assembly.{hierarchy}.{key}",
                        )
                    )

            product = (
                row.source_company_item.aas_product
                if is_project
                and row.source_company_item_id
                and row.source_company_item.aas_product_id
                else row.aas_product
                if not is_project and row.aas_product_id
                else None
            )
            product_identity = getattr(product, "aas_identity", None) if product else None
            product_profile = getattr(product, "aas_profile", None) if product else None
            global_asset_id = (
                (product_profile.global_asset_id if product_profile else "")
                or (product_identity.logical_aas_id if product_identity else "")
            )
            entity = {
                "modelType": "Entity",
                "idShort": id_short,
                "entityType": "SelfManagedEntity" if global_asset_id else "CoManagedEntity",
                "semanticId": _reference(
                    HIERARCHICAL_ENTRY_NODE_SEMANTIC_ID
                    if row.id == assembly.id
                    else HIERARCHICAL_NODE_SEMANTIC_ID
                ),
                "statements": statements,
            }
            if global_asset_id:
                entity["globalAssetId"] = global_asset_id
            nodes[row.id] = entity

        for row in subtree:
            if row.id == assembly.id:
                continue
            parent = nodes.get(row.parent_id)
            child = nodes[row.id]
            if parent is None:
                validation.append(
                    ValidationMessage(
                        "error",
                        "invalid_parent",
                        "An assembly references a parent outside the selected hierarchy.",
                        f"Assembly.{row.get_sequence()}",
                    )
                )
                continue
            parent["statements"].append(child)
            parent["statements"].append(
                {
                    "modelType": "RelationshipElement",
                    "idShort": f"HasPart_{child['idShort']}",
                    "semanticId": _reference(HIERARCHICAL_HAS_PART_SEMANTIC_ID),
                    "first": _entity_reference(submodel_id, node_paths[row.parent_id]),
                    "second": _entity_reference(submodel_id, node_paths[row.id]),
                }
            )

        selected_product = None
        if (
            is_project
            and assembly.source_company_item_id
            and assembly.source_company_item.aas_product_id
        ):
            selected_product = assembly.source_company_item.aas_product
        elif not is_project and assembly.aas_product_id:
            selected_product = assembly.aas_product
        product_document = (
            ProductAASMapper().map(selected_product, aas_id)
            if selected_product
            else None
        )

        shell = {
            "modelType": "AssetAdministrationShell",
            "id": aas_id,
            "idShort": _id_short(assembly.assembly_name, "Assembly"),
            "displayName": _display_name(assembly.assembly_name, assembly.custom_fields or {}),
            "assetInformation": {
                "assetKind": "Instance" if is_project else "Type",
                "globalAssetId": aas_id,
            },
            "submodels": [
                {
                    "type": "ModelReference",
                    "keys": [{"type": "Submodel", "value": submodel_id}],
                },
                {
                    "type": "ModelReference",
                    "keys": [{"type": "Submodel", "value": metadata_id}],
                },
            ],
        }
        if product_document:
            shell["submodels"].extend(product_document.shell["submodels"])
        hierarchy = {
            "modelType": "Submodel",
            "id": submodel_id,
            "idShort": "HierarchicalStructures",
            "kind": "Instance",
            "semanticId": _reference(HIERARCHICAL_STRUCTURES_SEMANTIC_ID),
            "submodelElements": [nodes[assembly.id]],
        }
        metadata = {
            "modelType": "Submodel",
            "id": metadata_id,
            "idShort": "AssemblyMetadata",
            "kind": "Instance",
            "submodelElements": [
                _property("AssemblyName", assembly.assembly_name),
                _property("CatalogLevel", "Project" if is_project else "Company"),
                _property("SourceUpdatedAt", assembly.updated_at.isoformat()),
            ],
        }
        return AASDocument(
            shell=shell,
            submodels=[
                hierarchy,
                metadata,
                *(product_document.submodels if product_document else []),
            ],
            concept_descriptions=(
                product_document.concept_descriptions if product_document else []
            ),
            supplementary_files=(
                product_document.supplementary_files if product_document else []
            ),
            bindings=product_document.bindings if product_document else [],
            provenance=product_document.provenance if product_document else [],
            validation=[
                *validation,
                *(product_document.validation if product_document else []),
            ],
        )


class FestoHandoverAASMapper:
    """Build the immutable, Festo-only purchasing package exposed externally."""

    def map(self, handover: FestoHandover, aas_id: str) -> AASDocument:
        submodel_id = f"{aas_id}/submodels/hierarchical-bom"
        metadata_id = f"{aas_id}/submodels/handover-metadata"
        rows = list(handover.item_snapshot)
        rows.sort(key=lambda row: tuple(int(part) for part in str(row["hierarchy"]).split(".")))
        nodes: dict[int, dict[str, Any]] = {}
        node_paths: dict[int, list[str]] = {}
        bindings: list[dict[str, Any]] = []
        provenance: list[dict[str, Any]] = []
        validation: list[ValidationMessage] = []

        for row in rows:
            hierarchy = str(row["hierarchy"])
            id_short = f"Part_{hierarchy.replace('.', '_')}"
            parent_path = node_paths.get(row.get("parent_row_id"), [])
            node_paths[row["row_id"]] = [*parent_path, id_short]
            statements = [
                _property("HierarchyNumber", hierarchy),
                _property("PartName", row.get("name", "")),
            ]
            entity = {
                "modelType": "Entity",
                "idShort": id_short,
                "entityType": "SelfManagedEntity",
                "globalAssetId": row.get("asset_id", ""),
                "semanticId": _reference(
                    HIERARCHICAL_NODE_SEMANTIC_ID
                    if parent_path
                    else HIERARCHICAL_ENTRY_NODE_SEMANTIC_ID
                ),
                "statements": statements,
            }
            article_number = row.get("article_number", "")
            asset_id = row.get("asset_id", "")
            statements.extend(
                [
                    _property("ArticleNumber", article_number),
                    _property("Quantity", row.get("quantity", 1)),
                    _property("Supplier", "Festo"),
                ]
            )
            for source_field, element_name, value, owner in (
                ("hierarchy", "HierarchyNumber", hierarchy, "links"),
                ("name", "PartName", row.get("name", ""), "links"),
                ("part_number", "ArticleNumber", article_number, "supplier"),
                ("custom_fields.quantity", "Quantity", row.get("quantity", 1), "links"),
            ):
                bindings.append(
                    {
                        "bom_part_id": row["row_id"],
                        "source_field": source_field,
                        "submodel_id": submodel_id,
                        "element_path": f"{'.'.join(node_paths[row['row_id']])}.{element_name}",
                        "semantic_id": "",
                    }
                )
                provenance.append(
                    {
                        "bom_part_id": row["row_id"],
                        "field_path": source_field,
                        "owner": owner,
                        "supplier": "Festo",
                        "source_aas_id": asset_id,
                        "last_value": value,
                        "manual_override": False,
                    }
                )
            if not asset_id:
                validation.append(
                    ValidationMessage(
                        "error",
                        "missing_festo_asset_id",
                        "A selected Festo item has no valid asset identifier.",
                        f"BOM.{hierarchy}.AssetId",
                    )
                )
            nodes[row["row_id"]] = entity

        entry_nodes = []
        for row in rows:
            child = nodes[row["row_id"]]
            parent = nodes.get(row.get("parent_row_id"))
            if parent is None:
                entry_nodes.append(child)
                continue
            parent["statements"].append(child)
            parent_path = node_paths[row["parent_row_id"]]
            parent["statements"].append(
                {
                    "modelType": "RelationshipElement",
                    "idShort": f"HasPart_{child['idShort']}",
                    "semanticId": _reference(HIERARCHICAL_HAS_PART_SEMANTIC_ID),
                    "first": _entity_reference(submodel_id, parent_path),
                    "second": _entity_reference(submodel_id, node_paths[row["row_id"]]),
                }
            )

        shell = {
            "modelType": "AssetAdministrationShell",
            "id": aas_id,
            "idShort": _id_short(f"Festo_{handover.project.project_name}", "FestoHandover"),
            "displayName": [
                {"language": "en", "text": f"Festo handover - {handover.project.project_name}"}
            ],
            "assetInformation": {"assetKind": "Instance", "globalAssetId": aas_id},
            "submodels": [
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": submodel_id}]},
                {"type": "ModelReference", "keys": [{"type": "Submodel", "value": metadata_id}]},
            ],
        }
        hierarchy = {
            "modelType": "Submodel",
            "id": submodel_id,
            "idShort": "HierarchicalStructures",
            "kind": "Instance",
            "semanticId": _reference(HIERARCHICAL_STRUCTURES_SEMANTIC_ID),
            "submodelElements": entry_nodes,
        }
        metadata = {
            "modelType": "Submodel",
            "id": metadata_id,
            "idShort": "HandoverMetadata",
            "kind": "Instance",
            "submodelElements": [
                _property("PackagePurpose", "Festo purchasing handover"),
                _property("ProjectName", handover.project.project_name),
                _property("SelectedItemCount", len(handover.item_snapshot)),
            ],
        }
        if not handover.item_snapshot:
            validation.append(
                ValidationMessage("error", "empty_festo_handover", "No Festo items were selected.", "BOM")
            )
        return AASDocument(
            shell=shell,
            submodels=[hierarchy, metadata],
            bindings=bindings,
            provenance=provenance,
            validation=validation,
        )


def validation_report(document: AASDocument) -> dict[str, Any]:
    messages = [message.__dict__ for message in document.validation]
    return {
        "valid": not any(message["level"] == "error" for message in messages),
        "errors": [message for message in messages if message["level"] == "error"],
        "warnings": [message for message in messages if message["level"] == "warning"],
        "source_hash": document.hash,
    }
