import re

from models.document_control_configuration import get_document_control_configuration

from project_management.models import Document
from project_management.services.document_numbering import allocate_document_sequence
from project_management.services.project_codes import propose_unique_project_code
from project_management.utils import get_abbreviation

MAX_COLLISION_RETRIES = 25


def _code_for(value, config, key):
    """Configured code ("Drawing:DWG" -> DWG); without a code, 3 letters of the label."""
    if not value:
        return ""
    code = get_abbreviation(value, config, key)
    if code and code != value:
        return str(code)
    return re.sub(r"[^A-Za-z0-9]", "", str(value))[:3].upper()


def build_document_reference(reference_config, values, sequence):
    """Assemble the reference from the configured segments; empty segments are skipped.

    COMPANY has no code yet (company_code is a deferred decision): it is accepted in the
    configuration but contributes nothing.
    """
    parts = []
    for segment in reference_config["segments"]:
        if segment == "SEQUENCE":
            parts.append(str(sequence).zfill(reference_config["sequence_width"]))
        elif values.get(segment):
            parts.append(values[segment])
    return reference_config["separator"].join(parts)


def generate_document_reference(
    folder, *, document_type, discipline, discipline_code, category, config
):
    """(sequence_number, full_document_number) of a NEW document.

    Default: PROJECT-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE (e.g. MRS-STR-DWG-00042); neither the
    version nor the revision is part of it. Called once, at creation; the result is permanent.
    Caller holds a transaction (the counter row is locked while the sequence is allocated).
    """
    settings = get_document_control_configuration(config)["reference"]
    project = folder.project if folder.project_id else None
    project_code = ""
    if project is not None:
        if not project.project_code:
            project.project_code = propose_unique_project_code(project)
            project.save(update_fields=["project_code"])
        project_code = project.project_code

    discipline_part = discipline_code or _code_for(discipline, config, "document_disciplines")
    type_part = _code_for(document_type, config, "document_types")
    values = {
        "PROJECT": project_code,
        "DISCIPLINE": discipline_part,
        "DOCUMENT_TYPE": type_part,
        "CATEGORY": _code_for(category, config, "document_categories"),
    }
    for _ in range(MAX_COLLISION_RETRIES):
        sequence = allocate_document_sequence(
            folder,
            settings["sequence_scope"],
            discipline_code=discipline_part,
            type_code=type_part,
        )
        reference = build_document_reference(settings, values, sequence)
        # Runtime protection on top of the locked counters: never reuse an existing reference.
        if not Document.objects.filter(full_document_number=reference).exists():
            return sequence, reference
    raise RuntimeError("Could not allocate a unique document reference.")
