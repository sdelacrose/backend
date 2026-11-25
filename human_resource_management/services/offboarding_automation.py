"""L05 -- "attribution automatique du parcours adapte des que la date de
depart est connue" (spec 6.9, criteria 1/2), opt-in per company
(`OffboardingAutoTriggerConfig.is_enabled`, False everywhere until an
explicitly authorized person turns it on -- see views for who may).

Eligibility and template resolution are entirely LIVE, recomputed on every
call, never cached: a company that fixes an ambiguous mapping sees the
affected staff become eligible on the very next run, with no flag to flip
anywhere else. The one piece of state this reads is `OnboardingInstance`
itself (never a separate ledger) -- see its own docstring for why
`trigger_key` alone is enough to make "no recreation after cancellation for
the SAME departure event, but eligible again for a NEW one" true without any
extra bookkeeping model."""
from human_resource_management.models.onboarding import OnboardingInstance, ProcessKind
from human_resource_management.models.process_automation import OffboardingAutoTriggerConfig, OffboardingTemplateMapping
from human_resource_management.services.onboarding import OnboardingError, trigger_onboarding
from models.models import Staff

BLOCKED_NO_CONTRACT_TYPE = "contract_type_unknown"
BLOCKED_NO_TEMPLATE = "no_template_mapped"
BLOCKED_NO_CONFIG = "auto_trigger_not_enabled"


def resolve_offboarding_template_for_staff(staff, config):
    """Never guesses: an unset `contract_type` always blocks (CDI/CDD/
    alternance are explicitly NOT inferred from the temporary/permanent
    field alone, per the ticket's own instruction), and an unmapped-but-
    known contract_type only resolves via an EXPLICIT `default_template` --
    never a silent pick among several mappings (the mapping table's own
    unique constraint already rules out an ambiguous double-mapping for the
    same contract_type)."""
    if not staff.contract_type:
        return None, BLOCKED_NO_CONTRACT_TYPE
    mapping = OffboardingTemplateMapping.objects.filter(
        company_profile_id=config.company_profile_id, contract_type=staff.contract_type,
    ).select_related("template").first()
    if mapping is not None:
        return mapping.template, None
    if config.default_template_id:
        return config.default_template, None
    return None, BLOCKED_NO_TEMPLATE


def _eligible_staff_queryset(company):
    return Staff.objects.filter(
        user__company=company, user__is_active=True, contract_end_date__isnull=False,
    ).select_related("user", "supervisor__user")


def preview_auto_trigger_candidates(company):
    """Read-only dry run -- "apercu des dossiers concernes avant premiere
    activation". Returns one row per eligible-by-date staff member,
    regardless of whether the config is currently enabled (so RH can preview
    the effect BEFORE turning the switch on, exactly as asked)."""
    config = OffboardingAutoTriggerConfig.objects.filter(company_profile=company).first()
    rows = []
    for staff in _eligible_staff_queryset(company):
        trigger_key = staff.contract_end_date.isoformat()
        already_handled = OnboardingInstance.objects.filter(
            staff=staff, process_kind=ProcessKind.OFFBOARDING, trigger_key=trigger_key,
        ).exists()
        if already_handled:
            rows.append({
                "staff_id": staff.id, "contract_end_date": staff.contract_end_date,
                "status": "already_handled", "blocked_reason": None, "template_id": None,
            })
            continue
        if config is None:
            template, blocked_reason = None, BLOCKED_NO_CONFIG
        else:
            template, blocked_reason = resolve_offboarding_template_for_staff(staff, config)
        rows.append({
            "staff_id": staff.id, "contract_end_date": staff.contract_end_date,
            "status": "would_trigger" if template is not None else "blocked",
            "blocked_reason": blocked_reason, "template_id": template.id if template else None,
        })
    return rows


def run_auto_trigger_for_company(company):
    """The actual job body (see management command `auto_trigger_offboarding`
    and `tasks.auto_trigger_offboarding_task`) -- triggers through the EXACT
    same `trigger_onboarding` service as a manual RH click
    (source=SOURCE_AUTOMATIC, actor=None: no human initiated this one).
    Never runs at all if the company has not explicitly enabled it."""
    config = OffboardingAutoTriggerConfig.objects.filter(company_profile=company, is_enabled=True).first()
    if config is None:
        return {"triggered": 0, "blocked": 0, "skipped": "not_enabled"}
    triggered, blocked = 0, 0
    for staff in _eligible_staff_queryset(company):
        trigger_key = staff.contract_end_date.isoformat()
        if OnboardingInstance.objects.filter(staff=staff, process_kind=ProcessKind.OFFBOARDING, trigger_key=trigger_key).exists():
            continue
        template, blocked_reason = resolve_offboarding_template_for_staff(staff, config)
        if template is None:
            blocked += 1
            continue
        try:
            trigger_onboarding(
                staff=staff, template=template, anchor_date=staff.contract_end_date, actor=None,
                source=OnboardingInstance.SOURCE_AUTOMATIC, trigger_key=trigger_key,
            )
            triggered += 1
        except OnboardingError:
            # A required step's responsible could not be resolved -- exactly
            # the same "signaler le blocage, sans choisir arbitrairement"
            # rule as a manual trigger; this candidate stays open for the
            # NEXT run (never silently retried with a guessed destinataire,
            # never silently dropped either).
            blocked += 1
    return {"triggered": triggered, "blocked": blocked}
