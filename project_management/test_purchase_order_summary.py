from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from django.test import SimpleTestCase
from project_management.views.purchase_order import PurchaseOrderViewSet
from project_management.models import Invoice, PurchaseOrder

class PurchaseOrderSummaryTests(SimpleTestCase):
    def summarize(self, orders, invoices, *, root_count=None, converted=100):
        for invoice in invoices:
            if not hasattr(invoice, "status"):
                invoice.status = "issued"
        scoped = MagicMock()
        scoped.values_list.return_value = [row.id for row in orders]
        rows = MagicMock()
        rows.select_related.return_value = rows
        rows.__iter__.return_value = iter(orders)
        rows.filter.return_value.count.return_value = len(orders) if root_count is None else root_count
        invoice_rows = MagicMock()
        invoice_rows.select_related.return_value.prefetch_related.return_value = invoices
        view = PurchaseOrderViewSet()
        view.get_queryset = MagicMock(return_value=scoped)
        view.filter_queryset = MagicMock(return_value=scoped)
        request = SimpleNamespace(user=SimpleNamespace())
        with patch('project_management.views.purchase_order.get_purchase_order_total_excl_vat', side_effect=lambda record: getattr(record, 'amount_excl_vat', record.amount)), patch('project_management.views.purchase_order.get_invoice_total_excl_vat', side_effect=lambda record: getattr(record, 'net_total', record.grand_total)), patch('project_management.views.purchase_order.PurchaseOrder.objects.filter', return_value=rows), patch('project_management.views.purchase_order.Invoice.objects.filter', return_value=invoice_rows) as invoice_filter, patch('project_management.views.purchase_order.get_user_currency', return_value='EUR'), patch('project_management.views.purchase_order.symbol_to_currency_code', side_effect=lambda value: value), patch('project_management.views.purchase_order.get_currency_from_record', side_effect=lambda record: record.currency), patch('project_management.views.purchase_order.get_purchase_order_display_total_incl_vat', side_effect=lambda record: record.amount), patch('project_management.views.purchase_order.ECBCurrencyConverter') as converter:
            converter.return_value.convert_currency.return_value = converted
            response = view.sum(request)
        self.assertEqual(response.status_code, 200)
        view.filter_queryset.assert_called_once_with(scoped)
        invoice_filter.assert_called_once_with(purchase_order_id__in=scoped.values_list.return_value, document_type__in=(Invoice.DocumentType.INVOICE,))
        rows.filter.assert_called_once_with(document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER)
        return response.data

    def test_summary_covers_filtered_orders_and_change_notice_delta_without_counting_notices_as_orders(self):
        orders = [SimpleNamespace(id=1, amount=1000, currency='EUR'), SimpleNamespace(id=2, amount=500, currency='EUR'), SimpleNamespace(id=3, amount=200, currency='EUR')]
        invoices = [SimpleNamespace(grand_total=600, currency='EUR'), SimpleNamespace(grand_total=200, currency='EUR')]
        summary = self.summarize(orders, invoices, root_count=2)
        self.assertEqual(summary['purchase_order_count'], 2)
        self.assertEqual(summary['total_amount'], 1700)
        self.assertEqual(summary['invoiced_amount'], 800)
        self.assertEqual(summary['remaining_amount'], 900)

    def test_empty_scope_is_zero(self):
        summary = self.summarize([], [])
        self.assertEqual(summary['purchase_order_count'], 0)
        self.assertEqual(summary['remaining_amount'], 0)
        self.assertEqual(summary['paid_amount'], 0)

    def test_foreign_currency_invoices_are_converted(self):
        summary = self.summarize([SimpleNamespace(id=1, amount=1000, currency='EUR')], [SimpleNamespace(grand_total=50, currency='USD')], converted=100)
        self.assertEqual(summary['invoiced_amount'], 100)
        self.assertEqual(summary['remaining_amount'], 900)

    def test_failed_conversion_does_not_publish_a_misleading_balance(self):
        summary = self.summarize([SimpleNamespace(id=1, amount=1000, currency='EUR')], [SimpleNamespace(grand_total=50, currency='USD')], converted=None)
        self.assertFalse(summary['summary_complete'])
        self.assertIsNone(summary['invoiced_amount'])
        self.assertIsNone(summary['remaining_amount'])
        self.assertIsNone(summary['paid_amount'])
        self.assertIsNone(summary['total_amount_excl_vat'])
        self.assertIsNone(summary['paid_amount_excl_vat'])

    def test_overbilling_is_visible_as_a_negative_balance(self):
        summary = self.summarize([SimpleNamespace(id=1, amount=1000, currency='EUR')], [SimpleNamespace(grand_total=1500, currency='EUR')])
        self.assertEqual(summary['remaining_amount'], -500)

    def test_paid_total_excludes_issued_and_not_paid_invoices(self):
        summary = self.summarize(
            [SimpleNamespace(id=1, amount=1000, currency='EUR')],
            [SimpleNamespace(grand_total=200, currency='EUR', status='paid'),
             SimpleNamespace(grand_total=300, currency='EUR', status='issued'),
             SimpleNamespace(grand_total=100, currency='EUR', status='not paid')],
        )
        self.assertEqual(summary['paid_amount'], 200)
        self.assertEqual(summary['invoiced_amount'], 600)
        self.assertEqual(summary['remaining_amount'], 400)

    def test_paid_invoice_uses_converted_amount(self):
        summary = self.summarize(
            [SimpleNamespace(id=1, amount=1000, currency='EUR')],
            [SimpleNamespace(grand_total=50, currency='USD', status='paid')],
            converted=100,
        )
        self.assertEqual(summary['paid_amount'], 100)

    def test_paid_status_is_case_insensitive_and_trimmed(self):
        summary = self.summarize(
            [SimpleNamespace(id=1, amount=1000, currency='EUR')],
            [SimpleNamespace(grand_total=200, currency='EUR', status=' Paid ')],
        )
        self.assertEqual(summary['paid_amount'], 200)


    def test_net_cards_share_the_same_basis_and_gross_totals_remain_compatible(self):
        summary = self.summarize(
            [SimpleNamespace(id=1, amount=1200, amount_excl_vat=1000, currency='EUR')],
            [SimpleNamespace(grand_total=600, net_total=500, currency='EUR', status='paid')],
        )
        self.assertEqual(summary['total_amount_excl_vat'], 1000)
        self.assertEqual(summary['invoiced_amount_excl_vat'], 500)
        self.assertEqual(summary['paid_amount_excl_vat'], 500)
        self.assertEqual(summary['remaining_amount_excl_vat'], 500)
        self.assertEqual(summary['total_amount'], 1200)
        self.assertEqual(summary['remaining_amount'], 600)


class NetAmountTests(SimpleTestCase):
    def test_invoice_uses_actual_net_lines_with_mixed_vat_rates(self):
        from project_management.purchase_order_summary import get_invoice_total_excl_vat
        lines = MagicMock()
        lines.all.return_value = [SimpleNamespace(price=100, quantity=1), SimpleNamespace(price=50, quantity=2)]
        invoice = SimpleNamespace(line_items=lines, grand_total=230, vat_rate=20)
        self.assertEqual(get_invoice_total_excl_vat(invoice), 200)

    def test_legacy_invoice_without_lines_uses_its_vat_rate(self):
        from project_management.purchase_order_summary import get_invoice_total_excl_vat
        lines = MagicMock()
        lines.all.return_value = []
        invoice = SimpleNamespace(line_items=lines, grand_total=120, vat_rate=20)
        self.assertEqual(get_invoice_total_excl_vat(invoice), 100)

    def test_change_notice_adds_only_its_net_delta(self):
        from project_management.purchase_order_summary import get_purchase_order_total_excl_vat
        order = SimpleNamespace(document_type='change_notice', change_amount_excl_vat=25, amount_excl_vat=125)
        self.assertEqual(get_purchase_order_total_excl_vat(order), 25)

    def test_root_order_uses_its_net_amount(self):
        from project_management.purchase_order_summary import get_purchase_order_total_excl_vat
        order = SimpleNamespace(document_type='purchase_order', po_type='incoming', amount_excl_vat=100)
        self.assertEqual(get_purchase_order_total_excl_vat(order), 100)
