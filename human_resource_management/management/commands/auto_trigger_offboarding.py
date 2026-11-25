from django.core.management.base import BaseCommand

from human_resource_management.models.process_automation import OffboardingAutoTriggerConfig
from human_resource_management.services.offboarding_automation import run_auto_trigger_for_company


class Command(BaseCommand):
    help = (
        "L05: automatic offboarding attribution -- for every company with "
        "OffboardingAutoTriggerConfig.is_enabled=True, triggers an "
        "offboarding instance (via the EXACT same service as a manual RH "
        "click) for every Staff whose contract_end_date is known and not "
        "yet handled for that exact date (see OnboardingInstance.trigger_key "
        "docstring). Local-only scheduling precedent, same as "
        "send_hr_reminders/send_leave_balance_reminders -- NOT wired into "
        "the real production CronJob (manifests/ecpmp-api-deployment-prod."
        "yaml)."
    )

    def handle(self, *args, **options):
        total_triggered, total_blocked = 0, 0
        for config in OffboardingAutoTriggerConfig.objects.filter(is_enabled=True).select_related("company_profile"):
            result = run_auto_trigger_for_company(config.company_profile)
            total_triggered += result.get("triggered", 0)
            total_blocked += result.get("blocked", 0)
            self.stdout.write(self.style.SUCCESS(
                f"Company {config.company_profile_id}: triggered={result.get('triggered', 0)} blocked={result.get('blocked', 0)}"
            ))
        self.stdout.write(self.style.SUCCESS(f"Total: triggered={total_triggered} blocked={total_blocked}"))
