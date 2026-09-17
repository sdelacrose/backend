from django.core.management.base import BaseCommand

from project_management.models import PurchaseOrder
from project_management.purchase_order_invoice_status import (
    LINKED_BILLING_DOCUMENT_TYPES,
    sync_purchase_order_status_from_linked_invoices,
)


class Command(BaseCommand):
    help = (
        "Backfill PO invoicing statuses for purchase orders with linked invoices."
    )

    def handle(self, *args, **options):
        purchase_orders = (
            PurchaseOrder.objects.filter(
                invoices__document_type__in=LINKED_BILLING_DOCUMENT_TYPES,
            )
            .distinct()
            .order_by("id")
        )

        total = purchase_orders.count()
        updated = 0

        self.stdout.write(
            f"Found {total} purchase order(s) with linked invoices to backfill."
        )

        for purchase_order in purchase_orders.iterator():
            if sync_purchase_order_status_from_linked_invoices(purchase_order):
                updated += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'Updated PO #{purchase_order.reference or purchase_order.id} '
                        f'to "{purchase_order.status}"'
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfill complete. Updated {updated} of {total} purchase order(s)."
            )
        )
