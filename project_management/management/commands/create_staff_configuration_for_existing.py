from django.core.management.base import BaseCommand
from models.models import StaffConfiguration, CompanyProfile, User

class Command(BaseCommand):
    help = "Creates StaffConfiguration for existing companies and freelancers"

    def handle(self, *args, **kwargs):
        # Default values
        default_values = {
            "number_of_hours": 40,
            "number_of_leaves": 12,
            "number_of_wfh_leaves": 12,
            "hourly_rate": 70,
            "base_salary": 65000
        }

        # Create StaffConfiguration for Companies
        companies = CompanyProfile.objects.filter(staff_configuration__isnull=True)
        for company in companies:
            StaffConfiguration.objects.create(company_profile=company, **default_values)
            self.stdout.write(self.style.SUCCESS(f"Created StaffConfiguration for Company: {company.company_name}"))

        # Create StaffConfiguration for Freelancers
        freelancers = User.objects.filter(user_type="freelancer", staff_configuration__isnull=True)
        for freelancer in freelancers:
            StaffConfiguration.objects.create(freelancer=freelancer, **default_values)
            self.stdout.write(self.style.SUCCESS(f"Created StaffConfiguration for Freelancer: {freelancer.full_name}"))

        self.stdout.write(self.style.SUCCESS("StaffConfiguration creation completed!"))
