from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from models.models import CompanyProfile, User

from .contact import ContactCompany, ContactUser


class CrmInteraction(models.Model):
    """CRM-1B: a manual commercial interaction (note, call, meeting).

    Stored once and shown on every record it is linked to (contact, company, opportunity) --
    never copied per record. Tenant is company XOR freelancer, like Correspondence.
    """

    class Type(models.TextChoices):
        NOTE = "note", "Note"
        CALL = "call", "Call"
        MEETING = "meeting", "Meeting"

    type = models.CharField(max_length=16, choices=Type.choices)
    occurred_at = models.DateTimeField()
    summary = models.TextField()
    author = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="crm_interactions",
    )
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, null=True, blank=True, related_name="crm_interactions",
    )
    freelancer = models.ForeignKey(
        User, on_delete=models.CASCADE, null=True, blank=True, related_name="freelancer_crm_interactions",
    )
    contact_user = models.ForeignKey(
        ContactUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="crm_interactions",
    )
    contact_company = models.ForeignKey(
        ContactCompany, on_delete=models.SET_NULL, null=True, blank=True, related_name="crm_interactions",
    )
    opportunity = models.ForeignKey(
        "companyprofileapis.Opportunity", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="crm_interactions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    (Q(company_profile__isnull=False) & Q(freelancer__isnull=True))
                    | (Q(company_profile__isnull=True) & Q(freelancer__isnull=False))
                ),
                name="crm_interaction_single_tenant",
            ),
        ]
        indexes = [
            models.Index(fields=["company_profile", "occurred_at"], name="crm_inter_company_idx"),
            models.Index(fields=["freelancer", "occurred_at"], name="crm_inter_freelancer_idx"),
        ]

    def clean(self):
        from project_management.crm_tenancy import ensure_same_tenant, tenant_of

        if not (self.contact_user_id or self.contact_company_id or self.opportunity_id):
            raise ValidationError(
                {"links": "An interaction must be linked to a contact, a company or an opportunity."}
            )
        tenant = tenant_of(self)
        if tenant is None:
            raise ValidationError({"tenant": "An interaction must belong to a company or a freelancer."})
        ensure_same_tenant(tenant, [self.contact_user, self.contact_company, self.opportunity])

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.type} {self.occurred_at:%Y-%m-%d}"
