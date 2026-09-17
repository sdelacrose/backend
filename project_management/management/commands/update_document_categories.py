from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

class Command(BaseCommand):
    help = 'Add Calendar and Revision document types to existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [ECPMPConfig]
        
        document_categories_to_add = ["Calendar:CAL", "Revision:REV"]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                updated = False
                for doc_type in document_categories_to_add:
                    if doc_type not in config.document_categories:
                        config.document_categories.append(doc_type)
                        updated = True

                if updated:
                    config.save()
                    updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects with added document categories')
        )
