from django.core.management.base import BaseCommand
from models.models import GlobalConfig
from models import constants

class Command(BaseCommand):
    help = "Sync existing GlobalConfig objects with default values by adding missing values"

    def handle(self, *args, **kwargs):
        # Get all GlobalConfig objects
        configs = GlobalConfig.objects.all()
        updated_count = 0
        
        # Define default values from constants
        default_values = {
            'contract_type': constants.GC_CONTRACT_TYPES,
            'international_standard': constants.GC_INTERNATIONAL_STANDARDS,
                    'major_project_model': constants.GC_MAJOR_PROJECT_MODELS,
            'commercial_model': constants.GC_COMMERCIAL_MODELS,
            'supporting_agreement': constants.GC_SUPPORTING_AGREEMENTS,
            'team_members_roles': constants.GC_TEAM_MEMBERS_ROLES,
            'funding_strategy': constants.GC_FUNDING_STRATEGY,
            'status_options': constants.GC_STATUS_OPTIONS,
            'invoice_status': constants.GC_INVOICE_STATUS,
            'reminder_settings': constants.GC_REMINDER_SETTINGS,
            'expense_types': constants.GC_EXPENSE_TYPES,
            'project_types': constants.GC_PROJECT_TYPES,
            'document_types': constants.GC_COMPANY_DOCUMENT_TYPES,
            'document_status': constants.GC_DOCUMENT_STATUSES,
            'document_internal_review_status': constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
            'document_external_review_status': constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
            'transmittal_category': constants.GC_TRANSMITTAL_CATEGORIES,
            'priority_levels': constants.GC_PRIORITY_LEVELS,
            'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
            'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
            'leave_types': constants.GC_LEAVE_TYPES,
            'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
        }
        
        for config in configs:
            needs_update = False
            synced_fields = {}
            
            # Check each field and add missing values
            for field, default_value in default_values.items():
                current_value = getattr(config, field, None)
                
                # If field is empty or None, set default value
                if not current_value:
                    setattr(config, field, default_value)
                    synced_fields[field] = default_value
                    needs_update = True
                    continue
                
                # If field has values but is missing some default values
                if isinstance(current_value, list) and isinstance(default_value, list):
                    # Convert to sets for comparison (case insensitive)
                    current_set = {str(item).lower() for item in current_value}
                    default_set = {str(item).lower() for item in default_value}
                    
                    # Find missing values
                    missing_values = [item for item in default_value 
                                     if str(item).lower() not in current_set]
                    
                    # Add missing values
                    if missing_values:
                        setattr(config, field, current_value + missing_values)
                        synced_fields[field] = missing_values
                        needs_update = True
            
            if needs_update:
                config.save()
                updated_count += 1
                config_name = config.company_profile.company_name if config.company_profile else config.user.email
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Updated GlobalConfig for {config_name}"
                    )
                )
                
                # Display synced fields and values
                for field, values in synced_fields.items():
                    if isinstance(values, list):
                        self.stdout.write(f"  - {field}: Added {len(values)} values: {', '.join(str(v) for v in values)}")
                    else:
                        self.stdout.write(f"  - {field}: Set default values")
        
        self.stdout.write(
            self.style.SUCCESS(
                f"Successfully synced {updated_count} GlobalConfig objects with default values."
            )
        )
