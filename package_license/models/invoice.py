from django.db import models
from django.conf import settings

from models.models import CompanyProfile, User
from package_license.models import Package

class StripeInvoice(models.Model):
    INVOICE_STATUS = (
        ('failed', 'failed'),
        ('paid', 'Paid'),
    )
    company_profile = models.ForeignKey(CompanyProfile, related_name="invoices", on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(User, related_name="invoices", on_delete=models.CASCADE, null=True, blank=True)
    invoice_id = models.CharField(max_length=255, unique=True)  # Stripe invoice ID
    invoice_no = models.CharField(max_length=255, null=True, blank=True)  # Stripe invoice No
    amount_due = models.FloatField()
    amount_paid = models.FloatField()
    amount_remaining = models.FloatField()
    status = models.CharField(max_length=10, choices=INVOICE_STATUS, default='paid')  
    currency = models.CharField(max_length=10)
    invoice_date = models.DateTimeField(null=True, blank=True)
    payment_method = models.CharField(max_length=500, null=True, blank=True)
    receipt_url = models.URLField(null=True, blank=True)
    line_items = models.JSONField(default=dict)
    package = models.ForeignKey(Package, related_name="invoices", on_delete=models.SET_NULL, null=True, blank=True)
    invoice_pdf = models.URLField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Invoice {self.invoice_id} for {self.company_profile.company_name if self.company_profile else self.user.first_name}"
