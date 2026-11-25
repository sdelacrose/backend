"""Net amounts for the purchase-order summary cards."""
from project_management.invoice_amounts import get_invoice_total_excl_vat, money

from project_management.serializers.purchase_order import get_change_notice_delta_excl_vat


def get_purchase_order_total_excl_vat(order):
    delta = get_change_notice_delta_excl_vat(order)
    return money(order.amount_excl_vat) if delta is None else delta


