from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

# Define the new document types
NEW_DOCUMENT_TYPES = [
    "Funding Request",
    "Confirmation of Receipt",
    "Funding Authorisation"
]

class Command(BaseCommand):
    help = 'Populate new document types in GlobalConfig, CompanyConfig, and ECPMPConfig'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [GlobalConfig, CompanyConfig, ECPMPConfig]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                # Ensure there is an attribute for document types and update it
                if hasattr(config, 'document_types'):
                    existing_types = set(config.document_types or [])  # Get existing document types
                    new_types = set(NEW_DOCUMENT_TYPES)

                    if not new_types.issubset(existing_types):  # Check if update is needed
                        config.document_types = list(existing_types.union(new_types))  # Merge existing and new
                        config.save()
                        updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects with new document types')
        )
