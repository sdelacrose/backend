from copy import deepcopy
import logging

from django.db import transaction

from models import constants
from models.configuration_templates import (
    NESTED_DERIVED_FIELDS,
    PROJECT_CONFIGURATION_FIELDS,
    get_project_template_defaults,
)
from models.hierarchical_configurations import (
    DOCUMENT_DISCIPLINES_KEY,
    flatten_nested_configurations,
    resolve_nested_configurations,
)
from models.models import GlobalConfig, ProjectConfigTemplate
from project_management.models import ECPMPConfig


logger = logging.getLogger(__name__)


def _project_owner(project):
    if project.user and project.user.is_freelancer:
        return {"user": project.user}

    company = project.company_profile or getattr(project.user, "company", None)
    if company is not None:
        return {"company_profile": company}
    raise ValueError("The project does not have a configuration owner.")


@transaction.atomic
def ensure_project_config_template(project):
    owner = _project_owner(project)
    template = ProjectConfigTemplate.objects.filter(**owner).first()
    if template is not None:
        return template

    source = GlobalConfig.objects.filter(**owner).first()
    template = ProjectConfigTemplate.objects.create(
        **owner,
        **get_project_template_defaults(source),
    )
    logger.warning(
        "Repaired missing project configuration template for project_id=%s",
        project.id,
    )
    return template


def build_project_configuration_values(project):
    template = ensure_project_config_template(project)
    values = {
        field_name: deepcopy(getattr(template, field_name))
        for field_name in PROJECT_CONFIGURATION_FIELDS
        if field_name not in NESTED_DERIVED_FIELDS.values()
    }
    nested = resolve_nested_configurations(values["nested_configurations"])
    nested = {DOCUMENT_DISCIPLINES_KEY: nested[DOCUMENT_DISCIPLINES_KEY]}
    flattened = flatten_nested_configurations(nested)
    values["nested_configurations"] = nested
    for config_key, field_name in NESTED_DERIVED_FIELDS.items():
        values[field_name] = flattened[config_key]

    # Project priority and rooms remain local project-level configuration.
    values.update(
        {
            "project_priority_levels": list(constants.GC_PROJECT_PRIORITY_LEVELS),
            "rooms": list(constants.GC_ROOMS),
        }
    )
    return values


def create_project_configuration(project):
    return ECPMPConfig.objects.create(
        project=project,
        **build_project_configuration_values(project),
    )
