import hashlib
import io
import posixpath
import zipfile
from xml.etree import ElementTree

from django.conf import settings


CONTENT_MARKER = "{{SHAREMUNDO_CONTENT}}"
DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class TemplateValidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _read_template_bytes(template_file):
    template_file.open("rb")
    try:
        content = template_file.read()
    finally:
        template_file.close()
    return content


def validate_template_file(template_file, original_name):
    extension = (original_name or "").lower().rsplit(".", 1)[-1]
    if extension != "docx":
        raise TemplateValidationError(
            "unsupported_template_type",
            "Only macro-free DOCX templates are supported.",
        )

    content = _read_template_bytes(template_file)
    max_bytes = settings.PROJECT_CONTENT_TEMPLATE_MAX_BYTES
    if not content or len(content) > max_bytes:
        raise TemplateValidationError(
            "template_size_invalid",
            f"Template size must be between 1 and {max_bytes} bytes.",
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise TemplateValidationError(
            "invalid_docx_archive",
            "The uploaded file is not a valid DOCX archive.",
        ) from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > settings.PROJECT_CONTENT_TEMPLATE_MAX_ENTRIES:
            raise TemplateValidationError(
                "template_entry_limit",
                "The template contains too many archive entries.",
            )

        total_uncompressed = 0
        document_xml = b""
        for entry in entries:
            normalized = posixpath.normpath(entry.filename.replace("\\", "/"))
            if normalized.startswith("../") or normalized.startswith("/"):
                raise TemplateValidationError(
                    "unsafe_template_path",
                    "The template contains an unsafe archive path.",
                )
            unix_file_type = (entry.external_attr >> 16) & 0o170000
            if unix_file_type == 0o120000:
                raise TemplateValidationError(
                    "unsafe_template_path",
                    "The template contains an unsafe symbolic link.",
                )
            if entry.flag_bits & 0x1:
                raise TemplateValidationError(
                    "encrypted_template",
                    "Encrypted DOCX templates are not supported.",
                )
            total_uncompressed += entry.file_size
            if total_uncompressed > settings.PROJECT_CONTENT_TEMPLATE_MAX_UNCOMPRESSED_BYTES:
                raise TemplateValidationError(
                    "template_uncompressed_limit",
                    "The uncompressed template is too large.",
                )
            lowered = normalized.lower()
            if lowered.endswith("vbaproject.bin") or "vba" in lowered:
                raise TemplateValidationError(
                    "macro_template",
                    "Macro-enabled templates are not supported.",
                )
            if lowered == "word/document.xml":
                document_xml = archive.read(entry)
            if lowered.endswith(".rels"):
                relationships = archive.read(entry).lower()
                if b'targetmode="external"' in relationships:
                    raise TemplateValidationError(
                        "external_template_relationship",
                        "Templates cannot contain external relationships.",
                    )

        try:
            document_text = "".join(ElementTree.fromstring(document_xml).itertext())
        except ElementTree.ParseError as exc:
            raise TemplateValidationError(
                "invalid_document_xml",
                "The DOCX document body is malformed.",
            ) from exc
        if CONTENT_MARKER not in document_text:
            raise TemplateValidationError(
                "missing_content_marker",
                f"Template must contain the marker {CONTENT_MARKER}.",
            )

    return {
        "valid": True,
        "marker": CONTENT_MARKER,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "entry_count": len(entries),
        "uncompressed_bytes": total_uncompressed,
    }
