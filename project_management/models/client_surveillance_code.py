"""GED-WF.4.1: per-project customisation of the client surveillance result codes (A/B/C/D by default).

The four business categories are fixed and stable (they carry the business rules, see
services.client_surveillance_code): a project may only customise the VISIBLE code and the EN/FR/DE
labels. A row exists only for a category the project customised; without a row the defaults apply
(default code + the translations already shipped with the application), so nothing is created
until a project actually changes something.

Nothing here touches ClientSurveillance / ClientIssue: results recorded later store the category
(stable) plus a snapshot of the code and label at the time, so renaming a code never rewrites history.
"""

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower

from models.models import User

from .project import Project

CODE_PATTERN = r"^[A-Za-z0-9]{1,10}$"


class ClientSurveillanceCode(models.Model):
    class Category(models.TextChoices):
        ACCEPTED = "ACCEPTED", "Accepted"
        ACCEPTED_WITH_COMMENTS = "ACCEPTED_WITH_COMMENTS", "Accepted with comments"
        COMMENTED = "COMMENTED", "Commented"
        REJECTED = "REJECTED", "Rejected"

    project = models.ForeignKey(Project, related_name="client_surveillance_codes", on_delete=models.CASCADE)
    category = models.CharField(max_length=32, choices=Category.choices)
    code = models.CharField(
        max_length=10,
        validators=[RegexValidator(CODE_PATTERN, "The code is 1 to 10 letters or digits.")],
    )
    # null = use the default translation of the category
    label_en = models.CharField(max_length=100, null=True, blank=True)
    label_fr = models.CharField(max_length=100, null=True, blank=True)
    label_de = models.CharField(max_length=100, null=True, blank=True)
    updated_by = models.ForeignKey(User, related_name="+", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["project", "category"], name="client_surveillance_code_unique_category"),
            models.UniqueConstraint(
                Lower("code"), "project", name="client_surveillance_code_unique_code_ci",
            ),
        ]

    def __str__(self):
        return f"{self.code} ({self.category}) for project {self.project_id}"

    def save(self, *args, **kwargs):
        self.full_clean(exclude=["project", "updated_by"], validate_unique=False)
        if self.pk:
            stored = ClientSurveillanceCode.objects.filter(pk=self.pk).values("category", "project_id").first()
            if stored and (stored["category"] != self.category or stored["project_id"] != self.project_id):
                raise ValidationError("The category of a client surveillance code cannot be changed.")
        super().save(*args, **kwargs)
