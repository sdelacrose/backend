from datetime import timedelta

from django.core.management.base import BaseCommand
from django.core.mail import send_mail
from django.template import loader
from django.utils import timezone
from django.conf import settings
from django.db.models import Q

from project_management.models import Invoice, Folder, Document
from project_management.invoice_status import get_automatic_invoice_status
from project_management.utils import (
    create_pdf_invoice_by_bill,
    format_currency_amount,
    get_billing_document_label,
    get_currency_from_record,
    get_invoice_reference_display,
    get_user_email_preferences,
)
from ecpmpapp.alt_messages import (
    plain_messages_for_invoice_send_reminders,
    plain_message_for_invoice_due_reminder,
    plain_message_invoice_overdue,
    plain_message_invoice_overdue_reminder
)
class Command(BaseCommand):
    help = 'Send invoice reminder emails based on due dates and payment status'

    def handle(self, *args, **kwargs):
        today = timezone.now().date()
        
        # Send reminder 1 day before due date
        tomorrow = today + timedelta(days=1)
        tomorrow_invoices = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='open',
            due_date=tomorrow
        )
        self.send_reminders(tomorrow_invoices, "tomorrow")

        # Send reminder 1 week before due date
        week_ahead = today + timedelta(days=7)
        upcoming_invoices = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='open',
            due_date=week_ahead
        )
        self.send_reminders(upcoming_invoices, "upcoming")

        # Send overdue notification 1 day after due date
        yesterday = today - timedelta(days=1)
        new_overdue_invoices = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='due',
            due_date=yesterday
        )
        self.send_reminders(new_overdue_invoices, "just_overdue")

        # Send weekly reminders for all overdue invoices
        all_overdue_invoices = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='overdue',
            due_date__lt=today
        )
        for invoice in all_overdue_invoices:
            days_overdue = (today - invoice.due_date).days
            if days_overdue % 7 == 0:  # Send reminder every 7 days
                self.send_reminders([invoice], "weekly_overdue")

        # Update statuses
        self.update_invoice_statuses()

    def send_reminders(self, invoices, reminder_type):
        for invoice in invoices:
            invoice_pdf_url = self.get_invoice_pdf_url(invoice)
            recipient_name = invoice.reciepient_user.name if invoice.reciepient_user else invoice.reciepient_company.company_name
            recipient_email = invoice.reciepient_user.email if invoice.reciepient_user else invoice.reciepient_company.contact_email
            if invoice.reciepient_user:
                recipient_id = invoice.reciepient_user.user_id
            elif invoice.reciepient_company.company_profile_id:
                recipient_id = invoice.reciepient_company.company_profile.admin_user_id
            else:
                recipient_id = invoice.reciepient_company.user_id
            email_check = get_user_email_preferences(recipient_id)
            currency = get_currency_from_record(invoice)

            context = {
                "contact_name": recipient_name,
                "invoice_no": get_invoice_reference_display(invoice),
                "due_date": invoice.due_date,
                "invoice_url": invoice_pdf_url,
                "days_overdue": (timezone.now().date() - invoice.due_date).days if reminder_type in ["just_overdue", "weekly_overdue"] else None,
                "amount": format_currency_amount(invoice.grand_total, currency),
            }
            document_label = get_billing_document_label(invoice)
            reference_number = get_invoice_reference_display(invoice)

            subject_map = {
                "tomorrow": f"{document_label} #{reference_number} Due Tomorrow",
                "upcoming": f"Upcoming {document_label} #{reference_number} Due in One Week",
                "just_overdue": f"{document_label} #{reference_number} Overdue",
                "weekly_overdue": f"{document_label} #{reference_number} Overdue"
            }

            template_map = {
                "tomorrow": 'invoice_due_email.html',
                "upcoming": 'invoice_upcoming_email.html',
                "just_overdue": 'invoice_overdue_email.html',
                "weekly_overdue": 'invoice_weekly_overdue_email.html'
            }

            tomorrow_message = plain_messages_for_invoice_send_reminders(
                contact_name=recipient_name,
                invoice_no=reference_number,
                due_date=invoice.due_date,
                amount=format_currency_amount(invoice.grand_total, currency),
                invoice_url=invoice_pdf_url
            )

            upcoming_message = plain_message_for_invoice_due_reminder(
                contact_name=recipient_name,
                invoice_no=reference_number,
                due_date=invoice.due_date,
                amount=format_currency_amount(invoice.grand_total, currency),
                invoice_url=invoice_pdf_url
            )

            overdue_message = plain_message_invoice_overdue(
                contact_name=recipient_name,
                invoice_no=reference_number,
                due_date=invoice.due_date,
                amount=format_currency_amount(invoice.grand_total, currency),
                invoice_url=invoice_pdf_url
            )

            weekly_overdue_message = plain_message_invoice_overdue_reminder(
                contact_name=recipient_name,
                invoice_no=reference_number,
                due_date=invoice.due_date,
                days_overdue=context["days_overdue"],
                amount=format_currency_amount(invoice.grand_total, currency),
                invoice_url=invoice_pdf_url
            )

            plain_messages = {
                "tomorrow": tomorrow_message,
                "upcoming": upcoming_message,
                "just_overdue": overdue_message,
                "weekly_overdue": weekly_overdue_message
            }
            if email_check:
                try:
                    send_mail(
                        subject=subject_map[reminder_type],
                        message=plain_messages[reminder_type],
                        html_message=loader.render_to_string(template_map[reminder_type], context),
                        from_email=settings.DEFAULT_FROM_EMAIL,
                        recipient_list=[recipient_email],
                        fail_silently=False,
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'Reminder email ({reminder_type}) sent to {recipient_email} for {document_label} #{reference_number}'
                    ))
                except Exception as e:
                    self.stdout.write(self.style.ERROR(
                        f'Failed to send reminder email to {recipient_email}, Reason: {repr(e)}'
                    ))

    def get_invoice_pdf_url(self, record):
        root_folder_name = None
        if record.project:
            if record.project.user.is_freelancer:
                root_folder_name = f"{record.project.user.id}_freelancer_root_folder"
            else:
                root_folder_name = f"{record.project.user.company.id}_company_root_folder"
        elif record.company_profile:
            root_folder_name = f"{record.company_profile.id}_company_root_folder"
        elif record.freelancer:
            root_folder_name = f"{record.freelancer.id}_freelancer_root_folder"

        if root_folder_name:
            root_folder = Folder.objects.filter(name=root_folder_name).first()
            if root_folder:
                invoices_folder = Folder.objects.filter(parent=root_folder, name="invoices").first()
                if invoices_folder:
                    file_name = f"invoice_project_{root_folder_name}_bill_{record.id}.pdf"
                    existing_document = Document.objects.filter(
                        folder=invoices_folder, 
                        file__icontains=file_name.split(".")[0]
                    ).first()
                    return existing_document.file.url if existing_document else None
        return None

    def update_invoice_statuses(self):
        today = timezone.now().date()

        # Update to 'due' status on the due date
        open_due_today = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='open',
            due_date=today
        )
        self.update_invoice_status(open_due_today, "due")

        # Update to 'overdue' status one day after due date
        due_past = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='due',
            due_date__lt=today
        )
        self.update_invoice_status(due_past, "overdue")

        # Catch-up: open invoices already past due (missed cron / created past due)
        open_past = Invoice.objects.filter(
            document_type=Invoice.DocumentType.INVOICE,
            status__iexact='open',
            due_date__lt=today
        )
        self.update_invoice_status(open_past, "overdue")

    def update_invoice_status(self, invoices, status):
        for invoice in invoices:
            invoice.status = get_automatic_invoice_status(
                status=invoice.status,
                due_date=invoice.due_date,
                document_type=invoice.document_type,
            )
            invoice.save()
            create_pdf_invoice_by_bill(invoice)
            self.stdout.write(self.style.SUCCESS(
                f'Status updated to "{status}" for invoice #{get_invoice_reference_display(invoice)}'
            ))
