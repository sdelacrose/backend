import re
import os
from django.core.management.base import BaseCommand
from django.conf import settings
from models.models import GlobalConfig, CompanyConfig
from project_management.models import ECPMPConfig

class Command(BaseCommand):
    help = 'Remove duplicate "Specification:SPE" values from configs'

    def handle(self, *args, **kwargs):
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]

        for model in models:
            # Fetch all ECPMPConfig objects
            configs = model.objects.all()

            for config in configs:
                document_types = [item for item in config.document_types if item != "Specification:SPE"]
                document_types.append("Specification:SPE")
                config.document_types = document_types
                config.save()
                self.stdout.write(self.style.SUCCESS(f"Updated config ID {config.id}"))