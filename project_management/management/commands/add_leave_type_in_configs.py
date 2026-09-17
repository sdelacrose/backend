from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig

class Command(BaseCommand):
    help = 'Add "other" as an invoice status to existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [GlobalConfig, CompanyConfig, ECPMPConfig]

        for model in models:
            configs = model.objects.all()
            for config in configs:
                if "Sent" not in config.document_status:
                    config.document_status.append("Sent")
                    config.save()
                    updated_count += 1

        self.stdout.write(self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects across ECPMPConfig, GlobalConfig, and CompanyConfig'))
