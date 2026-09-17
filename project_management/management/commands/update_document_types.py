from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

class Command(BaseCommand):
    help = 'Remove Calendar and Revision document types from existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]
        
        document_types_to_remove = ["Calendar:CAL", "Revision:REV"]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                for doc_type in document_types_to_remove:
                    if doc_type in config.document_types:
                        config.document_types.remove(doc_type)

                config.save()
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects with document numbering fields')
        )