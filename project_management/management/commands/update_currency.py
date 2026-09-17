from django.core.management.base import BaseCommand
from project_management.models import Project, Invoice
from models.models import CompanyProfile, User, FreelancerProfile

class Command(BaseCommand):
    help = "Update currency from ₭, ₦, ₫ to € in projects, companies, and freelancers"

    def handle(self, *args, **kwargs):
        # Update Project currency
        updated_projects = Project.objects.filter(currency__in=['₭', '₦', '₫']).update(currency='€')
        self.stdout.write(self.style.SUCCESS(f"Updated currency in {updated_projects} projects."))

        # Update Company currency
        updated_companies = CompanyProfile.objects.filter(currency__in=['₭', '₦', '₫']).update(currency='€')
        self.stdout.write(self.style.SUCCESS(f"Updated currency in {updated_companies} companies."))

        # Update FreelancerProfile currency
        updated_freelancers = FreelancerProfile.objects.filter(currency__in=['₭', '₦', '₫']).update(currency='€')
        self.stdout.write(self.style.SUCCESS(f"Updated currency in {updated_freelancers} freelancer profiles."))
