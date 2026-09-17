from django.core.management.base import BaseCommand
from models.models import User, CompanyConfig, CompanyProfile
from models import constants
from models.configuration_templates import get_company_configuration_defaults
from project_management.models import Folder

class Command(BaseCommand):
    help = "Ensure each company has a corresponding CompanyConfig entry"

    def handle(self, *args, **kwargs):
        users = User.objects.filter(user_type="freelancer")
        configuration_defaults = get_company_configuration_defaults()
        created_count = 0
        skipped_count = 0

        for user in users:
            config, created = CompanyConfig.objects.get_or_create(
                user=user,
                defaults={
                    "contract_type": constants.GC_CONTRACT_TYPES,
                    "international_standard": constants.GC_INTERNATIONAL_STANDARDS,
                "major_project_model": constants.GC_MAJOR_PROJECT_MODELS,
                    "commercial_model": constants.GC_COMMERCIAL_MODELS,
                    "supporting_agreement": constants.GC_SUPPORTING_AGREEMENTS,
                    "team_members_roles": constants.GC_TEAM_MEMBERS_ROLES,
                    "nested_configurations": configuration_defaults[
                        "nested_configurations"
                    ],
                    "funding_strategy": constants.GC_FUNDING_STRATEGY,
                    "status_options": constants.GC_STATUS_OPTIONS,
                    "invoice_status": constants.GC_INVOICE_STATUS,
                    "reminder_settings": constants.GC_REMINDER_SETTINGS,
                    "expense_types": constants.GC_EXPENSE_TYPES,
                    "project_types": constants.GC_PROJECT_TYPES,
                    "document_types": constants.GC_COMPANY_DOCUMENT_TYPES,
                    "document_status": constants.GC_DOCUMENT_STATUSES,
                    "document_internal_review_status": constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                    "document_external_review_status": constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                    "transmittal_category": constants.GC_TRANSMITTAL_CATEGORIES,
                    "priority_levels": constants.GC_PRIORITY_LEVELS,
                    "project_priority_levels": constants.GC_PROJECT_PRIORITY_LEVELS,
                },
            )

            if created:
                created_count += 1
                self.stdout.write(self.style.SUCCESS(f"Created CompanyConfig for {user.full_name}"))
            else:
                skipped_count += 1
                self.stdout.write(self.style.WARNING(f"CompanyConfig already exists for {user.full_name}, skipping..."))

        self.stdout.write(self.style.SUCCESS(f"\nProcess completed: {created_count} created, {skipped_count} skipped."))
       
        
        # Define the list of predefined subfolders
        predefined_folders = [
            "project information",
            "client correspondence",
            "other correspondence",
            "meeting records",
            "invoices"
        ]
        
        
        for user in users:
            # Create root folder for the project
            root_folder, _  = Folder.objects.get_or_create(
                freelancer=user,
                name=f"{user.id}_freelancer_root_folder",
                parent=None, 
            )
            # Create predefined folders under the root folder
            for folder_name in predefined_folders:
                Folder.objects.get_or_create(
                    freelancer=user,
                    name=folder_name,
                    parent=root_folder,
                )
        
        companies = CompanyProfile.objects.all()
        for company in companies:
            # Create root folder for the project
            root_folder, _ = Folder.objects.get_or_create(
                company_profile=company,
                name=f"{company.id}_company_root_folder",
                parent=None, 
            )
            # Create predefined folders under the root folder
            for folder_name in predefined_folders:
                Folder.objects.get_or_create(
                    company_profile=company,
                    name=folder_name,
                    parent=root_folder,
                )        
