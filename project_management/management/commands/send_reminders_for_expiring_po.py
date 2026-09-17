from datetime import timedelta
from django.utils import timezone
from django.core.management.base import BaseCommand
from django.db.models import Q
from project_management.models import PurchaseOrder
from project_management.utils import send_reminder_emails_for_expiring_po

class Command(BaseCommand):
    help = 'Send reminders for expiring purchase orders'

    def handle(self, *args, **kwargs):
        # Get all purchase orders with end_date in the next 30 days
        expiring_po = PurchaseOrder.objects.filter(
            Q(end_date__gte=timezone.now()) &
            Q(end_date__lte=timezone.now() + timedelta(days=30)) &
            ~Q(status__in=["closed", "cancelled"])
        )
        self.stdout.write(f"Found {expiring_po.count()} expiring purchase orders")
        for po in expiring_po:
            po_duration = po.end_date - po.start_date
            if po_duration.days >= 30:
                reminder_date = po.end_date - timedelta(days=30)
            elif po_duration.days >= 7:
                reminder_date = po.end_date - timedelta(days=7)
            else:
                reminder_date = po.end_date - timedelta(days=2)
            if reminder_date == timezone.now():
                send_reminder_emails_for_expiring_po(po, po.created_by)
                self.stdout.write(f"Reminder for PO {po.po_number} sent on {reminder_date}")
            else:
                self.stdout.write(f"Reminder for PO {po.po_number} will be sent on {reminder_date}")
            