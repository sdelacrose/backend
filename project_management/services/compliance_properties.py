"""Resolve recorded scalar properties without inferring engineering values."""

import json
from types import SimpleNamespace

from django.conf import settings

from aas_integration.domain import SolutionSpaceAASMapper
from aas_integration.permissions import has_aas_models_permission
from project_management.permissions import (
    CompanySolutionSpacePermission,
    ProjectSolutionSpacePermission,
)


def aas_properties(workstation, node, actor):
    if not node or not settings.AAS_ENABLED or not has_aas_models_permission(actor):
        return None, None
    assembly = node.source_project_item or node.source_company_item
    if assembly is None:
        return None, None
    project_source = bool(node.source_project_item_id)
    if project_source and assembly.project_id != workstation.project_id:
        return None, None
    permission = (
        ProjectSolutionSpacePermission(action="view", scope="project")
        if project_source
        else CompanySolutionSpacePermission(action="view", scope="company")
    )
    request = SimpleNamespace(
        user=actor,
        method="GET",
        query_params={"project": str(workstation.project_id)},
        data={},
    )
    if not permission.has_object_permission(
        request, SimpleNamespace(action="detail"), assembly
    ):
        return None, None
    identity = getattr(assembly, "aas_identity", None)
    suffix = "project" if project_source else "company"
    identifier = (
        identity.logical_aas_id
        if identity
        else f"https://aas.sharemundolinks.com/ids/pending-{suffix}-assembly"
    )
    document = SolutionSpaceAASMapper().map(assembly, identifier)
    properties, duplicates = {}, set()

    def walk(elements, path, depth=0):
        if depth > 32:
            return
        for element in elements:
            if not isinstance(element, dict):
                continue
            current = [*path, element.get("idShort", "")]
            if element.get("modelType") == "Property":
                key = json.dumps(current, separators=(",", ":"))
                if key in properties:
                    duplicates.add(key)
                record = {
                    "id": key,
                    "label": "/".join(current),
                    "value": element.get("value"),
                    "value_type": element.get("valueType"),
                }
                if "unit" in element:
                    record["unit"] = element["unit"]
                properties[key] = record
            for field in ("submodelElements", "statements", "value"):
                if isinstance(element.get(field), list):
                    walk(element[field], current, depth + 1)

    for submodel in document.as_environment().get("submodels", []):
        walk(submodel.get("submodelElements", []), [submodel["id"]])
    for key in duplicates:
        properties.pop(key, None)
    return properties, document.hash
