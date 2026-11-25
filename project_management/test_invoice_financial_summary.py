from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from django.test import SimpleTestCase
from project_management.invoice_financial_summary import invoice_financial_summary


class InvoiceFinancialSummaryTests(SimpleTestCase):
    today = date(2026, 11, 24)

    def invoice(self, gross, status='issued', due=date(2026, 12, 1), kind='invoice', lines=None):
        return SimpleNamespace(grand_total=gross, vat_rate=20, document_type=kind, status=status, due_date=due,
            line_items=SimpleNamespace(all=lambda: [SimpleNamespace(price=p, quantity=q) for p, q in (lines or [])]))

    def summarize(self, rows, convert=lambda inv, net: net):
        return invoice_financial_summary(rows, self.today, convert)

    def test_net_amounts_and_invoice_count_match_disjoint_categories(self):
        result = self.summarize([self.invoice(360,'paid'), self.invoice(240), self.invoice(120,'open',date(2026,11,19))])
        self.assertEqual(result, {'invoice_count':3,'summary_complete':True,'invoiced_amount_excl_vat':600.0,'paid_amount_excl_vat':300.0,'current_amount_excl_vat':200.0,'overdue_amount_excl_vat':100.0})

    def test_due_today_is_current_and_stale_overdue_status_uses_real_due_date(self):
        result = self.summarize([self.invoice(120,'overdue',self.today),self.invoice(240,'issued',date(2026,11,23)),self.invoice(360,'paid',date(2026,1,1))])
        self.assertEqual(result['current_amount_excl_vat'],100)
        self.assertEqual(result['overdue_amount_excl_vat'],200)
        self.assertEqual(result['paid_amount_excl_vat'],300)

    def test_drafts_cancelled_closed_and_credit_notes_are_excluded(self):
        result = self.summarize([self.invoice(120,'draft'),self.invoice(120,'cancelled'),self.invoice(120,'closed'),self.invoice(-120,'issued',kind='credit_note'),self.invoice(120,'issued')])
        self.assertEqual(result['invoice_count'],1)
        self.assertEqual(result['invoiced_amount_excl_vat'],100)

    def test_actual_mixed_vat_line_items_are_used(self):
        result = self.summarize([self.invoice(230,lines=[(100,1),(50,2)])])
        self.assertEqual(result['invoiced_amount_excl_vat'],200)

    def test_missing_conversion_or_amount_never_returns_a_partial_total(self):
        for result in [self.summarize([self.invoice(120)],lambda inv,net:None),self.summarize([self.invoice(None)])]:
            self.assertFalse(result['summary_complete'])
            self.assertIsNone(result['invoiced_amount_excl_vat'])
            self.assertEqual(result['invoice_count'],1)

    def test_converted_totals_and_zero_invoices(self):
        self.assertEqual(self.summarize([self.invoice(120)],lambda inv,net:net*Decimal('1.5'))['invoiced_amount_excl_vat'],150)
        self.assertEqual(self.summarize([])['invoice_count'],0)
        self.assertEqual(self.summarize([])['invoiced_amount_excl_vat'],0)


class InvoiceFinancialRouteAuthTests(SimpleTestCase):
    def test_anonymous_financial_routes_are_rejected_before_reading_data(self):
        from rest_framework.test import APIRequestFactory
        from project_management.views.billing import InvoiceViewSet
        factory = APIRequestFactory()
        for action,method in [('summary','get'),('reminder_preview','post'),('prepare_reminder','post')]:
            request = getattr(factory,method)('/api/project_management/billing-invoice/',{},format='json')
            response = InvoiceViewSet.as_view({method:action})(request,**({'pk':1} if method=='post' else {}))
            self.assertIn(response.status_code,(401,403))

    def test_preparing_a_reminder_checks_invoice_view_without_granting_invoice_create(self):
        from unittest.mock import patch
        from project_management.permissions import InvoicePermission
        permission = InvoicePermission('view','company')
        request = SimpleNamespace(method='POST',user=SimpleNamespace())
        with patch.object(permission,'_check_view_permission',return_value=True), patch.object(permission,'_check_create_permission',return_value=False) as create:
            for action in ('reminder_preview','prepare_reminder'):
                view=SimpleNamespace(action=action)
                self.assertTrue(permission.has_permission(request,view))
                self.assertTrue(permission.has_object_permission(request,view,SimpleNamespace()))
            create.assert_not_called()
            self.assertFalse(permission.has_permission(request,SimpleNamespace(action='create')))
