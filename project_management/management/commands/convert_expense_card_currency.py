from django.core.management.base import BaseCommand
from project_management.models import ExpenseCard
from project_management.utils import symbol_to_currency_code, get_currency_from_record, ECBCurrencyConverter


class Command(BaseCommand):
    help = 'Convert currency for existing ExpenseCards that have no converted_amount'

    def handle(self, *args, **kwargs):
        converter = ECBCurrencyConverter()
        updated_count = 0
        failed_count = 0

        expense_cards = ExpenseCard.objects.filter()
        cards_to_update = []

        for card in expense_cards:
            try:
                # SAFELY determine company currency
                company_currency_symbol = '€'  # Default
                if card.company_profile and card.company_profile.currency:
                    company_currency_symbol = card.company_profile.currency
                elif card.freelancer and hasattr(card.freelancer, 'freelancer_profile'):
                    freelancer_profile = getattr(card.freelancer, 'freelancer_profile', None)
                    if freelancer_profile and freelancer_profile.currency:
                        company_currency_symbol = freelancer_profile.currency
                elif card.project and getattr(card.project, 'company_profile', None):
                    cp = card.project.company_profile
                    if cp and cp.currency:
                        company_currency_symbol = cp.currency

                company_currency_code = symbol_to_currency_code(company_currency_symbol)

                # Determine expense currency (from record)
                expense_currency_symbol = get_currency_from_record(card)
                expense_currency_code = symbol_to_currency_code(expense_currency_symbol)

                card.currency = expense_currency_code
                card.converted_currency = company_currency_code

                if expense_currency_code.upper() == company_currency_code.upper():
                    card.converted_amount = card.total
                else:
                    converted = converter.convert_currency(card.total, expense_currency_code, company_currency_code)
                    card.converted_amount = converted if converted is not None else 0

                cards_to_update.append(card)
                updated_count += 1
                self.stdout.write(self.style.SUCCESS(f"Prepared ExpenseCard ID {card.id} for update"))

            except Exception as e:
                failed_count += 1
                self.stderr.write(self.style.ERROR(f"Failed to prepare ExpenseCard ID {card.id}: {e}"))

        if cards_to_update:
            ExpenseCard.objects.bulk_update(
                cards_to_update,
                ['total', 'currency', 'converted_currency', 'converted_amount'],
                batch_size=100
            )

        self.stdout.write(self.style.SUCCESS(
            f"Bulk update completed: {updated_count} updated, {failed_count} failed."
        ))