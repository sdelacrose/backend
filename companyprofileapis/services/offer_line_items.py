from decimal import Decimal, ROUND_HALF_UP


MONEY_QUANTUM = Decimal("0.01")
PERCENT_DIVISOR = Decimal("100")


def quantize_money(value):
    return Decimal(value).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def calculate_line_amounts(rate, quantity, vat_rate):
    subtotal = quantize_money(Decimal(rate) * Decimal(quantity))
    vat_amount = quantize_money(
        subtotal * Decimal(vat_rate) / PERCENT_DIVISOR
    )
    return subtotal, vat_amount, subtotal + vat_amount


def calculate_offer_totals(line_items):
    subtotal = Decimal("0.00")
    vat_amount = Decimal("0.00")
    total = Decimal("0.00")

    for item in line_items:
        item_subtotal, item_vat, item_total = calculate_line_amounts(
            item["rate"],
            item["quantity"],
            item["vat_rate"],
        )
        subtotal += item_subtotal
        vat_amount += item_vat
        total += item_total

    return (
        quantize_money(subtotal),
        quantize_money(vat_amount),
        quantize_money(total),
    )
