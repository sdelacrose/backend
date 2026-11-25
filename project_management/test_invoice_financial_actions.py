from datetime import date, timedelta
from unittest.mock import patch
from django.utils import timezone
from project_management.models import Invoice, Correspondence, CorrespondenceSendEvent
from project_management.test_correspondence_template import TemplateBase
from project_management.views.billing import InvoiceViewSet


class InvoiceFinancialActionsTests(TemplateBase):
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        self.invoice = self.make_invoice('overdue', 3600, self.today - timedelta(days=5), 'INV-REMINDER-TEST')
        self.template = self.make(name='Invoice reminder', subject_template='Relance {{invoice_reference}}', body_template='Merci de régler {{invoice_amount}} avant le {{invoice_due_date}}. HT : {{invoice_amount_excl_vat}}.')
        for target in ('has_permission','has_object_permission'):
            patcher = patch('project_management.permissions.InvoicePermission.' + target, return_value=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(InvoiceViewSet, 'get_queryset', return_value=Invoice.objects.filter(company_profile=self.company))
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_invoice(self, status, gross, due, reference, invoice_date=None, company=None):
        return Invoice.objects.bulk_create([Invoice(company_profile=company or self.company, created_by=self.owner,
            reciepient_company=self.client_company, reciepient_user=self.recipient_contact,
            invoice_date=invoice_date or self.today, due_date=due, status=status, grand_total=gross, vat_rate=20,
            reference_number=reference, document_type='invoice')])[0]

    def post(self, action, **payload):
        return self.api(self.owner).post(f'/api/project_management/billing-invoice/{self.invoice.pk}/{action}/', {'template_id': self.template.pk, **payload}, format='json')

    def test_summary_and_card_filters_share_the_same_year_scope_and_categories(self):
        self.make_invoice('paid',1200,self.today-timedelta(days=30),'PAID')
        self.make_invoice('issued',2400,self.today+timedelta(days=10),'CURRENT')
        self.make_invoice('draft',12000,self.today+timedelta(days=10),'DRAFT')
        self.make_invoice('paid',12000,date(self.today.year-1,12,1),'OLD',invoice_date=date(self.today.year-1,11,1))
        response = self.api(self.owner).get('/api/project_management/billing-invoice/summary/')
        self.assertEqual(response.status_code,200,response.data)
        self.assertEqual(response.data['invoice_count'],3)
        self.assertEqual(response.data['invoiced_amount_excl_vat'],6000)
        self.assertEqual(response.data['paid_amount_excl_vat'],1000)
        self.assertEqual(response.data['current_amount_excl_vat'],2000)
        self.assertEqual(response.data['overdue_amount_excl_vat'],3000)
        for bucket,count in [('invoiced',3),('paid',1),('current',1),('overdue',1)]:
            response = self.api(self.owner).get('/api/project_management/billing-invoice/', {'billing_bucket':bucket,'invoice_date_after':f'{self.today.year}-01-01','invoice_date_before':f'{self.today.year}-12-31'})
            self.assertEqual(response.status_code,200,response.data)
            self.assertEqual(response.data['count'],count,bucket)

    def test_preview_renders_invoice_fields_without_creating_or_sending(self):
        before = Correspondence.objects.count()
        events = CorrespondenceSendEvent.objects.count()
        response = self.post('reminder-preview')
        self.assertEqual(response.status_code,200,response.data)
        self.assertIn('INV-REMINDER-TEST',response.data['subject'])
        self.assertIn('3000.00 € HT',response.data['body'])
        self.assertFalse(response.data['has_unresolved'])
        self.assertEqual(Correspondence.objects.count(),before)
        self.assertEqual(CorrespondenceSendEvent.objects.count(),events)

    @patch('project_management.services.correspondence_send.send_correspondence')
    def test_prepare_uses_the_real_customer_and_creates_an_editable_draft_only(self, send):
        response = self.post('prepare-reminder',subject='Payment reminder',body='Please pay invoice INV-REMINDER-TEST.')
        self.assertEqual(response.status_code,201,response.data)
        correspondence = Correspondence.objects.get(pk=response.data['correspondence_id'])
        self.assertEqual(correspondence.status,'DRAFT')
        self.assertEqual(correspondence.direction,'OUT')
        self.assertEqual(correspondence.recipient_company_id,self.client_company.id)
        self.assertEqual(correspondence.recipient_contact_id,self.recipient_contact.id)
        self.assertEqual(correspondence.company_profile_id,self.company.id)
        self.assertIn('INV-REMINDER-TEST',correspondence.message)
        send.assert_not_called()

    def test_foreign_template_and_foreign_invoice_remain_inaccessible(self):
        foreign = self.make(company=self.foreign.company,name='Foreign reminder')
        self.assertEqual(self.post('reminder-preview',template_id=foreign.pk).status_code,404)
        invoice = self.make_invoice('overdue',120,self.today-timedelta(days=1),'FOREIGN',company=self.foreign.company)
        self.assertIn(self.api(self.owner).post(f'/api/project_management/billing-invoice/{invoice.pk}/reminder-preview/',{'template_id':self.template.pk},format='json').status_code,(403,404))

    def test_invoice_view_does_not_grant_the_right_to_create_correspondences(self):
        with patch('project_management.permissions.CorrespondencePermission._check_action_or_full_access',return_value=False):
            self.assertEqual(self.post('reminder-preview').status_code,403)

    def test_paid_current_and_unresolved_reminders_are_refused(self):
        self.assertEqual(self.post('prepare-reminder',body='Please pay {{unknown}}').status_code,400)
        self.assertEqual(self.post('reminder-preview',template_id='invalid').status_code,400)
        Invoice.objects.filter(pk=self.invoice.pk).update(status='paid')
        self.assertEqual(self.post('reminder-preview').status_code,400)
        Invoice.objects.filter(pk=self.invoice.pk).update(status='issued',due_date=self.today)
        self.assertEqual(self.post('prepare-reminder').status_code,400)
