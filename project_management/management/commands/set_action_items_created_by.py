from django.core.management.base import BaseCommand
from django.db import transaction
from project_management.models import ActionItem
from models.models import User

class Command(BaseCommand):
    help = "Set created_by field for existing ActionItems based on company admin ,freelancer and project creator"

    def handle(self, *args, **kwargs):
        with transaction.atomic():
            updated_company_items = 0
            updated_project_items = 0
            updated_freelancer_items = 0
            skipped_items = 0

            action_items = ActionItem.objects.all()

            # Handle company action items
            company_action_items = action_items.filter(
                company_profile__isnull=False,
                created_by__isnull=True
            )
            
            for item in company_action_items:
                if item.company_profile.admin_user:
                    item.created_by = item.company_profile.admin_user
                    item.save()

                    for sub_item in item.sub_action_items.filter(created_by__isnull=True):
                        sub_item.created_by = item.created_by
                        sub_item.save()
                        updated_company_items += 1

                    updated_company_items += 1
                else:
                    skipped_items += 1

            # Handle project-related action items
            project_action_items = action_items.filter(
                milestone__isnull=False,
                created_by__isnull=True,
                parent_action_item__isnull=True  # Only get parent action items
            )
            
            for item in project_action_items:
                if item.milestone and item.milestone.project and item.milestone.project.user:
                    item.created_by = item.milestone.project.user
                    item.save()
                    
                    # # Update all sub-action items to match parent's created_by
                    for sub_item in item.sub_action_items.filter(created_by__isnull=True):
                        sub_item.created_by = item.created_by
                        sub_item.save()
                        updated_project_items += 1
                    
                    updated_project_items += 1
                else:
                    skipped_items += 1

            # Handle freelancer action items
            freelancer_action_items = action_items.filter(
                freelancer__isnull=False,
                created_by__isnull=True
            )
            
            for item in freelancer_action_items:
                item.created_by = item.freelancer
                item.save()

                for sub_item in item.sub_action_items.filter(created_by__isnull=True):
                    sub_item.created_by = item.created_by
                    sub_item.save()
                    updated_freelancer_items += 1

                updated_freelancer_items += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully updated ActionItems:\n"
                    f"- Company related action items: {updated_company_items}\n"
                    f"- Project related action items: {updated_project_items}\n"
                    f"- Freelancer related action items: {updated_freelancer_items}\n"
                    f"- Skipped actionitems: {skipped_items}"
                )
            )
