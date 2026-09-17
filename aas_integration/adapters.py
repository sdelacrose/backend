import base64
import hashlib
import io
import json
import mimetypes
import stat
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

import requests
from basyx.aas.adapter.aasx import AASXReader, AASXWriter, DictSupplementaryFileContainer
from basyx.aas.adapter.json import read_aas_json_file, write_aas_json_file
from basyx.aas.model import DictIdentifiableStore
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from .domain import AASDocument, canonical_json
from .semantic_profiles import classify_semantic_profile


def _with_semantic_profile(submodel):
    if not settings.AAS_SEMANTIC_PROFILE_UI_ENABLED:
        return submodel
    return classify_semantic_profile(submodel)


def base64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def create_aasx(environment: dict, supplementary_files: list[dict] | None = None) -> bytes:
    object_store = parse_aas_environment(environment)
    output = io.BytesIO()
    aas_ids = [shell["id"] for shell in environment.get("assetAdministrationShells", [])]
    if not aas_ids:
        raise ValueError("The AAS environment does not contain an Asset Administration Shell.")
    file_store = DictSupplementaryFileContainer()
    for item in supplementary_files or []:
        file_store.add_file(
            item["path"],
            io.BytesIO(item["content"]),
            item.get("content_type") or "application/octet-stream",
        )
    with AASXWriter(output, failsafe=False) as writer:
        writer.write_all_aas_objects(
            "/aasx/data.json",
            object_store,
            file_store,
            write_json=True,
        )
    return output.getvalue()


def parse_aas_environment(environment: dict) -> DictIdentifiableStore:
    """Parse the generated model with the official BaSyx SDK before publication."""
    return read_aas_json_file(io.BytesIO(canonical_json(environment)), failsafe=False)


def read_aasx(payload: bytes) -> DictIdentifiableStore:
    object_store = DictIdentifiableStore()
    with AASXReader(io.BytesIO(payload), failsafe=False) as reader:
        reader.read_into(object_store, DictSupplementaryFileContainer())
    return object_store


def read_aasx_environment(
    payload: bytes,
    *,
    max_entries: int,
    max_decompressed_bytes: int,
    max_compression_ratio: int,
) -> dict:
    """Read a current AASX package into its complete AAS JSON environment."""
    environment, _files = read_aasx_package(
        payload,
        max_entries=max_entries,
        max_decompressed_bytes=max_decompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    return environment


def read_aasx_package(
    payload: bytes,
    *,
    max_entries: int,
    max_decompressed_bytes: int,
    max_compression_ratio: int,
) -> tuple[dict, list[dict]]:
    """Read an AASX environment and its supplementary files after archive checks."""
    _preflight_aasx_archive(
        payload,
        max_entries=max_entries,
        max_decompressed_bytes=max_decompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    object_store = DictIdentifiableStore()
    files = DictSupplementaryFileContainer()
    with AASXReader(io.BytesIO(payload), failsafe=False) as reader:
        reader.read_into(object_store, files)
    serialized = io.BytesIO()
    write_aas_json_file(serialized, object_store)
    supplementary_files = []
    for path in files:
        contents = io.BytesIO()
        files.write_file(path, contents)
        supplementary_files.append(
            {
                "path": path,
                "content_type": files.get_content_type(path),
                "content": contents.getvalue(),
            }
        )
    return json.loads(serialized.getvalue()), supplementary_files


def _preflight_aasx_archive(
    payload: bytes,
    *,
    max_entries: int,
    max_decompressed_bytes: int,
    max_compression_ratio: int,
) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("The AASX file is not a valid archive.") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > max_entries:
            raise ValueError("The AASX file contains too many archive entries.")
        total_size = 0
        for entry in entries:
            normalized_name = entry.filename.replace("\\", "/")
            path = PurePosixPath(normalized_name)
            if (
                "\x00" in entry.filename
                or path.is_absolute()
                or ".." in path.parts
                or (path.parts and path.parts[0].endswith(":"))
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise ValueError("The AASX file contains an unsafe archive path.")
            if entry.flag_bits & 0x1:
                raise ValueError("Encrypted AASX files cannot be previewed.")
            total_size += entry.file_size
            if total_size > max_decompressed_bytes:
                raise ValueError("The decompressed AASX file is too large to preview.")
            if entry.file_size and entry.compress_size == 0:
                raise ValueError("The AASX file contains an invalid compressed entry.")
            if entry.compress_size and entry.file_size / entry.compress_size > max_compression_ratio:
                raise ValueError("The AASX file exceeds the safe compression ratio.")


def _model_type(value: dict) -> str:
    model_type = value.get("modelType", "")
    return str(model_type.get("name", "")) if isinstance(model_type, dict) else str(model_type)


def _reference_values(value) -> list[str]:
    if not isinstance(value, dict):
        return []
    return [str(key.get("value")) for key in value.get("keys", []) if key.get("value")]


def _normalize_element(element: dict) -> dict:
    normalized = {
        "model_type": _model_type(element),
        "id_short": str(element.get("idShort") or ""),
        "semantic_ids": _reference_values(element.get("semanticId")),
    }
    for source, target in (
        ("value", "value"),
        ("valueType", "value_type"),
        ("category", "category"),
        ("entityType", "entity_type"),
        ("globalAssetId", "global_asset_id"),
        ("contentType", "content_type"),
    ):
        if source in element and element[source] not in (None, ""):
            normalized[target] = element[source]
    for source, target in (("first", "first"), ("second", "second")):
        values = _reference_values(element.get(source))
        if values:
            normalized[target] = values
    children = []
    for key in ("submodelElements", "statements", "value", "annotations"):
        nested = element.get(key)
        if isinstance(nested, list):
            children.extend(_normalize_element(item) for item in nested if isinstance(item, dict))
    if children:
        normalized["children"] = children
    return normalized


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child(element, name: str):
    return next(
        (child for child in list(element) if _xml_local_name(child.tag) == name),
        None,
    )


def _xml_text(element, name: str) -> str:
    child = _xml_child(element, name)
    return (child.text or "").strip() if child is not None else ""


def _legacy_reference_values(element, name: str) -> list[str]:
    reference = _xml_child(element, name)
    if reference is None:
        return []
    return [
        (child.text or "").strip()
        for child in reference.iter()
        if _xml_local_name(child.tag) == "key" and (child.text or "").strip()
    ]


def _legacy_display_name(element) -> list[dict]:
    description = _xml_child(element, "description")
    if description is None:
        return []
    values = []
    for child in list(description):
        text = (child.text or "").strip()
        if text:
            values.append(
                {
                    "language": child.attrib.get("lang", ""),
                    "text": text,
                }
            )
    return values


def _legacy_element(element) -> dict | None:
    if _xml_local_name(element.tag) == "submodelElement":
        element = next(iter(element), None)
        if element is None:
            return None
    model_type = {
        "property": "Property",
        "submodelElementCollection": "SubmodelElementCollection",
        "submodelElementList": "SubmodelElementList",
        "entity": "Entity",
        "relationshipElement": "RelationshipElement",
        "annotatedRelationshipElement": "AnnotatedRelationshipElement",
        "multiLanguageProperty": "MultiLanguageProperty",
        "range": "Range",
        "file": "File",
        "blob": "Blob",
        "referenceElement": "ReferenceElement",
        "operation": "Operation",
        "capability": "Capability",
    }.get(_xml_local_name(element.tag), _xml_local_name(element.tag))
    normalized = {
        "model_type": model_type,
        "id_short": _xml_text(element, "idShort"),
        "semantic_ids": _legacy_reference_values(element, "semanticId"),
    }
    for source, target in (
        ("category", "category"),
        ("valueType", "value_type"),
        ("entityType", "entity_type"),
        ("mimeType", "content_type"),
        ("contentType", "content_type"),
    ):
        value = _xml_text(element, source)
        if value:
            normalized[target] = value
    for source in ("first", "second"):
        values = _legacy_reference_values(element, source)
        if values:
            normalized[source] = values
    asset_values = _legacy_reference_values(element, "assetRef")
    if asset_values:
        normalized["global_asset_id"] = asset_values[-1]

    value = _xml_child(element, "value")
    child_elements = []
    for container_name in ("submodelElements", "value", "statements", "annotations"):
        container = _xml_child(element, container_name)
        if container is None:
            continue
        for child in list(container):
            parsed = _legacy_element(child)
            if parsed:
                child_elements.append(parsed)
    if child_elements:
        normalized["children"] = child_elements
    elif value is not None and (value.text or "").strip():
        normalized["value"] = (value.text or "").strip()
    return normalized


def _inspect_legacy_aasx(payload: bytes) -> dict | None:
    """Provide a read-only structural preview for legacy AAS XML packages."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        model_names = [
            name
            for name in archive.namelist()
            if name.lower().endswith((".aas.xml", ".aasx.xml"))
        ]
        if not model_names:
            return None

        shells = []
        submodels = []
        for model_name in model_names:
            xml_payload = archive.read(model_name)
            if b"<!DOCTYPE" in xml_payload.upper() or b"<!ENTITY" in xml_payload.upper():
                raise ValueError("Legacy AAS XML with declarations cannot be previewed safely.")
            try:
                root = ElementTree.fromstring(xml_payload)
            except ElementTree.ParseError as exc:
                raise ValueError("The legacy AAS XML could not be parsed.") from exc

            assets = {}
            assets_container = next(
                (item for item in root.iter() if _xml_local_name(item.tag) == "assets"),
                None,
            )
            if assets_container is not None:
                for asset in list(assets_container):
                    asset_id = _xml_text(asset, "identification")
                    if asset_id:
                        assets[asset_id] = asset

            shell_container = next(
                (
                    item
                    for item in root.iter()
                    if _xml_local_name(item.tag) == "assetAdministrationShells"
                ),
                None,
            )
            if shell_container is not None:
                for shell in list(shell_container):
                    asset_refs = _legacy_reference_values(shell, "assetRef")
                    asset = assets.get(asset_refs[-1]) if asset_refs else None
                    shells.append(
                        {
                            "id": _xml_text(shell, "identification"),
                            "id_short": _xml_text(shell, "idShort"),
                            "display_name": _legacy_display_name(shell)
                            or (_legacy_display_name(asset) if asset is not None else []),
                            "asset_information": {
                                "asset_kind": _xml_text(asset, "kind")
                                if asset is not None
                                else None,
                                "global_asset_id": asset_refs[-1] if asset_refs else None,
                                "specific_asset_ids": [],
                            },
                            "submodel_ids": _legacy_reference_values(
                                shell, "submodelRefs"
                            ),
                        }
                    )

            submodel_container = next(
                (item for item in root.iter() if _xml_local_name(item.tag) == "submodels"),
                None,
            )
            if submodel_container is not None:
                for submodel in list(submodel_container):
                    elements = _xml_child(submodel, "submodelElements")
                    submodels.append(
                        _with_semantic_profile({
                            "id": _xml_text(submodel, "identification"),
                            "id_short": _xml_text(submodel, "idShort"),
                            "display_name": _legacy_display_name(submodel),
                            "semantic_ids": _legacy_reference_values(
                                submodel, "semanticId"
                            ),
                            "elements": [
                                parsed
                                for child in list(elements or [])
                                if (parsed := _legacy_element(child)) is not None
                            ],
                        })
                    )

        model_names_set = set(model_names)
        supplementary_files = []
        for entry in archive.infolist():
            name = entry.filename
            if (
                entry.is_dir()
                or name in model_names_set
                or name == "[Content_Types].xml"
                or name.endswith(".rels")
                or name.endswith("aasx-origin")
            ):
                continue
            contents = archive.read(entry)
            supplementary_files.append(
                {
                    "name": name,
                    "content_type": mimetypes.guess_type(name)[0]
                    or "application/octet-stream",
                    "size": len(contents),
                    "sha256": hashlib.sha256(contents).hexdigest(),
                }
            )
    return {
        "checksum": hashlib.sha256(payload).hexdigest(),
        "shells": shells,
        "submodels": submodels,
        "supplementary_files": supplementary_files,
        "warnings": [
            {
                "code": "legacy_aas_version",
                "message": (
                    "This package uses a legacy AAS XML model and is displayed "
                    "as a read-only structural preview."
                ),
            }
        ],
    }


def inspect_aasx(
    payload: bytes,
    *,
    max_entries: int,
    max_decompressed_bytes: int,
    max_compression_ratio: int,
) -> dict:
    _preflight_aasx_archive(
        payload,
        max_entries=max_entries,
        max_decompressed_bytes=max_decompressed_bytes,
        max_compression_ratio=max_compression_ratio,
    )
    files = DictSupplementaryFileContainer()
    try:
        object_store = DictIdentifiableStore()
        with AASXReader(io.BytesIO(payload), failsafe=False) as reader:
            reader.read_into(object_store, files)
        serialized = io.BytesIO()
        write_aas_json_file(serialized, object_store)
        environment = json.loads(serialized.getvalue())
    except Exception as exc:
        legacy = _inspect_legacy_aasx(payload)
        if legacy is not None:
            return legacy
        raise ValueError("The AASX model could not be parsed.") from exc

    shells = []
    for shell in environment.get("assetAdministrationShells", []):
        asset_information = shell.get("assetInformation") or {}
        shells.append(
            {
                "id": str(shell.get("id") or ""),
                "id_short": str(shell.get("idShort") or ""),
                "display_name": shell.get("displayName") or [],
                "asset_information": {
                    "asset_kind": asset_information.get("assetKind"),
                    "global_asset_id": asset_information.get("globalAssetId"),
                    "specific_asset_ids": asset_information.get("specificAssetIds") or [],
                },
                "submodel_ids": [
                    value
                    for reference in shell.get("submodels", [])
                    for value in _reference_values(reference)
                ],
            }
        )
    submodels = [
        _with_semantic_profile({
            "id": str(submodel.get("id") or ""),
            "id_short": str(submodel.get("idShort") or ""),
            "display_name": submodel.get("displayName") or [],
            "semantic_ids": _reference_values(submodel.get("semanticId")),
            "elements": [
                _normalize_element(element)
                for element in submodel.get("submodelElements", [])
                if isinstance(element, dict)
            ],
        })
        for submodel in environment.get("submodels", [])
    ]
    warnings = []
    if not shells:
        warnings.append({"code": "no_shells", "message": "The AASX file contains no AAS shell."})
    if not submodels:
        warnings.append({"code": "no_submodels", "message": "The AASX file contains no submodels."})
    supplementary_files = []
    for name in files:
        contents = io.BytesIO()
        files.write_file(name, contents)
        supplementary_files.append(
            {
                "name": name,
                "content_type": files.get_content_type(name),
                "size": contents.tell(),
                "sha256": files.get_sha256(name).hex(),
            }
        )
    return {
        "checksum": hashlib.sha256(payload).hexdigest(),
        "shells": shells,
        "submodels": submodels,
        "supplementary_files": supplementary_files,
        "warnings": warnings,
    }


class DjangoArtifactStore:
    def put_json(self, key: str, payload: bytes) -> str:
        if not default_storage.exists(key):
            default_storage.save(key, ContentFile(payload))
        return key

    def put_aasx(self, key: str, payload: bytes) -> str:
        if not default_storage.exists(key):
            default_storage.save(key, ContentFile(payload))
        return key

    def download_url(self, key: str, expires_seconds: int = 300) -> str:
        try:
            return default_storage.url(key, expire=expires_seconds)
        except TypeError:
            return default_storage.url(key)


class BasyxAASClient:
    RETRYABLE = {429, 500, 502, 503, 504}

    def __init__(self):
        self.base_url = settings.AAS_GATEWAY_INTERNAL_URL.rstrip("/")
        self.headers = {"X-AAS-Service-Key": settings.AAS_INTERNAL_SERVICE_KEY}
        self.timeout = settings.AAS_REQUEST_TIMEOUT_SECONDS

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = requests.request(method, f"{self.base_url}/{path.lstrip('/')}", headers=self.headers, timeout=self.timeout, **kwargs)
        if response.status_code in self.RETRYABLE:
            raise requests.ConnectionError(f"The AAS repository returned retryable status {response.status_code}.")
        response.raise_for_status()
        return response

    def _upsert(self, collection: str, identifier: str, payload: dict) -> None:
        encoded = base64url(identifier)
        response = requests.put(
            f"{self.base_url}/{collection}/{encoded}",
            headers=self.headers,
            timeout=self.timeout,
            json=payload,
        )
        if response.status_code == 404:
            response = requests.post(
                f"{self.base_url}/{collection}",
                headers=self.headers,
                timeout=self.timeout,
                json=payload,
            )
        if response.status_code in self.RETRYABLE:
            raise requests.ConnectionError(f"The AAS repository returned retryable status {response.status_code}.")
        response.raise_for_status()

    def _publish_environment(self, environment: dict) -> None:
        for submodel in environment.get("submodels", []):
            self._upsert("submodels", submodel["id"], submodel)
        for shell in environment.get("assetAdministrationShells", []):
            self._upsert("shells", shell["id"], shell)

    def publish(self, document: AASDocument, revision_aas_id: str) -> dict:
        current = document.as_repository_environment()
        revision = document.as_revision_repository_environment(revision_aas_id)
        self._publish_environment(revision)
        self._publish_environment(current)
        return revision

    def read_environment(self, aas_id: str) -> dict:
        shell = self._request("GET", f"shells/{base64url(aas_id)}").json()
        submodels = []
        for reference in shell.get("submodels", []):
            keys = reference.get("keys", [])
            if keys:
                submodels.append(self._request("GET", f"submodels/{base64url(keys[0]['value'])}").json())
        return {"assetAdministrationShells": [shell], "submodels": submodels, "conceptDescriptions": []}
