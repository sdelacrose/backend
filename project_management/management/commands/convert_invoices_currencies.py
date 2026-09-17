from django.core.management.base import BaseCommand
from project_management.models import Invoice
from project_management.utils import (
    get_currency_from_record,
    symbol_to_currency_code,
    ECBCurrencyConverter
)
import logging

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Convert and update company_currency_amount and conversion_rate for existing Invoices'

    def handle(self, *args, **kwargs):
        updated_count = 0
        failed_count = 0
        converter = ECBCurrencyConverter()

        invoices = Invoice.objects.all()
        invoices_to_update = []

        for invoice in invoices:
            try:
                if invoice: 
                    amount = invoice.grand_total or 0
                    if amount <= 0:
                        invoice.company_currency_amount = 0.0
                        invoice.conversion_rate = 1.0
                        invoices_to_update.append(invoice)
                        updated_count += 1
                        continue

                    if invoice.project is None:
                        invoice.company_currency_amount = round(amount, 2)
                        invoice.conversion_rate = 1.0
                        invoices_to_update.append(invoice)
                        updated_count += 1
                        continue
                    
                        
                    # SAFELY determine company currency
                    company_currency_symbol = '€'  # Default
                    if invoice.company_profile and invoice.company_profile.currency:
                        company_currency_symbol = invoice.company_profile.currency
                    elif invoice.freelancer and hasattr(invoice.freelancer, 'freelancer_profile'):
                        freelancer_profile = getattr(invoice.freelancer, 'freelancer_profile', None)
                        if freelancer_profile and freelancer_profile.currency:
                            company_currency_symbol = freelancer_profile.currency
                    elif invoice.project and getattr(invoice.project, 'company_profile', None):
                        cp = invoice.project.company_profile
                        if cp and cp.currency:
                            company_currency_symbol = cp.currency


                    company_currency_code = symbol_to_currency_code(company_currency_symbol)
                    
                    project_currency_symbol = invoice.project.currency or "€"
                    project_currency_code = symbol_to_currency_code(project_currency_symbol)

                    if project_currency_code.upper() == company_currency_code.upper():
                        invoice.company_currency_amount = round(amount, 2)
                        invoice.conversion_rate = 1.0
                    else:
                        converted_amount = converter.convert_currency(
                            amount, project_currency_code, company_currency_code
                        )
                        rate = converter.get_conversion_rate(project_currency_code, company_currency_code)

                        invoice.company_currency_amount = round(converted_amount, 2) if converted_amount else None
                        invoice.conversion_rate = rate if rate else None
                    
                    invoices_to_update.append(invoice)
                    updated_count += 1

            except Exception as e:
                failed_count += 1
                logger.error(f"Failed to update Invoice ID {invoice.id}: {e}")
                self.stderr.write(self.style.ERROR(f"Failed Invoice ID {invoice.id}: {e}"))

        if invoices_to_update:
            Invoice.objects.bulk_update(
                invoices_to_update, 
                ['company_currency_amount', 'conversion_rate'],
                batch_size=100
            )

        self.stdout.write(self.style.SUCCESS(
            f"Conversion completed: {updated_count} updated, {failed_count} failed."
        ))