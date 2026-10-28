from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from human_resource_management.models.onboarding import OnboardingTemplate, ProcessKind
from human_resource_management.models.process_automation import OffboardingAutoTriggerConfig, OffboardingTemplateMapping
from human_resource_management.permissions import can_manage_offboarding_automation
from human_resource_management.services.offboarding_automation import preview_auto_trigger_candidates
from models.models import StaffContractType


def serialize_config(config):
    if config is None:
        return {"is_enabled": False, "default_template_id": None, "enabled_by": None, "enabled_at": None}
    return {
        "is_enabled": config.is_enabled,
        "default_template_id": config.default_template_id,
        "enabled_by": {"id": config.enabled_by_id, "name": getattr(config.enabled_by, "full_name", None)} if config.enabled_by_id else None,
        "enabled_at": config.enabled_at,
    }


def serialize_mapping(mapping):
    return {
        "id": mapping.id,
        "contract_type": mapping.contract_type,
        "template_id": mapping.template_id,
    }


class OffboardingAutoTriggerConfigViewSet(viewsets.GenericViewSet):
    """L05: one row per company -- "configuration permettant un choix
    explicite... activation volontaire de l'attribution automatique". GET
    returns a default/disabled shape even before any row exists (never a
    404 for "not configured yet" -- that is simply the OFF state)."""

    def _company(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            raise DRFPermissionDenied("No company associated with this account.")
        return company

    def retrieve(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to view this company's offboarding automation configuration.")
        config = OffboardingAutoTriggerConfig.objects.filter(company_profile=company).first()
        return Response(serialize_config(config))

    def partial_update(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to manage this company's offboarding automation configuration.")
        config, _ = OffboardingAutoTriggerConfig.objects.get_or_create(company_profile=company)
        if "is_enabled" in request.data:
            new_value = bool(request.data["is_enabled"])
            if new_value and not config.is_enabled:
                config.enabled_by = request.user
                config.enabled_at = timezone.now()
            config.is_enabled = new_value
        if "default_template" in request.data:
            template_id = request.data["default_template"]
            if template_id:
                template = get_object_or_404(OnboardingTemplate, pk=template_id, company_profile=company, process_kind=ProcessKind.OFFBOARDING)
                config.default_template = template
            else:
                config.default_template = None
        config.save()
        return Response(serialize_config(config))

    def preview(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to preview this company's offboarding automation.")
        return Response(preview_auto_trigger_candidates(company))


class OffboardingTemplateMappingViewSet(viewsets.GenericViewSet):
    def _company(self, request):
        company = getattr(request.user, "company", None)
        if company is None:
            raise DRFPermissionDenied("No company associated with this account.")
        return company

    def list(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to view this company's offboarding template mappings.")
        mappings = OffboardingTemplateMapping.objects.filter(company_profile=company)
        return Response([serialize_mapping(m) for m in mappings])

    def create(self, request, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to manage this company's offboarding template mappings.")
        contract_type = request.data.get("contract_type")
        if contract_type not in StaffContractType.values:
            raise ValidationError({"contract_type": "Must be a real Staff.contract_type value (permanent/temporary)."})
        template = get_object_or_404(OnboardingTemplate, pk=request.data.get("template"), company_profile=company, process_kind=ProcessKind.OFFBOARDING)
        if OffboardingTemplateMapping.objects.filter(company_profile=company, contract_type=contract_type).exists():
            raise ValidationError({"contract_type": "This contract type already has a mapped template."})
        mapping = OffboardingTemplateMapping.objects.create(
            company_profile=company, contract_type=contract_type, template=template, created_by=request.user,
        )
        return Response(serialize_mapping(mapping), status=status.HTTP_201_CREATED)

    def destroy(self, request, pk=None, *args, **kwargs):
        company = self._company(request)
        if not can_manage_offboarding_automation(request.user, company):
            raise DRFPermissionDenied("You do not have permission to manage this company's offboarding template mappings.")
        mapping = get_object_or_404(OffboardingTemplateMapping, pk=pk, company_profile=company)
        mapping.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
