from django.utils import timezone

from project_management.models import Invoice


def get_automatic_invoice_status(
    *,
    status,
    due_date,
    document_type=Invoice.DocumentType.INVOICE,
    today=None,
):
    """Return the date-driven status for an open, due, or overdue invoice.

    - due_date < today  → overdue
    - due_date == today → due
    - due_date > today  → open
    """
    if (
        document_type != Invoice.DocumentType.INVOICE
        or not status
        or status.lower() not in {"open", "due", "overdue"}
        or not due_date
    ):
        return status

    today = today or timezone.now().date()
    if due_date < today:
        return "overdue"
    if due_date == today:
        return "due"
    return "open"
