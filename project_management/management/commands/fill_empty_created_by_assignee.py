from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

# Import all models that have created_by or assignee fields
from project_management.models import (
    ActionItem, ObjectAssessment, Document, SiteReport,
    Project, ProjectPhase, ProjectFunding, PurchaseOrder,
    TimeCard, ExpenseCard, Invoice
)
from companyprofileapis.models import Opportunity
from userapis.models import CalendarEvent
from human_resource_management.models import StaffObservation
from models.models import User, CompanyProfile, Role


class Command(BaseCommand):
    help = "Fill empty created_by and assignee fields with appropriate fallback values"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be updated without making changes',
        )
        parser.add_argument(
            '--models',
            nargs='+',
            help='Specific models to update (e.g., ActionItem Document)',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        specific_models = options.get('models', [])
        
        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN MODE - No changes will be made"))
        
        # Define model processors
        model_processors = {
            'ActionItem': self.process_action_items,
            'ObjectAssessment': self.process_object_assessments,
            'Document': self.process_documents,
            'SiteReport': self.process_site_reports,
            'Project': self.process_projects,
            'ProjectPhase': self.process_project_phases,
            'ProjectFunding': self.process_project_fundings,
            'PurchaseOrder': self.process_purchase_orders,
            'TimeCard': self.process_time_cards,
            'ExpenseCard': self.process_expense_cards,
            'Invoice': self.process_invoices,
            'Opportunity': self.process_opportunities,
            'CalendarEvent': self.process_calendar_events,
            'StaffObservation': self.process_staff_observations,
        }

        # Filter models if specific models are provided
        if specific_models:
            model_processors = {k: v for k, v in model_processors.items() if k in specific_models}

        total_updated = 0
        
        with transaction.atomic():
            for model_name, processor in model_processors.items():
                self.stdout.write(f"\nProcessing {model_name}...")
                model_updated = processor(dry_run)
                total_updated += model_updated
                
                if model_updated > 0:
                    self.stdout.write(
                        self.style.SUCCESS(f"Updated {model_updated} {model_name} records")
                    )
                else:
                    self.stdout.write(f"No {model_name} records needed updating")

        self.stdout.write(
            self.style.SUCCESS(f"\nTotal records updated: {total_updated}")
        )

    def process_action_items(self, dry_run):
        """Process ActionItem model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = ActionItem.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_action_item_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for ActionItem ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ActionItem ID {item.id}")

        # Handle assignees relation
        empty_assignees = ActionItem.objects.filter(assignees__isnull=True).distinct()
        for item in empty_assignees:
            fallback_user = self.get_action_item_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.assignees.set([fallback_user])
                updated_count += 1
                self.stdout.write(f"  - Set assignees to {fallback_user.email} for ActionItem ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ActionItem ID {item.id}")

        return updated_count

    def get_action_item_fallback_user(self, item):
        """Get fallback user for ActionItem - priority: freelancer > project user > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.milestone and item.milestone.project:
            project = item.milestone.project
            if project.user:
                return project.user
            if project.company_profile and project.company_profile.admin_user:
                return project.company_profile.admin_user
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_object_assessments(self, dry_run):
        """Process ObjectAssessment model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = ObjectAssessment.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_object_assessment_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for ObjectAssessment ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ObjectAssessment ID {item.id}")

        # Handle assignee field
        empty_assignee = ObjectAssessment.objects.filter(assignee__isnull=True)
        for item in empty_assignee:
            if item.author:
                fallback_user = item.author
            else:
                fallback_user = self.get_object_assessment_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.assignee = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set assignee to {fallback_user.email} for ObjectAssessment ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ObjectAssessment ID {item.id}")

        return updated_count

    def get_object_assessment_fallback_user(self, item):
        """Get fallback user for ObjectAssessment - priority: project user > company admin"""
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        return None

    def process_documents(self, dry_run):
        """Process Document model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = Document.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_document_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for Document ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for Document ID {item.id}")

        # Handle assignee field
        empty_assignee = Document.objects.filter(assignee__isnull=True)
        for item in empty_assignee:
            if item.author:
                fallback_user = item.author
            else:
                fallback_user = self.get_document_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.assignee = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set assignee to {fallback_user.email} for Document ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for Document ID {item.id}")

        return updated_count

    def get_document_fallback_user(self, item):
        """Get fallback user for Document - priority: project user > company admin"""
        if item.folder and item.folder.project:
            project = item.folder.project
            if project.user:
                return project.user
            if project.company_profile and project.company_profile.admin_user:
                return project.company_profile.admin_user
        
        if item.folder.company_profile:
            return item.folder.company_profile.admin_user
        
        if item.folder.freelancer:
            return item.folder.freelancer
        
        return None

    def process_site_reports(self, dry_run):
        """Process SiteReport model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = SiteReport.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            if item.observer and item.observer.team_member:
                fallback_user = item.observer.team_member
            else:
                fallback_user = self.get_site_report_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for SiteReport ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for SiteReport ID {item.id}")

        # Handle assignees field
        empty_assignees = SiteReport.objects.filter(assignees__isnull=True)
        for item in empty_assignees:
            # Add observer's team member to assignees
            if item.observer and item.observer.team_member:
                if not dry_run:
                    item.assignees.add(item.observer.team_member)
                updated_count += 1
                self.stdout.write(f"  - Added observer {item.observer.team_member.email} to assignees for SiteReport ID {item.id}")
            
            # Add all present team members to assignees
            for present_member in item.present.all():
                if present_member.team_member:
                    if not dry_run:
                        item.assignees.add(present_member.team_member)
                    updated_count += 1
                    self.stdout.write(f"  - Added present member {present_member.team_member.email} to assignees for SiteReport ID {item.id}")
            
            if not dry_run:
                item.save()

        return updated_count

    def get_site_report_fallback_user(self, item):
        """Get fallback user for SiteReport - priority: project user > company admin"""
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        return None

    def process_projects(self, dry_run):
        """Process Project model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = Project.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_project_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for Project ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for Project ID {item.id}")

        return updated_count

    def get_project_fallback_user(self, item):
        """Get fallback user for Project - priority: freelancer > company admin"""
        if item.user:  # freelancer
            return item.user
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_project_phases(self, dry_run):
        """Process ProjectPhase model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = ProjectPhase.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_project_phase_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for ProjectPhase ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ProjectPhase ID {item.id}")

        return updated_count

    def get_project_phase_fallback_user(self, item):
        """Get fallback user for ProjectPhase - priority: project user > company admin"""
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        return None

    def process_project_fundings(self, dry_run):
        """Process ProjectFunding model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = ProjectFunding.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_project_funding_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for ProjectFunding ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ProjectFunding ID {item.id}")

        return updated_count

    def get_project_funding_fallback_user(self, item):
        """Get fallback user for ProjectFunding - priority: project user > company admin"""
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        return None

    def process_purchase_orders(self, dry_run):
        """Process PurchaseOrder model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = PurchaseOrder.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_purchase_order_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for PurchaseOrder ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for PurchaseOrder ID {item.id}")

        return updated_count

    def get_purchase_order_fallback_user(self, item):
        """Get fallback user for PurchaseOrder - priority: project user > company admin"""
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        return None

    def process_time_cards(self, dry_run):
        """Process TimeCard model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = TimeCard.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_time_card_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for TimeCard ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for TimeCard ID {item.id}")

        return updated_count

    def get_time_card_fallback_user(self, item):
        """Get fallback user for TimeCard - priority: freelancer > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_expense_cards(self, dry_run):
        """Process ExpenseCard model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = ExpenseCard.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_expense_card_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for ExpenseCard ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for ExpenseCard ID {item.id}")

        return updated_count

    def get_expense_card_fallback_user(self, item):
        """Get fallback user for ExpenseCard - priority: freelancer > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_invoices(self, dry_run):
        """Process Invoice model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = Invoice.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_invoice_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for Invoice ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for Invoice ID {item.id}")

        return updated_count

    def get_invoice_fallback_user(self, item):
        """Get fallback user for Invoice - priority: freelancer > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.project:
            if item.project.user:
                return item.project.user
            if item.project.company_profile and item.project.company_profile.admin_user:
                return item.project.company_profile.admin_user
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_opportunities(self, dry_run):
        """Process Opportunity model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = Opportunity.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_opportunity_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for Opportunity ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for Opportunity ID {item.id}")

        return updated_count

    def get_opportunity_fallback_user(self, item):
        """Get fallback user for Opportunity - priority: freelancer > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None

    def process_calendar_events(self, dry_run):
        """Process CalendarEvent model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = CalendarEvent.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_calendar_event_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for CalendarEvent ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for CalendarEvent ID {item.id}")

        return updated_count

    def get_calendar_event_fallback_user(self, item):
        """Get fallback user for CalendarEvent - priority: freelancer > company admin"""
        if item.freelancer:
            return item.freelancer
        
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        if item.user:
            return item.user

        return None

    def process_staff_observations(self, dry_run):
        """Process StaffObservation model with specific fallback logic"""
        updated_count = 0
        
        # Handle created_by field
        empty_created_by = StaffObservation.objects.filter(created_by__isnull=True)
        for item in empty_created_by:
            fallback_user = self.get_staff_observation_fallback_user(item)
            if fallback_user:
                if not dry_run:
                    item.created_by = fallback_user
                    item.save()
                updated_count += 1
                self.stdout.write(f"  - Set created_by to {fallback_user.email} for StaffObservation ID {item.id}")
            else:
                self.stdout.write(f"  - Could not determine fallback user for StaffObservation ID {item.id}")

        return updated_count

    def get_staff_observation_fallback_user(self, item):
        """Get fallback user for StaffObservation - priority: company admin"""
        if item.company_profile and item.company_profile.admin_user:
            return item.company_profile.admin_user
        
        return None
