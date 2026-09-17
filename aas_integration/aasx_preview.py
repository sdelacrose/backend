from django.conf import settings
from django.core.cache import cache

from project_management.models import Document

from .adapters import inspect_aasx


def aasx_preview_cache_key(document: Document) -> str:
    updated = document.updated_at.isoformat() if document.updated_at else "unknown"
    return f"aasx-preview:{document.id}:{updated}"


def aasx_checksum_cache_key(checksum: str) -> str:
    return f"aasx-preview:checksum:{checksum}"


def get_cached_aasx_preview(document: Document):
    cached = cache.get(aasx_preview_cache_key(document))
    if cached and cached.get("checksum"):
        return cache.get(aasx_checksum_cache_key(cached["checksum"])) or cached
    return cached


def set_aasx_preview_state(document: Document, value: dict) -> None:
    if value.get("checksum"):
        cache.set(
            aasx_checksum_cache_key(value["checksum"]),
            value,
            timeout=settings.AASX_PREVIEW_CACHE_SECONDS,
        )
    cache.set(
        aasx_preview_cache_key(document),
        value,
        timeout=settings.AASX_PREVIEW_CACHE_SECONDS,
    )


def parse_document_aasx(document: Document) -> dict:
    if not document.file:
        raise ValueError("The document has no file attached.")
    file_name = document.file_name or document.file.name
    if not file_name.lower().endswith(".aasx"):
        raise ValueError("Only AASX documents can be previewed with this endpoint.")
    if document.file.size > settings.AASX_PREVIEW_MAX_COMPRESSED_BYTES:
        raise ValueError("The AASX file is too large to preview.")
    with document.file.open("rb") as source:
        payload = source.read(settings.AASX_PREVIEW_MAX_COMPRESSED_BYTES + 1)
    if len(payload) > settings.AASX_PREVIEW_MAX_COMPRESSED_BYTES:
        raise ValueError("The AASX file is too large to preview.")
    preview = inspect_aasx(
        payload,
        max_entries=settings.AASX_PREVIEW_MAX_ENTRIES,
        max_decompressed_bytes=settings.AASX_PREVIEW_MAX_DECOMPRESSED_BYTES,
        max_compression_ratio=settings.AASX_PREVIEW_MAX_COMPRESSION_RATIO,
    )
    return {
        "status": "ready",
        "document_id": document.id,
        "file_name": file_name.rsplit("/", 1)[-1],
        **preview,
    }


def parse_and_cache_document_aasx(document: Document) -> dict:
    try:
        result = parse_document_aasx(document)
    except ValueError as exc:
        result = {
            "status": "failed",
            "document_id": document.id,
            "file_name": (document.file_name or document.file.name or "").rsplit("/", 1)[-1],
            "detail": str(exc),
        }
    except Exception:
        result = {
            "status": "failed",
            "document_id": document.id,
            "file_name": (document.file_name or getattr(document.file, "name", "") or "").rsplit("/", 1)[-1],
            "detail": "The AASX preview could not be generated.",
        }
    set_aasx_preview_state(document, result)
    return result
