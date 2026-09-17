from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

DEFAULT_COLOR = "#000000"

class Command(BaseCommand):
    help = 'Fix status options that are missing color values by assigning a default color'

    def handle(self, *args, **kwargs):
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]
        total_fixed = 0

        for model in models:
            configs = model.objects.all()
            for config in configs:
                if not hasattr(config, 'status_options') or not config.status_options:
                    continue

                fixed_options = []
                needs_update = False

                for option in config.status_options:
                    if ':' not in option:
                        fixed_options.append(f"{option}:{DEFAULT_COLOR}")
                        needs_update = True
                    else:
                        fixed_options.append(option)

                if needs_update:
                    config.status_options = fixed_options
                    config.save()
                    total_fixed += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Fixed status options in {model.__name__} (ID: {config.id})"
                        )
                    )

        if total_fixed == 0:
            self.stdout.write(
                self.style.SUCCESS("No malformed status options found.")
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully fixed status options in {total_fixed} configuration objects."
                )
            )