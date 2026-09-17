from django.core.management.base import BaseCommand
from models.models import CompanyConfig
from models.constants import (
    GC_OPPORTUNITY_SALES_PHASE,
    GC_CALENDAR_EVENT_STATUS_OPTIONS
)

class Command(BaseCommand):
    help = "Add opportunity_sales_phase and calendar_event_status_options to company configs where they are empty"

    def handle(self, *args, **kwargs):
        updated_count = 0

        # Update CompanyConfig objects
        company_configs = CompanyConfig.objects.all()
        for config in company_configs:
            needs_update = False
            
            if not config.opportunity_sales_phase:
                config.opportunity_sales_phase = GC_OPPORTUNITY_SALES_PHASE
                needs_update = True
                
            if not config.calendar_event_status_options:
                config.calendar_event_status_options = GC_CALENDAR_EVENT_STATUS_OPTIONS
                needs_update = True
                
            if needs_update:
                config.save()
                updated_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Updated CompanyConfig for {config.company_profile.company_name if config.company_profile else config.user.email}"
                    )
                )

        # Print summary
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully updated {updated_count} company configurations."
            )
        )
