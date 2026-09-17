from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.db.models.signals import post_save
from django.test import TestCase
from django.utils import timezone

from models.models import CompanyProfile, User
from project_management.models import ContactCompany, Invoice, Project, PurchaseOrder
from project_management.purchase_order_invoice_status import (
    get_linked_invoice_total,
    sync_purchase_order_status_from_linked_invoices,
)
from project_management.signals import create_default_folders, notify_on_project_save


class PurchaseOrderInvoiceStatusSyncTests(TestCase):
    def setUp(self):
        post_save.disconnect(create_default_folders, sender=Project)
        post_save.disconnect(notify_on_project_save, sender=Project)
        self.addCleanup(post_save.connect, create_default_folders, sender=Project)
        self.addCleanup(post_save.connect, notify_on_project_save, sender=Project)

        notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)

        self.user = User.objects.create_user(
            email="po-invoice-status@example.com",
            password="pass12345",
            first_name="PO",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="PO Status Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="po-status@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.recipient = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Client Co",
            contact_email="client@example.com",
        )
        self.project = Project.objects.create(
            project_name="PO Status Project",
            contract_value=Decimal("1000"),
            company_profile=self.company,
            user=self.user,
            created_by=self.user,
        )

    def _create_po(self, *, po_type=PurchaseOrder.POType.INCOMING, amount_incl_vat=1000):
        status = "Received" if po_type == PurchaseOrder.POType.INCOMING else "Sent"
        return PurchaseOrder.objects.create(
            project=self.project,
            created_by=self.user,
            reference=f"PO-{po_type}-{amount_incl_vat}",
            po_date=timezone.now().date(),
            currency="EUR",
            status=status,
            amount_excl_vat=amount_incl_vat / 1.19,
            vat_rate=19,
            vat_amount=amount_incl_vat - (amount_incl_vat / 1.19),
            amount_incl_vat=amount_incl_vat,
            po_type=po_type,
            document_type=PurchaseOrder.DocumentType.PURCHASE_ORDER,
        )

    def _create_invoice(self, *, purchase_order, grand_total, reference):
        today = timezone.now().date()
        return Invoice.objects.bulk_create(
            [
                Invoice(
                    company_profile=self.company,
                    project=self.project,
                    purchase_order=purchase_order,
                    reciepient_company=self.recipient,
                    invoice_date=today,
                    due_date=today,
                    status="open",
                    grand_total=grand_total,
                    vat_rate=19,
                    document_type=Invoice.DocumentType.INVOICE,
                    created_by=self.user,
                    reference_number=reference,
                )
            ]
        )[0]

    def test_partially_invoiced_when_linked_total_below_po_total(self):
        po = self._create_po(amount_incl_vat=1000)
        self._create_invoice(purchase_order=po, grand_total=500, reference="INV-PO-500")

        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Partially Invoiced")

    def test_fully_invoiced_when_linked_total_matches_po_total(self):
        po = self._create_po(amount_incl_vat=1000)
        self._create_invoice(purchase_order=po, grand_total=500, reference="INV-PO-500A")
        self._create_invoice(purchase_order=po, grand_total=500, reference="INV-PO-500B")

        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Fully Invoiced")

    def test_pending_invoice_when_all_invoices_unlinked(self):
        po = self._create_po(amount_incl_vat=1000)
        invoice = self._create_invoice(
            purchase_order=po,
            grand_total=500,
            reference="INV-PO-UNLINK",
        )
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()
        self.assertEqual(po.status, "Partially Invoiced")

        invoice.purchase_order = None
        invoice.save(update_fields=["purchase_order"])
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Pending Invoice")

    def test_partially_invoiced_after_one_of_two_invoices_unlinked(self):
        po = self._create_po(amount_incl_vat=1000)
        invoice_one = self._create_invoice(
            purchase_order=po,
            grand_total=500,
            reference="INV-PO-KEEP",
        )
        invoice_two = self._create_invoice(
            purchase_order=po,
            grand_total=500,
            reference="INV-PO-REMOVE",
        )
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Invoiced")

        invoice_two.purchase_order = None
        invoice_two.save(update_fields=["purchase_order"])
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Partially Invoiced")
        self.assertIsNotNone(invoice_one.purchase_order_id)

    def test_pending_invoice_receipt_for_outgoing_po_with_no_linked_invoices(self):
        po = self._create_po(
            po_type=PurchaseOrder.POType.OUTGOING,
            amount_incl_vat=1000,
        )
        self._create_invoice(
            purchase_order=po,
            grand_total=500,
            reference="INV-PO-OUT",
        )
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()
        self.assertEqual(po.status, "Partially Invoiced")

        po.invoices.update(purchase_order=None)
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Pending Invoice Receipt")

    def test_closed_po_status_is_not_changed(self):
        po = self._create_po(amount_incl_vat=1000)
        po.status = "Closed"
        po.save(update_fields=["status"])
        self._create_invoice(purchase_order=po, grand_total=500, reference="INV-PO-CLOSED")

        changed = sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertFalse(changed)
        self.assertEqual(po.status, "Closed")

    def test_does_not_set_fully_paid(self):
        po = self._create_po(amount_incl_vat=1000)
        invoice = self._create_invoice(
            purchase_order=po,
            grand_total=1000,
            reference="INV-PO-PAID",
        )
        invoice.status = "paid"
        invoice.save(update_fields=["status"])

        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        self.assertEqual(po.status, "Fully Invoiced")

    def test_credit_note_does_not_change_po_status_from_linked_invoices(self):
        po = self._create_po(amount_incl_vat=1000)
        invoice = self._create_invoice(
            purchase_order=po,
            grand_total=1000,
            reference="INV-PO-FULL",
        )
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()
        self.assertEqual(po.status, "Fully Invoiced")

        Invoice.objects.create(
            company_profile=self.company,
            project=self.project,
            purchase_order=po,
            reciepient_company=self.recipient,
            invoice_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status="open",
            grand_total=-400,
            vat_rate=0,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            source_invoice=invoice,
            created_by=self.user,
            reference_number="CN-PO-400",
        )
        sync_purchase_order_status_from_linked_invoices(po)
        po.refresh_from_db()

        # PO status is based on invoices only; CN does not free capacity.
        self.assertEqual(po.status, "Fully Invoiced")
        self.assertEqual(get_linked_invoice_total(po), Decimal("1000.00"))

    def test_backfill_command_updates_only_pos_with_linked_invoices(self):
        po_with_invoice = self._create_po(amount_incl_vat=1000)
        po_without_invoice = self._create_po(amount_incl_vat=1000)
        self._create_invoice(
            purchase_order=po_with_invoice,
            grand_total=500,
            reference="INV-PO-BACKFILL",
        )

        call_command("backfill_purchase_order_invoice_statuses")

        po_with_invoice.refresh_from_db()
        po_without_invoice.refresh_from_db()

        self.assertEqual(po_with_invoice.status, "Partially Invoiced")
        self.assertEqual(po_without_invoice.status, "Received")
