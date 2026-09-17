from django.db import models
from django.contrib.postgres.fields import ArrayField

from model_utils import FieldTracker
from simple_history.models import HistoricalRecords
from models.models import User, CompanyProfile

class Contact(models.Model):
    title = models.CharField(max_length=255)
    name = models.CharField(max_length=255)
    surname = models.CharField(max_length=255)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    address_line_1 = models.CharField(max_length=255, null=True, blank=True)
    town = models.CharField(max_length=100, db_index=True)  # Adding db_index for searchability
    country = models.CharField(max_length=100)
    postcode = models.CharField(max_length=20)
    telephone_number = models.CharField(max_length=50, blank=True, null=True)
    mobile_number = models.CharField(max_length=50, blank=True, null=True)
    area_of_competence = models.CharField(max_length=255, blank=True, null=True)
    contact_type = models.CharField(max_length=255)
    web_address = models.CharField(max_length=255, blank=True, null=True)
    email_address = models.CharField(max_length=255)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="contacts", null=True, default=None
    )

    history = HistoricalRecords()
    tracker = FieldTracker()
    class Meta:
        ordering = ['id']

    def delete(self, *args, **kwargs):
        # Delete associated historical records
        self.history.all().delete()
        # Call the parent class's delete method
        super().delete(*args, **kwargs)


class ContactUser(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="contact_users", null=True, default=None
    )
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="contact_users", null=True, default=None
    )
    name = models.CharField(max_length=500)
    surname = models.CharField(max_length=500)
    telephone_no = models.CharField(max_length=500, null=True, blank=True)
    mobile_number = models.CharField(max_length=500, null=True, blank=True)
    linkedin_url = models.CharField(max_length=500, null=True, blank=True)
    email = models.EmailField()
    birthday = models.DateField(null=True, blank=True)
    headline = models.CharField(max_length=1000, null=True, blank=True)
    industry = models.CharField(max_length=500, null=True, blank=True)
    education = models.CharField(max_length=500, null=True, blank=True)
    position = models.CharField(max_length=500, null=True, blank=True)
    keyword = models.CharField(max_length=1000, null=True, blank=True)
    qualification = models.CharField(max_length=500, null=True, blank=True)
    country = models.CharField(max_length=500, null=True, blank=True)
    city = models.CharField(max_length=500, null=True, blank=True)
    spoken_languages = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
    )
    availability_from = models.DateField(null=True, blank=True)
    availability_until = models.DateField(null=True, blank=True)
    availability_location = models.TextField(null=True, blank=True)
    open_to_new_challenges = models.BooleanField(default=False)
    competencies = models.CharField(max_length=500, null=True, blank=True)
    main_area_of_expertise = models.CharField(max_length=255, null=True, blank=True)
    sub_area_of_expertise = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
    )
    company_name = models.CharField(max_length=255, null=True, blank=True)
    contact_company = models.ForeignKey(
        "ContactCompany",
        on_delete=models.SET_NULL,
        related_name="linked_contact_users",
        null=True,
        blank=True,
    )
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    postcode = models.CharField(max_length=20, null=True, blank=True)
    street = models.CharField(max_length=255, null=True, blank=True)
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    iban = models.CharField(max_length=255, null=True, blank=True)
    bic = models.CharField(max_length=255, null=True, blank=True)
    bank_address = models.CharField(max_length=255, null=True, blank=True)
    salutation = models.CharField(max_length=50, null=True, blank=True)
    tracker = FieldTracker()

class ContactCompany(models.Model):
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="contact_companies", null=True, default=None
    )
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="contact_companies", null=True, default=None
    )
    company_name = models.CharField(max_length=255)
    company_role = models.CharField(max_length=255, null=True, blank=True)
    company_logo = models.CharField(max_length=1000, null=True, blank=True)
    contact_name = models.CharField(max_length=255, null=True, blank=True)
    contact_surname = models.CharField(max_length=255, null=True, blank=True)
    company_address = models.CharField(max_length=1000, null=True, blank=True)
    company_postcode = models.CharField(max_length=255, null=True, blank=True)
    company_town = models.CharField(max_length=255, null=True, blank=True)
    company_tel_no = models.CharField(max_length=255, null=True, blank=True)
    company_website = models.CharField(max_length=500, null=True, blank=True)
    company_country = models.CharField(max_length=500, null=True, blank=True)
    area_of_competence = models.CharField(max_length=500, null=True, blank=True)
    main_area_of_expertise = models.CharField(max_length=255, null=True, blank=True)
    sub_area_of_expertise = ArrayField(
        models.CharField(max_length=255),
        default=list,
        blank=True,
    )
    contact_email = models.EmailField()
    products = models.CharField(max_length=1000, null=True, blank=True)
    services = models.CharField(max_length=1000, null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)
    bank_name = models.CharField(max_length=255, null=True, blank=True)
    iban = models.CharField(max_length=255, null=True, blank=True)
    bic = models.CharField(max_length=255, null=True, blank=True)
    bank_address = models.CharField(max_length=255, null=True, blank=True)
    salutation = models.CharField(max_length=50, null=True, blank=True)
    tracker = FieldTracker()
