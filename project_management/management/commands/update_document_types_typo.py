from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

class Command(BaseCommand):
    help = 'Update Other Field typo in document types configuration fields to existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                for document_type in config.document_types:
                    if "Other" in document_type:
                        config.document_types.remove(document_type)
                        config.document_types.append("Other:OTH")

                config.save()
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects with document numbering fields')
        )