from rest_framework import serializers

from models.document_control_configuration import get_document_control_configuration

from project_management.models import Document
from project_management.services.document_numbering import allocate_document_sequence
from project_management.services.project_codes import propose_unique_project_code
from project_management.utils import get_abbreviation

MAX_COLLISION_RETRIES = 25


def _resolve_code(value, config, key, *, active, field, label):
    """Configured code ("Drawing:DWG" -> DWG). No label-derived fallback: when `value` is set
    and its segment is active in the reference configuration, a code that cannot be resolved
    is a clean validation error -- never a silently generated abbreviation. When the segment
    is not part of the active reference (e.g. a company config with no CATEGORY segment), no
    code is needed for it and "" is returned, matching the previous lenient behaviour there.
    """
    if not value:
        return ""
    code = get_abbreviation(value, config, key)
    if code and code != value:
        return str(code)
    if not active:
        return ""
    raise serializers.ValidationError(
        {
            field: (
                f'No reference code is configured for {label} "{value}". Configure a '
                "code for it before creating this document."
            )
        }
    )


def _document_type_lookup_config(config, *, is_project_scope):
    """A config-like shim whose `document_types` is merged with the same canonical defaults
    the dropdown already offers (include_defaults=true) -- so a type the user was shown as
    selectable is also resolvable here, even when the stored config list is empty/partial.
    LOT 2D-12: without this, a value valid in the UI could still fail code resolution.
    """
    from types import SimpleNamespace

    from companyprofileapis.services.document_type_defaults import (
        merge_missing_document_type_defaults,
    )
    from models import constants

    canonical = (
        constants.GC_PROJECT_DOCUMENT_TYPES if is_project_scope else constants.GC_COMPANY_DOCUMENT_TYPES
    )
    stored = getattr(config, "document_types", None) if config else None
    return SimpleNamespace(document_types=merge_missing_document_type_defaults(stored, canonical))


def _resolve_company_code(company_profile):
    """The company's permanent short code: proposed and persisted once, like project_code."""
    from models.company_codes import propose_unique_company_code

    if not company_profile.company_code:
        company_profile.company_code = propose_unique_company_code(company_profile)
        company_profile.save(update_fields=["company_code"])
    return company_profile.company_code


def build_document_reference(reference_config, values, sequence):
    """Assemble the reference from the configured segments; empty segments are skipped."""
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

    Default: PROJECT-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE for a project document (e.g.
    MRS-STR-DRW-00042). For a company-library document without a project, the same default
    resolves as COMPANY-DISCIPLINE-DOCUMENT_TYPE-SEQUENCE (e.g. SHA-STR-DRW-00047): there is no
    project to fill the leading segment with, so it is read as the company's own permanent code
    instead -- the (shared, project-oriented) configuration keeps saying "PROJECT" and does not
    need to be rewritten for this. A freelancer's own library (no CompanyProfile) is unaffected
    and keeps its previous behaviour (the segment is simply empty and skipped).

    Neither the version nor the revision is ever part of it. Called once, at creation; the
    result is permanent. Caller holds a transaction (the counter row is locked while the
    sequence is allocated).
    """
    settings = get_document_control_configuration(config)["reference"]
    segments = list(settings["segments"])
    project = folder.project if folder.project_id else None

    project_code = ""
    company_code = ""
    if project is not None:
        if not project.project_code:
            project.project_code = propose_unique_project_code(project)
            project.save(update_fields=["project_code"])
        project_code = project.project_code
    elif folder.company_profile_id:
        if "COMPANY" not in segments:
            segments = ["COMPANY" if segment == "PROJECT" else segment for segment in segments]
        if "COMPANY" in segments:
            company_code = _resolve_company_code(folder.company_profile)
            if not company_code:
                # Unreachable in practice (propose_unique_company_code always returns a
                # value), kept as an explicit, clean failure rather than a degraded reference.
                raise serializers.ValidationError(
                    {
                        "company_code": (
                            "This company does not have a Company Code yet; the permanent "
                            "reference cannot be generated."
                        )
                    }
                )

    discipline_part = discipline_code or _resolve_code(
        discipline,
        config,
        "document_disciplines",
        active="DISCIPLINE" in segments,
        field="discipline",
        label="this discipline",
    )
    type_part = _resolve_code(
        document_type,
        _document_type_lookup_config(config, is_project_scope=project is not None),
        "document_types",
        active="DOCUMENT_TYPE" in segments,
        field="document_type",
        label="this document type",
    )
    category_part = _resolve_code(
        category,
        config,
        "document_categories",
        active="CATEGORY" in segments,
        field="category",
        label="this category",
    )

    values = {
        "PROJECT": project_code,
        "COMPANY": company_code,
        "DISCIPLINE": discipline_part,
        "DOCUMENT_TYPE": type_part,
        "CATEGORY": category_part,
    }
    resolved_settings = dict(settings)
    resolved_settings["segments"] = segments

    for _ in range(MAX_COLLISION_RETRIES):
        sequence = allocate_document_sequence(
            folder,
            settings["sequence_scope"],
            discipline_code=discipline_part,
            type_code=type_part,
        )
        reference = build_document_reference(resolved_settings, values, sequence)
        # Runtime protection on top of the locked counters: never reuse an existing reference.
        if not Document.objects.filter(full_document_number=reference).exists():
            return sequence, reference
    raise RuntimeError("Could not allocate a unique document reference.")
