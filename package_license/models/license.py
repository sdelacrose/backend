from django.db import models
from django.core.exceptions import ValidationError
from models.models import User, CompanyProfile
from django.utils import timezone


class Product(models.Model):
    STATUS_CHOICES = [
        ('Active', 'Active'),
        ('Stopped', 'Stopped'),
    ]
    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    price = models.FloatField() # Monthly price
    yearly_price = models.FloatField(null=True, blank=True)
    stripe_monthly_price_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_yearly_price_id = models.CharField(max_length=255, blank=True, null=True)
    link = models.URLField(max_length=500, blank=True, null=True)  
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Active')  
    specification_file = models.FileField(upload_to='product_specifications', null=True, blank=True)
    specification_file_name = models.CharField(max_length=255, null=True, blank=True)
    specification_uploaded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class DefaultPackage(models.Model):
    name = models.CharField(max_length=255, unique=True)
    products = models.ManyToManyField(Product, related_name="packages")
    user_count = models.IntegerField()
    price = models.FloatField()
    stripe_monthly_price_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_yearly_price_id = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class Package(models.Model):
    STATUS_CHOICES = [
        ("Auto Renew", "Auto Renew"),
        ("Cancelled", "Cancelled"),
    ]
    is_active = models.BooleanField(default=True)
    company_profile = models.ForeignKey(CompanyProfile, related_name="licenses", on_delete=models.CASCADE, null=True, blank=True)
    user = models.ForeignKey(User, related_name="packages", on_delete=models.CASCADE, null=True, blank=True)
    stripe_session_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_subscription_id = models.CharField(max_length=255, blank=True, null=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="Auto Renew")
    is_freetrial = models.BooleanField(default=False)
    billing_plan = models.CharField(null=True, blank=True)
    promotion_code_applied = models.CharField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def package_type(self):
        return self.invoices.latest("created_at").line_items['data'][0]['plan']['interval'] if self.invoices.exists() and self.invoices.latest("created_at").line_items else ''


class License(models.Model):
    package = models.ForeignKey(Package, related_name="licenses", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name="licenses", on_delete=models.CASCADE)
    is_active = models.BooleanField(default=False)
    license_assigned_at = models.DateTimeField(null=True, blank=True)
    user = models.ForeignKey(User, related_name="licenses", null=True, blank=True, on_delete=models.SET_NULL)
    
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"License {self.product.name} ({self.package.id if self.package else 'No Package'})"

    @property
    def license_type(self):
        if self.package.billing_plan:
            return 'year' if self.package.billing_plan == 'yearly' else 'month'
        return None
