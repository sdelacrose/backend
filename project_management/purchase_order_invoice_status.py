from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

from project_management.models import Invoice, PurchaseOrder

CLOSED_PO_STATUS = "Closed"
FULLY_INVOICED_STATUS = "Fully Invoiced"
PARTIALLY_INVOICED_STATUS = "Partially Invoiced"
INCOMING_PENDING_INVOICE_STATUS = "Pending Invoice"
OUTGOING_PENDING_INVOICE_STATUS = "Pending Invoice Receipt"

# Only invoices count toward PO billed amount / remaining capacity.
LINKED_BILLING_DOCUMENT_TYPES = (Invoice.DocumentType.INVOICE,)


def _quantize_currency(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_linked_invoice_total(purchase_order: PurchaseOrder) -> Decimal:
    """Total of invoices linked to the PO (credit notes are excluded)."""
    total = purchase_order.invoices.filter(
        document_type__in=LINKED_BILLING_DOCUMENT_TYPES,
    ).aggregate(total=Sum("grand_total"))["total"]
    return _quantize_currency(total)


def resolve_purchase_order_status_from_linked_invoices(
    purchase_order: PurchaseOrder,
) -> str:
    from project_management.serializers.purchase_order import get_po_revised_amount_incl_vat

    po_total = _quantize_currency(get_po_revised_amount_incl_vat(purchase_order))
    linked_total = get_linked_invoice_total(purchase_order)

    if purchase_order.po_type == PurchaseOrder.POType.OUTGOING:
        pending_status = OUTGOING_PENDING_INVOICE_STATUS
    else:
        pending_status = INCOMING_PENDING_INVOICE_STATUS

    if linked_total <= 0:
        return pending_status
    if linked_total < po_total:
        return PARTIALLY_INVOICED_STATUS
    return FULLY_INVOICED_STATUS


def sync_purchase_order_status_from_linked_invoices(purchase_order_or_id) -> bool:
    """
    Update PO invoicing status from linked invoice totals.
    Does not set Fully Paid or change Closed POs.
    Returns True when status was changed.
    """
    if purchase_order_or_id is None:
        return False

    if isinstance(purchase_order_or_id, PurchaseOrder):
        purchase_order = purchase_order_or_id
    else:
        purchase_order = PurchaseOrder.objects.filter(pk=purchase_order_or_id).first()

    if not purchase_order:
        return False

    if (purchase_order.status or "").strip() == CLOSED_PO_STATUS:
        return False

    new_status = resolve_purchase_order_status_from_linked_invoices(purchase_order)
    if purchase_order.status == new_status:
        return False

    purchase_order.status = new_status
    purchase_order.save(update_fields=["status", "updated_at"])
    return True
