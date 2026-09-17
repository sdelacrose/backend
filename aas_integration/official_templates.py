import hashlib
from dataclasses import asdict, dataclass
from urllib.parse import quote

import requests
from django.conf import settings
from rest_framework.exceptions import ValidationError

from .models import AASSubmodelTemplate
from .template_services import import_submodel_templates


IDTA_TEMPLATE_COMMIT = "a9664731a903b29ac5f45e23ab3a25c581f3d92f"
IDTA_TEMPLATE_REPOSITORY = "https://github.com/admin-shell-io/submodel-templates"


@dataclass(frozen=True)
class OfficialTemplate:
    key: str
    name: str
    id_short: str
    template_identifier: str
    version: str
    source_path: str
    sha256: str

    @property
    def source_url(self):
        encoded_path = quote(self.source_path, safe="/")
        return (
            "https://raw.githubusercontent.com/admin-shell-io/"
            f"submodel-templates/{IDTA_TEMPLATE_COMMIT}/{encoded_path}"
        )


OFFICIAL_TEMPLATES = (
    OfficialTemplate(
        key="predictive-maintenance",
        name="Predictive Maintenance",
        id_short="PredictiveMaintenance",
        template_identifier=(
            "https://admin-shell.io/idta/SubmodelTemplate/"
            "PredictiveMaintenance/1/0"
        ),
        version="1.0",
        source_path=(
            "published/Predictive Maintenance/1/0/"
            "IDTA 02048_Template_PredictiveMaintenance_"
            "forAASMetamodelV3.1.json"
        ),
        sha256="c0bc22bd9989ef0f4c8248ff08f3e03cf8e8676a476975c3b8d56771dae5b48b",
    ),
    OfficialTemplate(
        key="handover-documentation",
        name="Handover Documentation",
        id_short="HandoverDocumentation",
        template_identifier=(
            "https://admin-shell.io/idta/SubmodelTemplate/"
            "HandoverDocumentation/2/0"
        ),
        version="2.0.1",
        source_path=(
            "published/Handover Documentation/2/0/1/"
            "IDTA 02004-2-0-1_Template_HandoverDocumentation__"
            "forAASMetamodelV3.1.json"
        ),
        sha256="068ba24e4427facd9169dfc8163de46c4805b663cbd061dedd20dba669604689",
    ),
    OfficialTemplate(
        key="contact-information",
        name="Contact Information",
        id_short="ContactInformations",
        template_identifier=(
            "https://admin-shell.io/idta/SubmodelTemplate/"
            "ContactInformation/1/0"
        ),
        version="1.0.1",
        source_path=(
            "published/Contact Information/1/0/1/"
            "IDTA 02002-1-0-1_Template_ContactInformation_"
            "forAASMetamodelV3.1.json"
        ),
        sha256="a1f83c668e470f6d0a473300d1ff53e2ee4139d633de8489b14cece056284755",
    ),
    OfficialTemplate(
        key="provision-of-3d-models",
        name="Provision of 3D Models",
        id_short="Models3D",
        template_identifier=(
            "https://admin-shell.io/idta/SubmodelTemplate/"
            "ProvisionOf3DModels/1/0"
        ),
        version="1.0.1",
        source_path=(
            "published/Provision of 3D Models/1/0/1/"
            "IDTA 02026-1-0-1_Template_ProvisionOf3DModels_"
            "forAASMetamodelV3.1.json"
        ),
        sha256="894aad180e90ed5c6ba02b284eaf94aabe5404f4a49f967de941a1226b960259",
    ),
)
OFFICIAL_TEMPLATES_BY_KEY = {item.key: item for item in OFFICIAL_TEMPLATES}


def official_template_catalog(company) -> list[dict]:
    existing = {
        (template.template_identifier, template.version): template
        for template in AASSubmodelTemplate.objects.filter(
            company=company,
            template_identifier__in=[
                item.template_identifier for item in OFFICIAL_TEMPLATES
            ],
        )
    }
    result = []
    for item in OFFICIAL_TEMPLATES:
        template = existing.get((item.template_identifier, item.version))
        result.append(
            {
                **asdict(item),
                "source_repository": IDTA_TEMPLATE_REPOSITORY,
                "source_commit": IDTA_TEMPLATE_COMMIT,
                "imported_template_id": str(template.id) if template else None,
                "status": template.status if template else "not_imported",
            }
        )
    return result


def import_official_template(*, company, user, key: str):
    item = OFFICIAL_TEMPLATES_BY_KEY.get(key)
    if item is None:
        raise ValidationError({"template": "Choose a supported official template."})
    existing = AASSubmodelTemplate.objects.filter(
        company=company,
        template_identifier=item.template_identifier,
        version=item.version,
    ).first()
    if existing:
        return existing, False
    try:
        response = requests.get(item.source_url, timeout=(5, 30))
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ValidationError(
            {"template": "The official IDTA template could not be retrieved."}
        ) from exc
    payload = response.content
    if len(payload) > settings.AASX_PREVIEW_MAX_COMPRESSED_BYTES:
        raise ValidationError({"template": "The official template is too large."})
    if hashlib.sha256(payload).hexdigest() != item.sha256:
        raise ValidationError(
            {"template": "The official template checksum did not match the pinned version."}
        )
    templates = import_submodel_templates(
        company=company,
        user=user,
        file_name=f"{item.key}.json",
        payload=payload,
        version=item.version,
        profile_keys=["idta-product"],
    )
    template = next(
        (
            template
            for template in templates
            if template.template_identifier == item.template_identifier
        ),
        None,
    )
    if template is None:
        raise ValidationError(
            {"template": "The official package did not contain the expected submodel."}
        )
    return template, True
