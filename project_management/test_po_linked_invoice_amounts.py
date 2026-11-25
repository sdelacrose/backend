from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock
from django.test import SimpleTestCase
from project_management.serializers.purchase_order import PurchaseOrderSerializer


class LinkedInvoiceAmountTests(SimpleTestCase):
    def serialize_invoice(self, gross, lines, document_type='invoice', due_date=None, currency=None):
        line_manager = MagicMock()
        line_manager.all.return_value = [SimpleNamespace(price=price, quantity=quantity) for price, quantity in lines]
        invoice = SimpleNamespace(id=1, reference_number='DEMO', document_type=document_type, status='paid', grand_total=gross, invoice_date=None, vat_rate=20, line_items=line_manager, due_date=due_date, project=SimpleNamespace(currency=currency) if currency else None)
        manager = MagicMock()
        manager.filter.return_value.order_by.return_value.select_related.return_value.prefetch_related.return_value = [invoice]
        return PurchaseOrderSerializer().get_linked_invoices(SimpleNamespace(pk=1, invoices=manager))[0]

    def test_net_and_vat_match_gross_for_mixed_vat_lines(self):
        row = self.serialize_invoice(230, [(100, 1), (50, 2)])
        self.assertEqual(row['amount_excl_vat'], 200)
        self.assertEqual(row['vat_amount'], 30)
        self.assertEqual(row['grand_total'], 230)
        self.assertIn('due_date', row)
        self.assertEqual(row['currency'], '€')

    def test_credit_note_keeps_negative_amounts(self):
        row = self.serialize_invoice(-120, [(-100, 1)], 'credit_note')
        self.assertEqual(row['amount_excl_vat'], -100)
        self.assertEqual(row['vat_amount'], -20)
        self.assertEqual(row['grand_total'], -120)

    def test_missing_total_remains_unknown(self):
        row = self.serialize_invoice(None, [])
        self.assertIsNone(row['amount_excl_vat'])
        self.assertIsNone(row['vat_amount'])
        self.assertIsNone(row['grand_total'])

    def test_real_due_date_and_currency_are_exposed_for_progress(self):
        row = self.serialize_invoice(120, [(100, 1)], due_date=date(2026, 12, 15), currency='USD')
        self.assertEqual(row['due_date'], '2026-12-15')
        self.assertEqual(row['currency'], 'USD')
