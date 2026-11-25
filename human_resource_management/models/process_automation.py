"""L05 -- explicit, opt-in configuration for automatic offboarding
attribution ("attribution automatique du parcours adapte lorsque la date de
depart est connue"). Two small, company-scoped models, deliberately NOT a
generic rules engine: `OffboardingAutoTriggerConfig` is the single on/off
switch (plus who turned it on and when -- `StaffContractAuditLog` already
historises every change to `Staff.contract_end_date` itself, including its
author, so this config only needs to audit its OWN on/off history, not
duplicate that trail) and `OffboardingTemplateMapping` is a plain
contract_type -> template lookup table, matching ONLY the contract_type
values that actually exist on Staff (`permanent`/`temporary` --
StaffContractType, admin-server-models) -- no `CDI`/`CDD`/`alternance`
invented, per the ticket's explicit instruction; a staff whose contract_type
is unset or whose type has no mapping (and no default_template configured)
is signalled as blocked, never guessed (see
services.offboarding_automation)."""
from django.db import models
from models.models import CompanyProfile, StaffContractType, User

from human_resource_management.models.onboarding import OnboardingTemplate, ProcessKind


class OffboardingAutoTriggerConfig(models.Model):
    """One row per company -- `is_enabled=False` by default everywhere,
    never auto-created as enabled. `default_template` is an explicit,
    RH-chosen fallback for a resolvable-but-unmapped contract_type; it is
    NEVER a guess for an UNSET contract_type (that always blocks, regardless
    of whether a default_template is configured)."""

    company_profile = models.OneToOneField(CompanyProfile, on_delete=models.CASCADE, related_name="offboarding_auto_trigger_config")
    is_enabled = models.BooleanField(default=False)
    default_template = models.ForeignKey(
        OnboardingTemplate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    enabled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    enabled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.company_profile_id}: auto-trigger {'ON' if self.is_enabled else 'OFF'}"


class OffboardingTemplateMapping(models.Model):
    """`contract_type` is restricted to StaffContractType's REAL choices
    only (permanent/temporary) -- position and departure-reason mapping are
    explicitly deferred (no such field exists on Staff yet), never
    simulated. `unique_together` guarantees no ambiguous double-mapping for
    the same contract_type within a company."""

    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="offboarding_template_mappings")
    contract_type = models.CharField(max_length=16, choices=StaffContractType.choices)
    template = models.ForeignKey(
        OnboardingTemplate, on_delete=models.CASCADE, related_name="offboarding_contract_mappings",
        limit_choices_to={"process_kind": ProcessKind.OFFBOARDING},
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["company_profile", "contract_type"], name="unique_offboarding_mapping_per_contract_type"),
        ]

    def __str__(self):
        return f"{self.company_profile_id}: {self.contract_type} -> {self.template_id}"
