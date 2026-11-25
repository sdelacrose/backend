"""Shared net invoice amounts for PO summaries and linked invoices."""
from decimal import Decimal, ROUND_HALF_UP


def money(value):
    return Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def get_invoice_total_excl_vat(invoice):
    lines = list(invoice.line_items.all())
    if lines:
        # Use actual line amounts; a global VAT rate cannot represent mixed rates.
        return money(sum((Decimal(str(line.price or 0)) * Decimal(str(line.quantity or 0)) for line in lines), Decimal('0')))
    # Legacy invoices can have only a gross amount and a VAT rate.
    divisor = Decimal('1') + Decimal(str(invoice.vat_rate or 0)) / Decimal('100')
    return money(Decimal(str(invoice.grand_total or 0)) / divisor)
