from django.core.management.base import BaseCommand
from django.db import transaction

from project_management.models import SiteReport
from project_management.utils import create_pdf_invoice_by_site_report

class Command(BaseCommand):
    help = 'Create root and default subfolders for all existing projects'

    def handle(self, *args, **kwargs):
        site_reports = SiteReport.objects.all()
        for site_report in site_reports:
            create_pdf_invoice_by_site_report(site_report)
        
        print(self.style.SUCCESS(f"PDF reports having been created for {len(site_reports)} site reports."))
