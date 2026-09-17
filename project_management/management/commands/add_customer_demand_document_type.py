from django.core.management.base import BaseCommand

from project_management.models import ECPMPConfig
from project_management.utils import ensure_customer_demand_document_type


class Command(BaseCommand):
    help = 'Add Customer Demand as a default document type to all project configurations'

    def handle(self, *args, **kwargs):
        updated_count = 0

        for config in ECPMPConfig.objects.all():
            updated_types, changed = ensure_customer_demand_document_type(config.document_types)
            if changed:
                config.document_types = updated_types
                config.save(update_fields=["document_types"])
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Successfully updated {updated_count} project configuration(s) with Customer Demand document type'
            )
        )
