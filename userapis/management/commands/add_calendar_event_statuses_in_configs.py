from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig
from models.constants import GC_CALENDAR_EVENT_STATUS_OPTIONS  # Ensure this is imported

class Command(BaseCommand):
    help = 'Add calendar event statuses to existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                config.calendar_event_status_options = GC_CALENDAR_EVENT_STATUS_OPTIONS
                config.save()
                updated_count += 1

        self.stdout.write(self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects across ECPMPConfig, GlobalConfig, and CompanyConfig'))
