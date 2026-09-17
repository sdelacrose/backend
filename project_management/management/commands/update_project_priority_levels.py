from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig

# Define new statuses
GC_PROJECT_PRIORITY_LEVELS = ["Very Low","Low", "Medium", "High", "Very High"]

class Command(BaseCommand):
    help = "Update project priority levels in ECPMP ang GlobalConfig"

    def handle(self, *args, **kwargs):
        # Update GlobalConfig objects
        GlobalConfig.objects.update(project_priority_levels=GC_PROJECT_PRIORITY_LEVELS)
        ECPMPConfig.objects.update(project_priority_levels=GC_PROJECT_PRIORITY_LEVELS)

        # Output results
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully updated project priority levels in all GlobalConfig objects "
            )
        )
