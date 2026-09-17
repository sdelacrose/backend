from django.core.management.base import BaseCommand
from models.models import CompanyConfig
from models import constants


class Command(BaseCommand):
    help = "Set default fields for Core and Enterprise solutions if not already set."

    def handle(self, *args, **kwargs):
        solutions = ["Core Solution", "Enterprise Solution"]
        fields_to_set = {
            'priority_levels': constants.GC_PRIORITY_LEVELS,
            'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
            'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
            'leave_types': constants.GC_LEAVE_TYPES,
            'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
        }

        updated_count = 0

        for solution in solutions:
            configs = CompanyConfig.objects.filter(solution=solution)
            for config in configs:
                updated = False
                for field, default_value in fields_to_set.items():
                    if not getattr(config, field, None):
                        setattr(config, field, default_value)
                        updated = True
                if updated:
                    config.save()
                    updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f"Updated {updated_count} configurations with default values.")
        )
