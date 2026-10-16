"""MAIL-1.5-B: the company's library of e-mail templates for outgoing correspondences.

A template only ever *proposes* a subject and a body (plain text, with a closed list of
`{{variable}}` markers, see services/correspondence_template.py). Applying one copies the rendered
text into the correspondence's own subject/message, so the correspondence keeps its content even if
the template is later edited or deactivated: there is deliberately no link from a correspondence to
the template it started from.

No recipients, Cc/Bcc, signature, attachment or HTML is ever stored here. Templates are owned by one
company and are never shared across companies. They are deactivated, not deleted.
"""
from django.db import models
from simple_history.models import HistoricalRecords

from models.models import CompanyProfile, User


class CorrespondenceTemplate(models.Model):
    class Language(models.TextChoices):
        FR = "FR", "Français"
        EN = "EN", "English"
        DE = "DE", "Deutsch"

    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="correspondence_templates",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    language = models.CharField(max_length=2, choices=Language.choices)
    subject_template = models.CharField(max_length=500)
    body_template = models.TextField()
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name="created_correspondence_templates",
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile", "name"], name="correspondence_template_unique_name_per_company",
            ),
        ]

    def __str__(self):
        return f"{self.name} [{self.language}]"

    def clean(self):
        # Imported here: the service module imports the models of this app.
        from project_management.services.correspondence_template import validate_template_fields

        super().clean()
        validate_template_fields(
            name=self.name, subject_template=self.subject_template, body_template=self.body_template,
        )

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
