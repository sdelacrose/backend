from django.db import models
from django.db.models import Q
from django.utils import timezone
from model_utils import FieldTracker

from project_management.models import Project, Contact, ContactCompany, ContactUser, TimeCard, ExpenseCard, Document
from models.models import User, CompanyProfile


class Invoice(models.Model):
    class DocumentType(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CREDIT_NOTE = "credit_note", "Credit Note"

    freelancer = models.ForeignKey(User, related_name="billing_invoices", on_delete=models.CASCADE, null=True, blank=True)    
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="billing_invoices",  null=True, blank=True)
    company_profile = models.ForeignKey(CompanyProfile, related_name='billing_invoices', on_delete=models.CASCADE, null=True, blank=True)
    
    reciepient_user = models.ForeignKey(ContactUser, on_delete=models.CASCADE, related_name="billing_invoices", blank=True, null=True)
    reciepient_company = models.ForeignKey(ContactCompany, on_delete=models.CASCADE, related_name="billing_invoices", blank=True, null=True)
    invoice_date = models.DateField()
    due_date = models.DateField()
    status = models.CharField(max_length=255)
    grand_total = models.FloatField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    vat_rate = models.FloatField(null=True, blank=True, default=12)
    documents = models.ManyToManyField(
        Document, related_name='billing_invoices', blank=True
    )
    excluded_auto_documents = models.ManyToManyField(
        Document,
        related_name="billing_invoices_auto_excluded",
        blank=True,
    )
    collaborators = models.ManyToManyField(
        User,
        related_name="collaborator_invoices",
        blank=True,
    )
    
    # New fields for storing company currency conversion data
    company_currency_amount = models.FloatField(
        null=True,
        blank=True,
        help_text="Invoice amount converted to company's currency"
    )
    purchase_order = models.ForeignKey(
        "project_management.PurchaseOrder",
        on_delete=models.PROTECT,
        related_name="invoices",
        null=True,
        blank=True,
    )
    reference_number = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    document_type = models.CharField(
        max_length=20,
        choices=DocumentType.choices,
        default=DocumentType.INVOICE,
    )
    source_invoice = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="credit_notes",
        null=True,
        blank=True,
    )
    replaced_invoice = models.OneToOneField(
        "self",
        on_delete=models.PROTECT,
        related_name="replacement_invoice",
        null=True,
        blank=True,
    )
    carried_forward_amount = models.FloatField(null=True, blank=True, default=0)
    conversion_rate = models.FloatField(
        null=True,
        blank=True,
        help_text="Exchange rate used for currency conversion"
    )
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name="created_invoices", null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    def __str__(self):
        return f"Invoice {self.id} - {self.invoice_date}"
    
    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(document_type="invoice", source_invoice__isnull=True)
                    | Q(document_type="credit_note", source_invoice__isnull=False)
                ),
                name="invoice_source_invoice_matches_document_type",
            ),
            models.CheckConstraint(
                check=Q(document_type="invoice") | Q(replaced_invoice__isnull=True),
                name="invoice_replaced_invoice_only_for_invoices",
            ),
        ]


class InvoiceReferenceCounter(models.Model):
    class Prefix(models.TextChoices):
        INVOICE = "INV", "Invoice"
        CREDIT_NOTE = "CN", "Credit Note"

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="invoice_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        User,
        related_name="invoice_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    (Q(company_profile__isnull=False) & Q(freelancer__isnull=True))
                    | (Q(company_profile__isnull=True) & Q(freelancer__isnull=False))
                ),
                name="invoice_reference_counter_single_owner",
            ),
            models.UniqueConstraint(
                fields=["company_profile"],
                condition=Q(company_profile__isnull=False, freelancer__isnull=True),
                name="unique_invoice_reference_counter_company",
            ),
            models.UniqueConstraint(
                fields=["freelancer"],
                condition=Q(company_profile__isnull=True, freelancer__isnull=False),
                name="unique_invoice_reference_counter_freelancer",
            ),
        ]


class Billing(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="project_bills")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="project_bills")
    invoice_date = models.DateTimeField(default=timezone.now)
    invoice_no = models.CharField(max_length=255, null=True, blank=True)
    invoice_status = models.CharField(max_length=255, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    vat_rate = models.FloatField(null=True, blank=True)
    vat_amount = models.FloatField(null=True, blank=True)
    grand_total = models.FloatField(null=True, blank=True)
    notes = models.TextField(null=True, blank=True)
    documents = models.ManyToManyField(
        'Document', related_name='billings', blank=True
    )

class LineItem(models.Model):
    bill = models.ForeignKey(Billing, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=500, null=True, blank=True)
    rate = models.FloatField(null=True, blank=True)
    quantity = models.FloatField(null=True, blank=True)

class InvoiceLineItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    time_card = models.ForeignKey(TimeCard, on_delete=models.CASCADE, related_name="invoice_line_items", null=True, blank=True)
    expense_card = models.ForeignKey(ExpenseCard, on_delete=models.CASCADE, related_name="invoice_line_items", null=True, blank=True)
    monthly_timesheet_submission = models.ForeignKey(
        "project_management.MonthlyTimesheetSubmission",
        on_delete=models.PROTECT,
        related_name="invoice_line_items",
        null=True,
        blank=True,
    )
    description = models.CharField(max_length=500, null=True, blank=True)
    vat_amount = models.FloatField()
    price = models.FloatField()
    quantity = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
