import hashlib
from pathlib import PurePosixPath
from copy import deepcopy

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import (
    AASResourceIdentity,
    AASSubmodelFieldMapping,
    AASSubmodelTemplate,
    AASSubmodelTemplateSupplementaryFile,
    ProductAASSubmodelAssignment,
)
from .submodel_templates import (
    default_mapping,
    extract_template_package_from_aasx,
    extract_template_package_from_json,
    flatten_submodel_fields,
    reference_values,
    template_checksum,
    supplementary_file_paths,
    validate_submodel_definition,
)


def _template_name(submodel: dict) -> str:
    for item in submodel.get("displayName") or []:
        if isinstance(item, dict) and item.get("text"):
            return str(item["text"])
    return str(submodel.get("idShort") or submodel.get("id") or "Submodel")


def _template_version(submodel: dict, requested_version: str | None) -> str:
    if requested_version:
        return requested_version
    administration = submodel.get("administration") or {}
    version = str(administration.get("version") or "").strip()
    revision = str(administration.get("revision") or "").strip()
    if version and revision:
        return f"{version}.{revision}"
    return version or revision or "1.0"


@transaction.atomic
def import_submodel_templates(
    *,
    company,
    user,
    file_name: str,
    payload: bytes,
    version: str | None = None,
    profile_keys: list[str] | None = None,
) -> list[AASSubmodelTemplate]:
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if extension == "json":
        package = extract_template_package_from_json(payload)
        source_format = AASSubmodelTemplate.SourceFormat.JSON
    elif extension == "aasx":
        package = extract_template_package_from_aasx(payload)
        source_format = AASSubmodelTemplate.SourceFormat.AASX
    else:
        raise ValidationError({"file": "Upload an AAS JSON or AASX file."})
    submodels = package["submodels"]
    if not submodels:
        raise ValidationError({"file": "The file does not contain a submodel."})
    supplementary_files = {
        item["path"]: item for item in package["supplementary_files"]
    }

    templates = []
    for source_submodel in submodels:
        submodel = deepcopy(source_submodel)
        template_identifier = str(submodel.get("id") or "").strip()
        id_short = str(submodel.get("idShort") or "").strip()
        if not template_identifier or not id_short:
            raise ValidationError(
                {"file": "Every imported submodel requires an identifier and idShort."}
            )
        if len(template_identifier) > 1000 or len(id_short) > 128:
            raise ValidationError(
                {"file": "A submodel identifier or idShort exceeds the supported length."}
            )
        semantic_ids = reference_values(submodel.get("semanticId"))
        template_version = _template_version(submodel, version)
        if AASSubmodelTemplate.objects.filter(
            company=company,
            template_identifier=template_identifier,
            version=template_version,
        ).exists():
            raise ValidationError(
                {
                    "file": (
                        f"Version {template_version} of {template_identifier} "
                        "is already registered."
                    )
                }
            )
        mappings = [
            default_mapping(field, id_short, index)
            for index, field in enumerate(flatten_submodel_fields(submodel))
        ]
        report = validate_submodel_definition(submodel, mappings)
        template = AASSubmodelTemplate.objects.create(
            company=company,
            template_identifier=template_identifier,
            semantic_id=semantic_ids[0] if semantic_ids else "",
            id_short=id_short,
            name=_template_name(submodel),
            version=template_version,
            status=(
                AASSubmodelTemplate.Status.DRAFT
                if report["valid"]
                else AASSubmodelTemplate.Status.INVALID
            ),
            source_format=source_format,
            definition=submodel,
            concept_descriptions=package["concept_descriptions"],
            profile_keys=profile_keys or ["idta-product"],
            validation_report=report,
            checksum=template_checksum(submodel),
            created_by=user,
        )
        AASSubmodelFieldMapping.objects.bulk_create(
            [
                AASSubmodelFieldMapping(template=template, **mapping)
                for mapping in mappings
            ]
        )
        for path in supplementary_file_paths(submodel):
            item = supplementary_files.get(path)
            if item is None:
                continue
            stored_file = AASSubmodelTemplateSupplementaryFile(
                template=template,
                path=path,
                content_type=item.get("content_type")
                or "application/octet-stream",
                size=len(item["content"]),
                sha256=hashlib.sha256(item["content"]).hexdigest(),
            )
            stored_file.file.save(
                PurePosixPath(path).name or "supplementary-file",
                ContentFile(item["content"]),
                save=True,
            )
        templates.append(template)
    return templates


def validate_template(template: AASSubmodelTemplate) -> dict:
    mappings = [
        {
            "element_path": mapping.element_path,
            "source": mapping.source,
            "source_key": mapping.source_key,
            "editable": mapping.editable,
        }
        for mapping in template.field_mappings.all()
    ]
    report = validate_submodel_definition(template.definition, mappings)
    template.validation_report = report
    if template.status in {
        AASSubmodelTemplate.Status.DRAFT,
        AASSubmodelTemplate.Status.INVALID,
    }:
        template.status = (
            AASSubmodelTemplate.Status.DRAFT
            if report["valid"]
            else AASSubmodelTemplate.Status.INVALID
        )
        template.save(
            update_fields=["validation_report", "status", "updated_at"]
        )
    else:
        template.save(update_fields=["validation_report", "updated_at"])
    return report


def _mark_assigned_products_out_of_date(template: AASSubmodelTemplate) -> int:
    assigned_profile_ids = ProductAASSubmodelAssignment.objects.filter(
        template=template,
    ).values_list("profile_id", flat=True)
    identities = AASResourceIdentity.objects.filter(
        company=template.company,
        source_type=AASResourceIdentity.SourceType.COMPANY_PRODUCT,
        company_product__aas_profile__in=assigned_profile_ids,
    )
    return identities.update(out_of_date=True, updated_at=timezone.now())


@transaction.atomic
def activate_template(template: AASSubmodelTemplate, *, user) -> dict:
    template = (
        AASSubmodelTemplate.objects.select_for_update()
        .prefetch_related("field_mappings")
        .get(id=template.id)
    )
    report = validate_template(template)
    if not report["valid"]:
        return {
            "activated": False,
            "template": template,
            "validation_report": report,
            "deprecated_versions": 0,
            "out_of_date_resources": 0,
        }
    previous = list(
        AASSubmodelTemplate.objects.select_for_update().filter(
            company=template.company,
            template_identifier=template.template_identifier,
            status=AASSubmodelTemplate.Status.ACTIVE,
        ).exclude(id=template.id)
    )
    previous_ids = [item.id for item in previous]
    migrated_assignments = ProductAASSubmodelAssignment.objects.filter(
        template_id__in=previous_ids,
        template_identifier=template.template_identifier,
    ).update(template=template, updated_at=timezone.now())
    for item in previous:
        item.status = AASSubmodelTemplate.Status.DEPRECATED
        item.save(update_fields=["status", "updated_at"])
    template.status = AASSubmodelTemplate.Status.ACTIVE
    template.activated_by = user
    template.activated_at = timezone.now()
    template.save(
        update_fields=["status", "activated_by", "activated_at", "updated_at"]
    )
    return {
        "activated": True,
        "template": template,
        "validation_report": report,
        "deprecated_versions": len(previous),
        "migrated_assignments": migrated_assignments,
        "out_of_date_resources": _mark_assigned_products_out_of_date(template),
    }


@transaction.atomic
def deprecate_template(template: AASSubmodelTemplate) -> int:
    template = AASSubmodelTemplate.objects.select_for_update().get(id=template.id)
    if template.status != AASSubmodelTemplate.Status.ACTIVE:
        raise ValidationError("Only an active template can be deprecated.")
    template.status = AASSubmodelTemplate.Status.DEPRECATED
    template.save(update_fields=["status", "updated_at"])
    out_of_date_resources = _mark_assigned_products_out_of_date(template)
    ProductAASSubmodelAssignment.objects.filter(template=template).delete()
    return out_of_date_resources
