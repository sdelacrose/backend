from datetime import date, timedelta

from django.test import SimpleTestCase

from project_management.invoice_status import get_automatic_invoice_status
from project_management.models import Invoice


class AutomaticInvoiceStatusTests(SimpleTestCase):
    def test_due_today(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="open",
                due_date=today,
                today=today,
            ),
            "due",
        )

    def test_past_due_is_overdue(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="due",
                due_date=today - timedelta(days=1),
                today=today,
            ),
            "overdue",
        )

    def test_future_due_resets_to_open(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="due",
                due_date=today + timedelta(days=1),
                today=today,
            ),
            "open",
        )

    def test_overdue_with_future_due_resets_to_open(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="overdue",
                due_date=today + timedelta(days=2),
                today=today,
            ),
            "open",
        )

    def test_paid_unchanged(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="paid",
                due_date=today - timedelta(days=1),
                today=today,
            ),
            "paid",
        )

    def test_credit_note_unchanged(self):
        today = date(2026, 7, 23)
        self.assertEqual(
            get_automatic_invoice_status(
                status="open",
                due_date=today - timedelta(days=1),
                document_type=Invoice.DocumentType.CREDIT_NOTE,
                today=today,
            ),
            "open",
        )
