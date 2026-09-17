import re
from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

# Regex pattern for valid reminder strings
VALID_REMINDER_PATTERN = re.compile(r"^\d+\s?(day|days|week|weeks|month|months)$", re.IGNORECASE)

class Command(BaseCommand):
    help = "Clean invalid values from reminder_settings in all ECPMPConfig, ComapnyConfig and GlobalConfig instances"

    def handle(self, *args, **kwargs):
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]
        total_fixed = 0

        for model in models:
            configs = model.objects.all()
            for config in configs:
                original_settings = config.reminder_settings or []
                cleaned_settings = [
                    item for item in original_settings
                    if item.lower() == "none" or VALID_REMINDER_PATTERN.match(item)
                ]

                if cleaned_settings != original_settings:
                    config.reminder_settings = cleaned_settings
                    config.save()
                    total_fixed += 1
                    self.stdout.write(f"Updated config ID {config.id}: {original_settings} -> {cleaned_settings}")

            self.stdout.write(self.style.SUCCESS(f"{model.__name__} Cleanup complete. {total_fixed} configs updated."))
