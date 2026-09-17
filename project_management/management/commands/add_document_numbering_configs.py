from django.core.management.base import BaseCommand
from project_management.models import ECPMPConfig
from models.models import GlobalConfig, CompanyConfig
from project_management.models.constants import (
    GC_DOCUMENT_CATEGORIES,
    GC_DOCUMENT_DISCIPLINES,
    GC_DOCUMENT_TYPES
)

class Command(BaseCommand):
    help = 'Add document numbering configuration fields to existing ECPMPConfig, GlobalConfig, and CompanyConfig objects'

    def handle(self, *args, **kwargs):
        updated_count = 0
        models = [ECPMPConfig, GlobalConfig, CompanyConfig]
        # models = [CompanyConfig]
        # Mapping from old simple strings to new "string:abb" format
        old_to_new_document_types = {
            "Supplier Invoice": "Supplier Invoice:SIN",
            "Customer Invoice": "Customer Invoice:CIN", 
            "Contract": "Contract:CON",
            "Specification": "Specification:SPE",
            "Funding Request": "Funding Request:FUR",
            "Confirmation of Receipt": "Confirmation of Receipt:COR",
            "Funding Authorisation": "Funding Authorisation:FUA"
        }

        for model in models:
            configs = model.objects.all()
            for config in configs:
                # Handle document_types - convert old format to new format and add new types
                if hasattr(config, 'document_types') and getattr(config, 'document_types'):
                    # Convert existing simple strings to new format
                    updated_types = []
                    existing_types = config.document_types
                    for doc_type in existing_types:
                        if doc_type in old_to_new_document_types:
                            # Convert old format to new format
                            updated_types.append(old_to_new_document_types[doc_type])
                        elif ":" in doc_type:
                            # Already in new format, keep as is
                            updated_types.append(doc_type)
                        else:
                            # Unknown old format, convert to new format with quotes
                            updated_types.append(doc_type+":"+doc_type.upper()[:3])

                    for new_type in GC_DOCUMENT_TYPES:
                        if new_type not in updated_types:
                            updated_types.append(new_type)
                    
                    setattr(config, 'document_types', updated_types)
                else:
                    # No existing document_types, assign the new format directly
                    setattr(config, 'document_types', GC_DOCUMENT_TYPES)
                
                # Add document numbering categories (new field)
                if not hasattr(config, 'document_categories') or not getattr(config, 'document_categories'):
                    setattr(config, 'document_categories', GC_DOCUMENT_CATEGORIES)
                
                # Add document numbering disciplines (new field)
                if not hasattr(config, 'document_disciplines') or not getattr(config, 'document_disciplines'):
                    setattr(config, 'document_disciplines', GC_DOCUMENT_DISCIPLINES)
                
                config.save()
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(f'Successfully updated {updated_count} configuration objects with document numbering fields')
        )