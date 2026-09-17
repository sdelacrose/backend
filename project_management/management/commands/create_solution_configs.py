from django.core.management.base import BaseCommand
from models.models import CompanyProfile, CompanyConfig, User
from models import constants
from ecpmpapp.constants import UserType

class Command(BaseCommand):
    help = "Create Core and Enterprise solution configurations for existing companies and freelancers"

    def create_company_configs(self):
        companies = CompanyProfile.objects.all()
        created_count = {'Core': 0, 'Enterprise': 0}
        skipped_count = {'Core': 0, 'Enterprise': 0}

        for company in companies:
            # Create Core Solution Config
            core_config, created = CompanyConfig.objects.get_or_create(
                company_profile=company,
                user=None,
                solution='Core Solution',
                defaults={
                    'status_options': constants.CORE_STATUS_OPTIONS,
                    'contract_type': constants.CORE_CONTRACT_TYPES,
                    'international_standard': constants.GC_INTERNATIONAL_STANDARDS,
                    'major_project_model': constants.GC_MAJOR_PROJECT_MODELS,
                    'commercial_model': constants.GC_COMMERCIAL_MODELS,
                    'supporting_agreement': constants.GC_SUPPORTING_AGREEMENTS,
                    'reminder_settings': constants.CORE_REMINDER_SETTINGS,
                    'invoice_status': constants.CORE_INVOICE_STATUS,
                    'expense_types': constants.CORE_EXPENSE_TYPES,
                    'document_types': constants.CORE_DOCUMENT_TYPES,
                    'document_status': constants.CORE_DOCUMENT_STATUSES,
                    'document_internal_review_status': constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                    'document_external_review_status': constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                    'transmittal_category': constants.GC_TRANSMITTAL_CATEGORIES,
                    'priority_levels': constants.GC_PRIORITY_LEVELS,
                    'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
                    'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
                    'leave_types': constants.GC_LEAVE_TYPES,
                    'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
                }
            )

            if created:
                created_count['Core'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created Core Solution config for company: {company.company_name}"
                    )
                )
            else:
                skipped_count['Core'] += 1

            # Create Enterprise Solution Config
            enterprise_config, created = CompanyConfig.objects.get_or_create(
                company_profile=company,
                user=None,
                solution='Enterprise Solution',
                defaults={
                    'status_options': constants.ENTERPRISE_STATUS_OPTIONS,
                    'contract_type': constants.ENTERPRISE_CONTRACT_TYPES,
                    'international_standard': constants.GC_INTERNATIONAL_STANDARDS,
                    'major_project_model': constants.GC_MAJOR_PROJECT_MODELS,
                    'commercial_model': constants.GC_COMMERCIAL_MODELS,
                    'supporting_agreement': constants.GC_SUPPORTING_AGREEMENTS,
                    'team_members_roles': constants.ENTERPRISE_TEAM_MEMBERS_ROLES,
                    'reminder_settings': constants.ENTERPRISE_REMINDER_SETTINGS,
                    'invoice_status': constants.ENTERPRISE_INVOICE_STATUS,
                    'expense_types': constants.ENTERPRISE_EXPENSE_TYPES,
                    'project_types': constants.ENTERPRISE_PROJECT_TYPES,
                    'document_types': constants.ENTERPRISE_DOCUMENT_TYPES,
                    'document_status': constants.ENTERPRISE_DOCUMENT_STATUSES,
                    'document_internal_review_status': constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                    'document_external_review_status': constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                    'transmittal_category': constants.GC_TRANSMITTAL_CATEGORIES,
                    'priority_levels': constants.GC_PRIORITY_LEVELS,
                    'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
                    'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
                    'leave_types': constants.GC_LEAVE_TYPES,
                    'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
                }
            )

            if created:
                created_count['Enterprise'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created Enterprise Solution config for company: {company.company_name}"
                    )
                )
            else:
                skipped_count['Enterprise'] += 1

        return created_count, skipped_count

    def create_freelancer_configs(self):
        freelancers = User.objects.filter(user_type=UserType.FREELANCER.value)
        created_count = {'Core': 0, 'Enterprise': 0}
        skipped_count = {'Core': 0, 'Enterprise': 0}

        for freelancer in freelancers:
            # Create Core Solution Config
            core_config, created = CompanyConfig.objects.get_or_create(
                user=freelancer,
                company_profile=None,
                solution='Core Solution',
                defaults={
                    'status_options': constants.CORE_STATUS_OPTIONS,
                    'contract_type': constants.CORE_CONTRACT_TYPES,
                    'international_standard': constants.GC_INTERNATIONAL_STANDARDS,
                    'major_project_model': constants.GC_MAJOR_PROJECT_MODELS,
                    'commercial_model': constants.GC_COMMERCIAL_MODELS,
                    'supporting_agreement': constants.GC_SUPPORTING_AGREEMENTS,
                    'reminder_settings': constants.CORE_REMINDER_SETTINGS,
                    'invoice_status': constants.CORE_INVOICE_STATUS,
                    'expense_types': constants.CORE_EXPENSE_TYPES,
                    'document_types': constants.CORE_DOCUMENT_TYPES,
                    'document_status': constants.CORE_DOCUMENT_STATUSES,
                    'document_internal_review_status': constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                    'document_external_review_status': constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                    'transmittal_category': constants.GC_TRANSMITTAL_CATEGORIES,
                    'priority_levels': constants.GC_PRIORITY_LEVELS,
                    'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
                    'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
                    'leave_types': constants.GC_LEAVE_TYPES,
                    'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
                }
            )

            if created:
                created_count['Core'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created Core Solution config for freelancer: {freelancer.email}"
                    )
                )
            else:
                skipped_count['Core'] += 1

            # Create Enterprise Solution Config
            enterprise_config, created = CompanyConfig.objects.get_or_create(
                user=freelancer,
                company_profile=None,
                solution='Enterprise Solution',
                defaults={
                    'status_options': constants.ENTERPRISE_STATUS_OPTIONS,
                    'contract_type': constants.ENTERPRISE_CONTRACT_TYPES,
                    'international_standard': constants.GC_INTERNATIONAL_STANDARDS,
                    'major_project_model': constants.GC_MAJOR_PROJECT_MODELS,
                    'commercial_model': constants.GC_COMMERCIAL_MODELS,
                    'supporting_agreement': constants.GC_SUPPORTING_AGREEMENTS,
                    'team_members_roles': constants.ENTERPRISE_TEAM_MEMBERS_ROLES,
                    'reminder_settings': constants.ENTERPRISE_REMINDER_SETTINGS,
                    'invoice_status': constants.ENTERPRISE_INVOICE_STATUS,
                    'expense_types': constants.ENTERPRISE_EXPENSE_TYPES,
                    'project_types': constants.ENTERPRISE_PROJECT_TYPES,
                    'document_types': constants.ENTERPRISE_DOCUMENT_TYPES,
                    'document_status': constants.ENTERPRISE_DOCUMENT_STATUSES,
                    'document_internal_review_status': constants.GC_DOCUMENT_INTERNAL_REVIEW_STATUSES,
                    'document_external_review_status': constants.GC_DOCUMENT_EXTERNAL_REVIEW_STATUSES,
                    'transmittal_category': constants.GC_TRANSMITTAL_CATEGORIES,
                    'priority_levels': constants.GC_PRIORITY_LEVELS,
                    'project_priority_levels': constants.GC_PROJECT_PRIORITY_LEVELS,
                    'calendar_event_status_options': constants.GC_CALENDAR_EVENT_STATUS_OPTIONS,
                    'leave_types': constants.GC_LEAVE_TYPES,
                    'opportunity_sales_phase': constants.GC_OPPORTUNITY_SALES_PHASE,
                }
            )

            if created:
                created_count['Enterprise'] += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created Enterprise Solution config for freelancer: {freelancer.email}"
                    )
                )
            else:
                skipped_count['Enterprise'] += 1

        return created_count, skipped_count

    def handle(self, *args, **kwargs):
        # Handle company configurations
        company_created, company_skipped = self.create_company_configs()
        
        # Handle freelancer configurations
        freelancer_created, freelancer_skipped = self.create_freelancer_configs()

        # Print summary
        self.stdout.write(
            self.style.SUCCESS(
                f"\nProcess completed:\n"
                f"Companies:\n"
                f"  Core Solution: {company_created['Core']} created, {company_skipped['Core']} skipped\n"
                f"  Enterprise Solution: {company_created['Enterprise']} created, {company_skipped['Enterprise']} skipped\n"
                f"Freelancers:\n"
                f"  Core Solution: {freelancer_created['Core']} created, {freelancer_skipped['Core']} skipped\n"
                f"  Enterprise Solution: {freelancer_created['Enterprise']} created, {freelancer_skipped['Enterprise']} skipped"
            )
        )
