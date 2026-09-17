from django.db import models
from django.utils import timezone
from simple_history.models import HistoricalRecords

from project_management.models import ContactCompany, ContactUser, Document, Folder
from models.models import User, CompanyProfile
from model_utils import FieldTracker


def default_date():
    return timezone.now().date()

class Opportunity(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        DRAFT = "draft", "Draft"
        CONVERTED = "converted", "Converted To Offer"
        ISSUED = "issued", "Offer Issued"
        DECLINED = "declined", "Offer Declined"
        CONVERTED_TO_PROJECT = "converted_to_project", "Converted To Project"

    class ReferencePrefix(models.TextChoices):
        OPPORTUNITY = "OPP", "Opportunity"
        OFFER = "OFF", "Offer"

    OPPORTUNITY_STATUSES = Status.choices
    OFFER_STATUSES = {
        Status.DRAFT,
        Status.CONVERTED,
        Status.ISSUED,
        Status.DECLINED,
        Status.CONVERTED_TO_PROJECT,
    }
    created_by = models.ForeignKey(User, related_name="created_opportunities", on_delete=models.CASCADE, null=True, blank=True)    
    freelancer = models.ForeignKey(User, related_name="opportunities", on_delete=models.CASCADE, null=True, blank=True)    
    company_profile = models.ForeignKey(CompanyProfile, related_name='opportunities', on_delete=models.CASCADE, null=True, blank=True)

    title = models.CharField(max_length=500)
    reference = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        editable=False,
        db_index=True,
    )
    details = models.TextField(null=True, blank=True)
    status = models.TextField(max_length=100, default="open", choices=OPPORTUNITY_STATUSES)
    customer_user = models.ForeignKey(ContactUser, on_delete=models.CASCADE, related_name="opportunities", blank=True, null=True)
    customer_company = models.ForeignKey(ContactCompany, on_delete=models.CASCADE, related_name="opportunities", blank=True, null=True)
    country = models.CharField(max_length=255, null=True, blank=True)
    revenue = models.FloatField(null=True, blank=True)
    probability = models.FloatField(null=True, blank=True)
    contract_date = models.DateField(null=True, blank=True)
    planned_delivery_start_date = models.DateField(null=True, blank=True)
    planned_delivery_end_date = models.DateField(null=True, blank=True)
    purchase_order = models.TextField(null=True, blank=True)
    type_of_service = models.CharField(max_length=255, null=True, blank=True)
    responsible_salesperson = models.ForeignKey(
        User,
        related_name="responsible_opportunities",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    documents = models.ManyToManyField(Document, related_name="opportunities", blank=True)
    linked_folders = models.ManyToManyField(
        Folder,
        related_name="opportunity_linked_folders",
        blank=True,
    )
    projects = models.ManyToManyField(
        "project_management.Project",
        related_name="opportunities",
        blank=True,
    )

    approver = models.ForeignKey(User, related_name="approved_offers", on_delete=models.CASCADE, null=True, blank=True)
    sent_date = models.DateField(null=True, blank=True)
    offer_created_date = models.DateField(null=True, blank=True)
    offer_expiry_period = models.CharField(
        max_length=32,
        null=True,
        blank=True,
    )
    offer_expiry_date = models.DateField(null=True, blank=True)
    offer_pdf_document = models.ForeignKey(
        Document,
        related_name="generated_offer_for",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    value = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
    )
    sales_phase = models.CharField(max_length=50, null=True, blank=True, default="Identification")

    close_reason = models.CharField(max_length=255, null=True, blank=True)

    history = HistoricalRecords()
    tracker = FieldTracker()


    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def delete(self, *args, **kwargs):
        # Delete associated historical records
        self.history.all().delete()
        # Call the parent class's delete method
        super().delete(*args, **kwargs)

    @classmethod
    def reference_prefix_for_status(cls, status):
        if status in cls.OFFER_STATUSES:
            return cls.ReferencePrefix.OFFER
        return cls.ReferencePrefix.OPPORTUNITY

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile", "reference"],
                condition=models.Q(
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                name="unique_opportunity_reference_company",
            ),
            models.UniqueConstraint(
                fields=["freelancer", "reference"],
                condition=models.Q(
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                name="unique_opportunity_reference_freelancer",
            ),
        ]


class OpportunityReferenceCounter(models.Model):
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="opportunity_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        User,
        related_name="opportunity_reference_counters",
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
                    models.Q(
                        company_profile__isnull=False,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        company_profile__isnull=True,
                        freelancer__isnull=False,
                    )
                ),
                name="opportunity_reference_counter_single_owner",
            ),
            models.UniqueConstraint(
                fields=["company_profile"],
                condition=models.Q(
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                name="unique_opportunity_counter_company",
            ),
            models.UniqueConstraint(
                fields=["freelancer"],
                condition=models.Q(
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                name="unique_opportunity_counter_freelancer",
            ),
        ]


class OfferLineItem(models.Model):
    opportunity = models.ForeignKey(
        Opportunity,
        on_delete=models.CASCADE,
        related_name="offer_line_items",
    )
    available_product = models.ForeignKey(
        "project_management.AvailableProductPart",
        on_delete=models.SET_NULL,
        related_name="offer_line_items",
        null=True,
        blank=True,
    )
    line_number = models.PositiveIntegerField(default=1)
    item_name = models.CharField(max_length=255, blank=True, default="")
    rate = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.DecimalField(max_digits=12, decimal_places=4, default=1)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=12)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["line_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["opportunity", "line_number"],
                name="unique_offer_line_number",
            ),
            models.CheckConstraint(
                check=models.Q(rate__gte=0),
                name="offer_line_item_rate_nonnegative",
            ),
            models.CheckConstraint(
                check=models.Q(quantity__gte=0),
                name="offer_line_item_quantity_nonnegative",
            ),
            models.CheckConstraint(
                check=models.Q(vat_rate__gte=0, vat_rate__lte=100),
                name="offer_line_item_vat_range",
            ),
        ]


class NoticeBoard(models.Model):
    freelancer = models.ForeignKey(User, related_name="notices", on_delete=models.CASCADE, null=True, blank=True)    
    company_profile = models.ForeignKey(CompanyProfile, related_name='notices', on_delete=models.CASCADE, null=True, blank=True)

    text = models.TextField()
    expires_at = models.DateField()

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
