from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from models.models import CompanyProfile, User
from project_management.models import (
    ActionItem,
    ActionItemReminderDelivery,
    ContactCompany,
    Invoice,
    Milestone,
    Project,
)


class ActionItemReminderCommandTests(TestCase):
    def setUp(self):
        notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)

        self.user = User.objects.create_user(
            email="reminder@example.com",
            password="password",
            first_name="Reminder",
            last_name="Recipient",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Reminder Company",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="company@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )

    def create_action_item(self, project_status="In progress", reminder_date=None):
        reminder_date = reminder_date or timezone.now().date()
        project = Project(
            project_name="Reminder Project",
            project_description="",
            contract_value=Decimal("0"),
            project_status=project_status,
            company_profile=self.company,
            user=self.user,
        )
        # Project post-save signals depend on admin-server tables that are not
        # part of the isolated core-backend test database.
        Project.objects.bulk_create([project])
        milestone = Milestone.objects.create(
            project=project,
            name="Reminder Milestone",
            due_date=reminder_date + timedelta(days=2),
            reminder_setting="none",
        )
        action_item = ActionItem(
            milestone=milestone,
            name="Reminder Action Item",
            target_date=reminder_date + timedelta(days=2),
            reminder="2 days",
            status="open",
        )
        # Action-item notification signals also query admin-server tables.
        ActionItem.objects.bulk_create([action_item])
        ActionItem.assignees.through.objects.bulk_create([
            ActionItem.assignees.through(
                actionitem_id=action_item.id,
                user_id=self.user.id,
            )
        ])
        return action_item

    @patch(
        "project_management.management.commands.send_reminders_for_milestone.get_user_email_preferences",
        return_value=True,
    )
    @patch(
        "project_management.management.commands.send_reminders_for_milestone.send_mail",
        return_value=1,
    )
    def test_retry_does_not_send_the_same_reminder_twice(
        self,
        mock_send_mail,
        _mock_preferences,
    ):
        action_item = self.create_action_item()

        call_command("send_reminders_for_milestone")
        call_command("send_reminders_for_milestone")

        self.assertEqual(mock_send_mail.call_count, 1)
        delivery = ActionItemReminderDelivery.objects.get(action_item=action_item)
        self.assertEqual(delivery.status, ActionItemReminderDelivery.Status.SENT)
        self.assertIsNotNone(delivery.sent_at)

    @patch(
        "project_management.management.commands.send_reminders_for_milestone.get_user_email_preferences",
        return_value=True,
    )
    @patch(
        "project_management.management.commands.send_reminders_for_milestone.send_mail",
        return_value=1,
    )
    def test_closed_project_does_not_send_action_item_reminders(
        self,
        mock_send_mail,
        _mock_preferences,
    ):
        self.create_action_item(project_status="Closed")

        call_command("send_reminders_for_milestone")

        mock_send_mail.assert_not_called()
        self.assertFalse(ActionItemReminderDelivery.objects.exists())

    @patch(
        "project_management.management.commands.send_reminders_for_milestone.get_user_email_preferences",
        return_value=True,
    )
    @patch(
        "project_management.management.commands.send_reminders_for_milestone.send_mail",
        return_value=1,
    )
    def test_old_overdue_reminder_is_not_replayed(
        self,
        mock_send_mail,
        _mock_preferences,
    ):
        self.create_action_item(reminder_date=timezone.now().date() - timedelta(days=1))

        call_command("send_reminders_for_milestone")

        mock_send_mail.assert_not_called()
        self.assertFalse(ActionItemReminderDelivery.objects.exists())


class InvoiceDueOverdueStatusCommandTests(TestCase):
    def setUp(self):
        notification_patcher = patch(
            "ecpmpapp.utils.should_notify_user_comprehensive",
            return_value=False,
        )
        notification_patcher.start()
        self.addCleanup(notification_patcher.stop)

        self.user = User.objects.create_user(
            email="invoice-status@example.com",
            password="pass12345",
            first_name="Invoice",
            last_name="Owner",
            user_type="company_user",
        )
        self.company = CompanyProfile.objects.create(
            admin_user=self.user,
            company_name="Status Billing Co",
            company_address="Street 1",
            company_town="Town",
            company_tell_no="123",
            company_mob_no="456",
            company_web_address="https://example.com",
            company_email_address="status@example.com",
            company_tax_reference="tax",
            company_iban="iban",
        )
        self.user.company = self.company
        self.user.save(update_fields=["company"])
        self.recipient = ContactCompany.objects.create(
            company_profile=self.company,
            company_name="Client Co",
        )

    def _create_invoice(
        self,
        *,
        status,
        due_date,
        document_type=None,
        reference="INV-STATUS-1",
        source_invoice=None,
    ):
        today = timezone.now().date()
        # Avoid invoice post-save notification signals (admin-server tables).
        return Invoice.objects.bulk_create(
            [
                Invoice(
                    company_profile=self.company,
                    reciepient_company=self.recipient,
                    invoice_date=today,
                    due_date=due_date,
                    status=status,
                    grand_total=100,
                    vat_rate=0,
                    document_type=document_type or Invoice.DocumentType.INVOICE,
                    created_by=self.user,
                    reference_number=reference,
                    source_invoice=source_invoice,
                )
            ]
        )[0]

    @patch(
        "project_management.management.commands.send_open_invoice_reminder.create_pdf_invoice_by_bill"
    )
    def test_open_invoice_becomes_due_on_due_date(self, _mock_pdf):
        from project_management.management.commands.send_open_invoice_reminder import (
            Command,
        )

        today = timezone.now().date()
        invoice = self._create_invoice(status="open", due_date=today)

        Command().update_invoice_statuses()
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, "due")

    @patch(
        "project_management.management.commands.send_open_invoice_reminder.create_pdf_invoice_by_bill"
    )
    def test_due_invoice_becomes_overdue_after_due_date(self, _mock_pdf):
        from project_management.management.commands.send_open_invoice_reminder import (
            Command,
        )

        yesterday = timezone.now().date() - timedelta(days=1)
        invoice = self._create_invoice(status="due", due_date=yesterday)

        Command().update_invoice_statuses()
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, "overdue")

    @patch(
        "project_management.management.commands.send_open_invoice_reminder.create_pdf_invoice_by_bill"
    )
    def test_open_invoice_past_due_becomes_overdue_catchup(self, _mock_pdf):
        from project_management.management.commands.send_open_invoice_reminder import (
            Command,
        )

        yesterday = timezone.now().date() - timedelta(days=1)
        invoice = self._create_invoice(status="open", due_date=yesterday)

        Command().update_invoice_statuses()
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, "overdue")

    @patch(
        "project_management.management.commands.send_open_invoice_reminder.create_pdf_invoice_by_bill"
    )
    def test_paid_invoice_past_due_is_unchanged(self, _mock_pdf):
        from project_management.management.commands.send_open_invoice_reminder import (
            Command,
        )

        yesterday = timezone.now().date() - timedelta(days=1)
        invoice = self._create_invoice(status="paid", due_date=yesterday)

        Command().update_invoice_statuses()
        invoice.refresh_from_db()

        self.assertEqual(invoice.status, "paid")

    @patch(
        "project_management.management.commands.send_open_invoice_reminder.create_pdf_invoice_by_bill"
    )
    def test_credit_note_past_due_is_unchanged(self, _mock_pdf):
        from project_management.management.commands.send_open_invoice_reminder import (
            Command,
        )

        yesterday = timezone.now().date() - timedelta(days=1)
        source_invoice = self._create_invoice(
            status="paid",
            due_date=yesterday,
            reference="INV-STATUS-SOURCE-1",
        )
        credit_note = self._create_invoice(
            status="open",
            due_date=yesterday,
            document_type=Invoice.DocumentType.CREDIT_NOTE,
            reference="CN-STATUS-1",
            source_invoice=source_invoice,
        )

        Command().update_invoice_statuses()
        credit_note.refresh_from_db()

        self.assertEqual(credit_note.status, "open")
