from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

class Command(BaseCommand):
    help = 'Update default status option colors in existing configuration objects'

    # Define the current and updated status options
    DEFAULT_STATUS_OPTIONS = {
        "open": "#99dce8",
        "in progress": "#fff3ad",
        "closed": "#bdefe0",
    }

    def handle(self, *args, **kwargs):
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]
        total_updated = 0

        for model in models:
            updated_count = 0

            for config in model.objects.all():
                updated_status_options = []

                for option in config.status_options:
                    status, _, _ = option.partition(":")

                    if status in self.DEFAULT_STATUS_OPTIONS:
                        new_color = self.DEFAULT_STATUS_OPTIONS[status]
                        updated_status_options.append(f"{status}:{new_color}")
                    else:
                        updated_status_options.append(option)

                if updated_status_options != config.status_options:
                    config.status_options = updated_status_options
                    config.save()
                    updated_count += 1
                    total_updated += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"Updated {updated_count} {model.__name__} objects."
                )
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Status options updated successfully in {total_updated} configuration objects."
            )
        )
