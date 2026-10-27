from django.db import models
from django.utils import timezone

class Coupon(models.Model):
    
    stripe_coupon_id = models.CharField(max_length=100, null=True, blank=True)
    amount_off = models.IntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, default='USD')
    percent_off = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    
    name = models.CharField(max_length=255, unique=True)
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    times_redeemed = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    DURATION_CHOICES = [
        ('once', 'Once'),
        ('forever', 'Forever'),
        ('repeating', 'Repeating'),
    ]
    duration = models.CharField(
        max_length=20,
        choices=DURATION_CHOICES
    )
    duration_in_months = models.IntegerField(null=True, blank=True)    
    
    DISCOUNT_TYPE_CHOICES = [
        ('percentage', 'Percentage'),
        ('fixed', 'Fixed Amount'),
    ]
    discount_type = models.CharField(
        max_length=20,
        choices=DISCOUNT_TYPE_CHOICES,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def is_valid(self):
        """Check if the coupon is still valid"""
        if not self.is_active:
            return False
        if self.max_redemptions and self.times_redeemed and self.times_redeemed >= self.max_redemptions:
            return False
        return True

    def __str__(self):
        return f"{self.name} - {self.discount_type} ({self.amount_off if self.amount_off else self.percent_off})"


class PromoCode(models.Model):
    stripe_promo_id = models.CharField(max_length=100, null=True, blank=True)
    coupon = models.ForeignKey('Coupon', related_name='promo_codes', on_delete=models.CASCADE)

    code = models.CharField(max_length=50, unique=True)
    is_active = models.BooleanField(default=True)

    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    times_redeemed = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)

    first_time_transaction = models.BooleanField(default=False)
    minimum_amount = models.PositiveIntegerField(null=True, blank=True)
    minimum_amount_currency = models.CharField(max_length=3, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    
    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            return False
        if self.max_redemptions and self.times_redeemed >= self.max_redemptions:
            return False
        return True

    def __str__(self):
        return f"{self.code} for Coupon {self.coupon.name}"
