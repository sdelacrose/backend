from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.core.exceptions import ValidationError

from django.utils import timezone

from models.models import CompanyProfile, User
from .constants import DEFAULT_FOLDERS
from .project import Project
from model_utils import FieldTracker
from simple_history.models import HistoricalRecords
from ecpmpapp.constants import (
    UPLOAD_PATH_PROJECT,
    UPLOAD_PATH_COMPANY,
    UPLOAD_PATH_FREELANCER,
)

def get_upload_path(instance, filename):
    if instance.folder.project:
        return UPLOAD_PATH_PROJECT.format(
            project_id=instance.folder.project_id,
            folder_id=instance.folder_id,
            filename=filename,
        )
    elif instance.folder.company_profile:
        return UPLOAD_PATH_COMPANY.format(
            company_profile_id=instance.folder.company_profile_id,
            folder_id=instance.folder_id,
            filename=filename,
        )
    elif instance.folder.freelancer:
        return UPLOAD_PATH_FREELANCER.format(
            freelancer_id=instance.folder.freelancer_id,
            folder_id=instance.folder_id,
            filename=filename,
        )

class Folder(models.Model):
    project = models.ForeignKey(Project, related_name='folders', on_delete=models.CASCADE, null=True, blank=True)
    company_profile = models.ForeignKey(CompanyProfile, related_name='folders', on_delete=models.CASCADE, null=True, blank=True)
    freelancer = models.ForeignKey(User, related_name='freelancer_folders', on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=255)
    system_key = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    parent = models.ForeignKey('self', related_name='subfolders', on_delete=models.CASCADE, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    tracker = FieldTracker()
    class Meta:
        ordering = ['id']
        unique_together = ['parent', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile", "name"],
                condition=models.Q(parent__isnull=True, company_profile__isnull=False),
                name="folder_unique_company_root_name",
            ),
            models.UniqueConstraint(
                fields=["project", "system_key"],
                condition=models.Q(project__isnull=False, system_key__isnull=False),
                name="folder_unique_project_system_key",
            ),
        ]

    def clean(self):
        if self.parent:
            if self.project and self.project != self.parent.project:
                raise ValidationError("Child and parent folder must belong to the same project.")
            if self.company_profile and self.company_profile != self.parent.company_profile:
                raise ValidationError("Child and parent folder must belong to the same company.")
            if self.freelancer and self.freelancer != self.parent.freelancer:
                raise ValidationError("Child and parent folder must belong to the same freelancer.")


    def save(self, *args, **kwargs):
        if (
            self.parent_id
            and self.project_id is None
            and self.company_profile_id is None
            and self.freelancer_id is None
        ):
            self.project_id = self.parent.project_id
            self.company_profile_id = self.parent.company_profile_id
            self.freelancer_id = self.parent.freelancer_id

        try:
            self.clean()
        except ValidationError as e:
            raise ValidationError(e.message)

        # Fetch the original instance only once if it exists
        original = Folder.objects.filter(pk=self.pk).first()

        if original:
            if original.system_key and (
                self.system_key != original.system_key
                or self.name != original.name
                or self.parent_id != original.parent_id
                or self.project_id != original.project_id
                or self.company_profile_id != original.company_profile_id
                or self.freelancer_id != original.freelancer_id
            ):
                raise ValidationError("System folder identity cannot be updated.")
            # Check if it's a root folder
            if original.parent is None:
                is_root_project_folder = original.project and original.name == f"{original.project_id}_root_folder"
                is_root_company_folder = original.company_profile and original.name == f"{original.company_profile_id}_company_root_folder"
                is_root_freelancer_folder = original.freelancer and original.name == f"{original.freelancer_id}_freelancer_root_folder"
            else:
                is_root_project_folder = False
                is_root_company_folder = False
                is_root_freelancer_folder = False
            if (is_root_project_folder or is_root_company_folder or is_root_freelancer_folder) and (
                self.name != original.name or self.parent != original.parent or
                (is_root_project_folder and self.project != original.project) or
                (is_root_company_folder and self.company_profile != original.company_profile) or
                (is_root_freelancer_folder and self.freelancer != original.freelancer)
            ):
                raise ValidationError("Root folder cannot be updated.")

            # Check for default folder under root folder
            if self.pk and original.name in DEFAULT_FOLDERS and original.parent:
                is_project_root_parent = original.parent.name == f"{original.project_id}_root_folder"
                is_company_root_parent = original.parent.name == f"{original.company_profile_id}_company_root_folder"
                is_root_freelancer_folder = original.freelancer and original.name == f"{original.freelancer_id}_freelancer_root_folder"

                if (is_project_root_parent or is_company_root_parent or is_root_freelancer_folder) and (
                    self.name != original.name or self.parent != original.parent or
                    (is_project_root_parent and self.project != original.project) or
                    (is_company_root_parent and self.company_profile != original.company_profile) or
                    (is_root_freelancer_folder and self.freelancer != original.freelancer)
                ):
                    raise ValidationError("Default folder cannot be updated.")

        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.system_key:
            raise ValidationError("System folder cannot be deleted.")
        # Prevent deletion of the root folder
        if self.parent is None and \
           (
            self.name == f"{self.project_id}_root_folder" or 
            self.name == f"{self.company_profile_id}_company_root_folder" or 
            self.name == f"{self.freelancer_id}_freelancer_root_folder"
            ):  # Check if it's a root folder
            raise ValidationError("Root folder cannot be deleted.")

        
        if self.name in DEFAULT_FOLDERS and self.parent and \
           (
            self.parent.name == f"{self.project_id}_root_folder" or 
            self.name == f"{self.company_profile_id}_company_root_folder" or 
            self.name == f"{self.freelancer_id}_freelancer_root_folder"
            ):
            raise ValidationError("Default folder cannot be deleted.")

        super().delete(*args, **kwargs)

    def __str__(self):
        return self.name

class DocumentOrigin(models.TextChoices):
    """Who produced the document. A business classification, explicit and never inferred.

    Independent from Document.status (business status), the internal lifecycle, the internal
    workflow and any future distribution / review purpose. An INTERNAL document sent to a client
    stays INTERNAL; CLIENT means the client produced and sent the document to us.
    """

    INTERNAL = "INTERNAL", "Internal"
    CLIENT = "CLIENT", "Client"
    SUPPLIER = "SUPPLIER", "Supplier"


class Document(models.Model):
    folder = models.ForeignKey(Folder, related_name='documents', on_delete=models.CASCADE)
    author = models.ForeignKey(User, related_name='authored_documents', on_delete=models.SET_NULL, null=True, blank=True) # assignee
    # MAIN controlled file, kept as the compatibility pointer to the latest stored version.
    # Empty ("") for a register entry whose main document is not created yet (TO_CREATE).
    file = models.FileField(upload_to=get_upload_path, blank=True)
    file_name = models.CharField(max_length=500, null=True, blank=True)
    document_type = models.CharField(max_length=255, null=True, blank=True)
    identification_number = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=255, null=True, blank=True)
    # Legacy documents predate this distinction and keep the internal Document Control behaviour.
    document_origin = models.CharField(
        max_length=20,
        choices=DocumentOrigin.choices,
        default=DocumentOrigin.INTERNAL,
        db_index=True,
    )
    # CLIENT / SUPPLIER only: the Network company (ContactCompany) that issued the document.
    external_issuer_company = models.ForeignKey(
        "ContactCompany",
        related_name="issued_documents",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    internal_review_status = models.CharField(max_length=255, null=True, blank=True)
    external_review_status = models.CharField(max_length=255, null=True, blank=True)
    transmittal_category = models.CharField(max_length=255, null=True, blank=True)
    # Document identity metadata (not versioned): plain-text context and lightweight tags.
    description = models.TextField(blank=True, default="")
    tags = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
    )
    
    # Document numbering system fields
    category = models.CharField(max_length=255, null=True, blank=True)
    discipline = models.CharField(max_length=255, null=True, blank=True)
    discipline_group = models.CharField(max_length=255, null=True, blank=True)
    discipline_group_id = models.CharField(max_length=100, null=True, blank=True)
    discipline_option_id = models.CharField(max_length=100, null=True, blank=True)
    discipline_code = models.CharField(max_length=50, null=True, blank=True)
    sequence_number = models.IntegerField(null=True, blank=True)
    revision = models.IntegerField(null=True, blank=True, default=0)
    full_document_number = models.CharField(max_length=255, null=True, blank=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    history = HistoricalRecords()
    tracker = FieldTracker()

    created_by = models.ForeignKey(User, related_name='created_documents', on_delete=models.SET_NULL, null=True, blank=True)
    assignee = models.ForeignKey(User, related_name='assigned_documents', on_delete=models.SET_NULL, null=True, blank=True) # same as author

    class Meta:
        indexes = [
            GinIndex(
                fields=["file_name"],
                name="document_filename_trgm",
                opclasses=["gin_trgm_ops"],
            ),
        ]
    
    def __str__(self):
        return self.file.name

    def delete(self, *args, **kwargs):
        # Delete associated historical records
        self.history.all().delete()
        # Call the parent class's delete method
        super().delete(*args, **kwargs)


class DocumentReferenceCounter(models.Model):
    project = models.ForeignKey(
        Project,
        related_name="document_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="document_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        User,
        related_name="document_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    # "" = the library counter (legacy behaviour and the default PROJECT scope); the other
    # sequence scopes use their own keys ("company", "discipline:STR", "type:DWG").
    scope_key = models.CharField(max_length=100, default="", blank=True)
    last_sequence = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(
                        project__isnull=False,
                        company_profile__isnull=True,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        project__isnull=True,
                        company_profile__isnull=False,
                        freelancer__isnull=True,
                    )
                    | models.Q(
                        project__isnull=True,
                        company_profile__isnull=True,
                        freelancer__isnull=False,
                    )
                ),
                name="document_reference_counter_single_owner",
            ),
            models.UniqueConstraint(
                fields=["project", "scope_key"],
                condition=models.Q(
                    project__isnull=False,
                    company_profile__isnull=True,
                    freelancer__isnull=True,
                ),
                name="unique_document_reference_counter_project",
            ),
            models.UniqueConstraint(
                fields=["company_profile", "scope_key"],
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=False,
                    freelancer__isnull=True,
                ),
                name="unique_document_reference_counter_company",
            ),
            models.UniqueConstraint(
                fields=["freelancer", "scope_key"],
                condition=models.Q(
                    project__isnull=True,
                    company_profile__isnull=True,
                    freelancer__isnull=False,
                ),
                name="unique_document_reference_counter_freelancer",
            ),
        ]
