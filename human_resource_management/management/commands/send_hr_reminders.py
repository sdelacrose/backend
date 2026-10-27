from django.core.management.base import BaseCommand

from human_resource_management.services.hr_reminders import (
    process_onboarding_step_reminders, process_staff_document_validity_reminders,
)


class Command(BaseCommand):
    help = (
        "L03/L05: processes onboarding-step due-date reminders and "
        "document-validity expiry reminders, reusing the exact AI-1685 "
        "dedup/retry/stop-at-resolution shape. Same local-only scheduling "
        "precedent as send_leave_balance_reminders -- see "
        "ecpmpapp/settings/local.py's CELERY_BEAT_SCHEDULE; NOT wired into "
        "the real production CronJob (manifests/ecpmp-api-deployment-prod."
        "yaml), same as the AI-1685 command it reuses the pattern from."
    )

    def handle(self, *args, **options):
        step_result = process_onboarding_step_reminders(manual=False)
        doc_result = process_staff_document_validity_reminders(manual=False)
        self.stdout.write(self.style.SUCCESS(
            f"Onboarding step reminders: sent={step_result['sent']} failed={step_result['failed']} ignored={step_result['ignored']}"
        ))
        self.stdout.write(self.style.SUCCESS(
            f"Document validity reminders: sent={doc_result['sent']} failed={doc_result['failed']} ignored={doc_result['ignored']}"
        ))
