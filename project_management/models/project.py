from uuid import uuid4

from django.utils import timezone
from django.db import models, transaction
from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.conf import settings
from simple_history.models import HistoricalRecords
from model_utils import FieldTracker
from django.db.models import Count, Sum, Q, F, FloatField, Max

from models.models import CompanyProfile, User, Role
from models.constants import GC_SKILLS, SHAREMUNDO_SOLUTION_TYPES
from ecpmpapp.constants import UPLOAD_PATH_ASSESSMENT

from .constants import (
    ECPMP_ACCEPTANCE_STATUS_OPTIONS,
    ECPMP_BOM_STATUS_OPTIONS,
    ECPMP_DEMAND_STATUS_OPTIONS,
    ECPMP_REQUIREMENT_STATUS_OPTIONS,
    INCOMING_PURCHASE_ORDER_STATUSES,
    OUTGOING_PURCHASE_ORDER_STATUSES,
)

def default_start_date():
    return timezone.now().date()

def default_solution():
    return settings.PRODUCT_NAME

def default_requirement_skill_options():
    return list(GC_SKILLS)


def default_demand_status_options():
    return list(ECPMP_DEMAND_STATUS_OPTIONS)


def default_requirement_status_options():
    return list(ECPMP_REQUIREMENT_STATUS_OPTIONS)


def default_bom_status_options():
    return list(ECPMP_BOM_STATUS_OPTIONS)


def default_acceptance_status_options():
    return list(ECPMP_ACCEPTANCE_STATUS_OPTIONS)


def default_transport_columns():
    return []

def get_upload_path(instance, filename):
    # Generate the path based on the associated Folder's project_id
    return UPLOAD_PATH_ASSESSMENT.format(
        project_id=instance.project_id,
        record_type=instance.record_type,
        filename=filename
    )

class ProjectClient(models.Model):
    project = models.OneToOneField("Project", on_delete=models.CASCADE, related_name="client")
    contact_user = models.ForeignKey("ContactUser", on_delete=models.SET_NULL, related_name="projects", null=True, blank=True)
    contact_company = models.ForeignKey("ContactCompany", on_delete=models.SET_NULL, related_name="projects", null=True, blank=True)

    def clean(self):
        # Ensure only one of contact_user or contact_company is set
        if self.contact_user and self.contact_company:
            raise ValidationError("A project client can only be either a ContactUser or a ContactCompany.")
        if not self.contact_user and not self.contact_company:
            raise ValidationError("A project client must have at least one contact set.")

class Project(models.Model):
    PROJECT_STATUS = (
        ('Under preparation', 'Under preparation'),
        ('In progress', 'In progress'),
        ('On hold', 'On hold'),
        ('Closed', 'Closed')
    )

    solution = models.CharField(max_length=255, choices=SHAREMUNDO_SOLUTION_TYPES, default=default_solution)
    # Short code used in permanent document references: proposed from the name at creation,
    # editable until the first referenced Document exists, then permanent.
    project_code = models.CharField(max_length=10, blank=True, default="")
    project_name = models.CharField(max_length=255)
    project_description = models.TextField(blank=True, null=True)
    funding_strategy = models.CharField(max_length=255, blank=True, null=True)
    lat_long = models.CharField(max_length=500, null=True, blank=True)
    contract_type = models.CharField(max_length=255, blank=True, null=True)
    international_standard = models.CharField(max_length=255, blank=True, null=True)
    major_project_model = models.CharField(max_length=255, blank=True, null=True)
    commercial_model = models.CharField(max_length=255, blank=True, null=True)
    supporting_agreement = models.CharField(max_length=255, blank=True, null=True)
    contract_value = models.DecimalField(max_digits=30, decimal_places=2)
    address = models.CharField(max_length=500, blank=True, null=True)
    town = models.CharField(max_length=255, blank=True, null=True)
    country = models.CharField(max_length=100, blank=True, null=True)
    currency = models.CharField(max_length=100, blank=True, null=True, default="€")
    contract_details = models.TextField(blank=True, null=True)
    postcode = models.CharField(max_length=20, blank=True, null=True)
    project_image = models.CharField(max_length=1000, null=True, blank=True)
    project_priority = models.CharField(max_length=255, default="Medium", null=True, blank=True)
    project_start_date = models.DateField(default=default_start_date)
    project_end_date = models.DateField(blank=True, null=True)
    project_status = models.CharField(max_length=50, choices=PROJECT_STATUS, default='Under preparation')
    project_type = models.CharField(max_length=255, blank=True, null=True)
    project_colour = models.CharField(max_length=300, null=True, blank=True)
    place_id = models.CharField(max_length=300, null=True, blank=True)

    type_of_service = models.CharField(max_length=255, null=True, blank=True)
    
    company_profile = models.ForeignKey(
        CompanyProfile, on_delete=models.CASCADE, related_name="company_projects", null=True, blank=True
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="user_projects", null=True, blank=True
    )
    # Creator metadata
    created_by = models.ForeignKey(User, related_name='created_projects', on_delete=models.SET_NULL, null=True, blank=True)
    created_by_email = models.CharField(max_length=255, blank=True, null=True)
    created_by_name = models.CharField(max_length=255, blank=True, null=True)

    sharepoint_site_id = models.CharField(max_length=500, null=True, blank=True)
    sharepoint_site_name = models.CharField(max_length=500, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    def __str__(self):
        return str(self.project_name)
    
    def save(self, *args, **kwargs):
        if self.pk:  # Check if it's an update
            original = Project.objects.get(pk=self.pk)
            self._date_changed = (
                self.project_start_date != original.project_start_date or self.project_end_date != original.project_end_date
            )
            if self.project_code and self.project_code != original.project_code:
                from project_management.services.project_codes import (
                    assert_project_code_change_allowed,
                )

                assert_project_code_change_allowed(self)
        else:
            self._date_changed = False
        if not self.project_code:
            # Proposed once; a later rename never changes it.
            from project_management.services.project_codes import (
                propose_unique_project_code,
            )

            self.project_code = propose_unique_project_code(self)

        super().save(*args, **kwargs)


class ProjectTeamMembers(models.Model):
    ACCESS_CHOICES = (
        ('RO', 'Read Only'),
        ('RW', 'Read & Write')
    )
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='team_members')
    role = models.CharField(max_length=255)
    project_role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='project_team_members', null=True, blank=True)
    project_access = models.CharField(max_length=2, choices=ACCESS_CHOICES, default='RO')
    team_member = models.ForeignKey(User, on_delete=models.CASCADE, related_name='project_teams')
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    tracker = FieldTracker()

    class Meta:
        unique_together = ('project', 'team_member')

    def __str__(self):
        return f"{self.team_member.first_name} - {self.role}"


class ProjectExternalUsers(models.Model):
    project = models.ForeignKey('Project', on_delete=models.CASCADE, related_name='external_users')
    role = models.CharField(max_length=255)
    project_role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name='external_project_team_members', null=True, blank=True)
    contact_user = models.ForeignKey("ContactUser", on_delete=models.SET_NULL, related_name="external_project_users", null=True, blank=True)
    contact_company = models.ForeignKey("ContactCompany", on_delete=models.SET_NULL, related_name="external_project_users", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)

    def __str__(self):
        return f"{self.contact_user.name if self.contact_user else self.contact_company.company_name} - {self.role}"


class ECPMPConfig(models.Model):
    DEFAULT_INCOMING_PO_STATUSES = list(INCOMING_PURCHASE_ORDER_STATUSES)
    DEFAULT_OUTGOING_PO_STATUSES = list(OUTGOING_PURCHASE_ORDER_STATUSES)

    project = models.OneToOneField('Project', on_delete=models.CASCADE, related_name='configurations', default=None, null=True)
    m365_tenant_id = models.CharField(max_length=500, blank=True, null=True)
    funding_portal_url = models.URLField(blank=True, null=True)
    team_members_roles = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    funding_strategy = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    status_options = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    demand_status_options = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=default_demand_status_options,
    )
    requirement_status_options = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=default_requirement_status_options,
    )
    bom_status_options = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=default_bom_status_options,
    )
    acceptance_status_options = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=default_acceptance_status_options,
    )
    reminder_settings = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_types = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_status = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_internal_review_status = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_external_review_status = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    transmittal_category = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    priority_levels = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    project_priority_levels = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    project_service_types = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    labels = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_categories = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    document_disciplines = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    nested_configurations = models.JSONField(default=dict, blank=True)
    # Document Control settings (reference, revision, workflow); empty = canonical defaults.
    document_control_configuration = models.JSONField(default=dict, blank=True)
    rooms = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    skills = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=default_requirement_skill_options,
    )


class Milestone(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='milestones')
    sort_order = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    colour = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=255, default="open")
    reminder_setting = models.CharField(max_length=255, default="none")
    allocated_budget = models.FloatField(default=1)
    allocated_time = models.IntegerField(blank=True, null=True)
    phase_title = models.CharField(null=True, blank=True, max_length=1000)
    start_date = models.DateField(null=True, blank=True)
    due_date = models.DateField()
    documents = models.ManyToManyField(
        'Document', related_name='milestones', blank=True
    )
    linked_folders = models.ManyToManyField(
        'Folder', related_name='milestone_linked_folders', blank=True
    )
    closed_at = models.DateField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='closed_milestones', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, null=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ['sort_order', 'id']
        indexes = [
            models.Index(
                fields=['project', 'sort_order'],
                name='pm_phase_project_order_idx',
            ),
        ]

    def __str__(self):
        return str(self.name)
    
    def save(self, *args, **kwargs):
        is_new = self._state.adding
        if not is_new:  # Check if it's an update
            try:
                original = Milestone.objects.get(pk=self.pk)
                self._date_changed = (
                    self.start_date != original.start_date or self.due_date != original.due_date
                )
            except Milestone.DoesNotExist:
                self._date_changed = False
        else:
            self._date_changed = False

        if is_new and not self.sort_order:
            with transaction.atomic():
                Project.objects.select_for_update().only('id').get(
                    pk=self.project_id
                )
                max_sort_order = (
                    Milestone.objects.filter(project_id=self.project_id)
                    .aggregate(max_sort_order=Max('sort_order'))
                    .get('max_sort_order')
                    or 0
                )
                self.sort_order = max_sort_order + 1
                return super().save(*args, **kwargs)

        return super().save(*args, **kwargs)


class ActionItem(models.Model):
    milestone = models.ForeignKey(Milestone, related_name='action_items', on_delete=models.CASCADE, null=True, blank=True)
    parent_action_item = models.ForeignKey('self', related_name='sub_action_items', on_delete=models.CASCADE, null=True, blank=True)
    # TODO: remove legacy assignee field after all API, permission, notification,
    # and frontend consumers have been migrated to assignees.
    assignee = models.ForeignKey(User, related_name='action_items', on_delete=models.SET_NULL, null=True, blank=True)
    assignees = models.ManyToManyField(
        User,
        related_name='multi_assigned_action_items',
        blank=True,
    )
    company_profile = models.ForeignKey(CompanyProfile, related_name="action_items", on_delete=models.CASCADE, null=True, blank=True)
    freelancer = models.ForeignKey(User, related_name="freelancer_action_items", on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=255)
    notes = models.TextField(blank=True, null=True)
    allocated_time = models.IntegerField(blank=True, null=True)
    start_date = models.DateField(null=True, blank=True)
    target_date = models.DateField()
    forecast_date = models.DateField(null=True, blank=True)
    reminder = models.CharField(max_length=255, default="none")
    status = models.CharField(max_length=100)
    allocated_budget = models.FloatField(default=1)
    priority_level = models.CharField(max_length=255, null=True, blank=True)
    labels = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        default=list,
    )
    closed_at = models.DateField(null=True, blank=True)
    closed_by = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='closed_action_items', null=True, blank=True)
    created_by = models.ForeignKey(User, related_name='created_action_items', on_delete=models.SET_NULL, null=True, blank=True)
    documents = models.ManyToManyField('Document', related_name='action_items', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='action_item_linked_folders', blank=True)
    requirements = models.ManyToManyField('Requirement', related_name='action_items', blank=True)
    opportunities = models.ManyToManyField('companyprofileapis.Opportunity', related_name='action_items', blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    def __str__(self):
        return f"{self.name}"

    def save(self, *args, **kwargs):
        if self.pk:  # Check if it's an update
            try:
                original = ActionItem.objects.get(pk=self.pk)
                self._date_changed = (
                    self.start_date != original.start_date or self.target_date != original.target_date
                )
            except ActionItem.DoesNotExist:
                self._date_changed = False
        else:
            self._date_changed = False 

        if self.parent_action_item:
            if self.milestone:
                raise ValueError("Sub-action items cannot have a milestone attached.")
            if self.company_profile:
                raise ValueError("Sub-action items cannot have a company attached.")
            if self.freelancer:
                raise ValueError("Sub-action items cannot have a freelancer attached.")
            if self.parent_action_item.parent_action_item:
                raise ValueError("Sub-action items cannot have further sub-action items.")
            if self.pk and self.sub_action_items.exists():
                raise ValueError("An action item with children cannot become a sub-action item.")
        super().save(*args, **kwargs)


class ActionItemDependency(models.Model):
    class DependencyType(models.TextChoices):
        FINISH_TO_START = "finish_to_start", "Finish to start"

    predecessor = models.ForeignKey(
        ActionItem,
        related_name="outgoing_dependencies",
        on_delete=models.CASCADE,
    )
    dependent = models.ForeignKey(
        ActionItem,
        related_name="incoming_dependencies",
        on_delete=models.CASCADE,
    )
    dependency_type = models.CharField(
        max_length=32,
        choices=DependencyType.choices,
        default=DependencyType.FINISH_TO_START,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["predecessor", "dependent", "dependency_type"],
                name="unique_action_item_dependency",
            ),
            models.CheckConstraint(
                condition=~Q(predecessor=F("dependent")),
                name="action_item_dependency_not_self",
            ),
        ]
        indexes = [
            models.Index(
                fields=["predecessor", "dependency_type"],
                name="ai_dep_predecessor_type_idx",
            ),
            models.Index(
                fields=["dependent", "dependency_type"],
                name="ai_dep_dependent_type_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.predecessor_id} -> {self.dependent_id} "
            f"({self.dependency_type})"
        )


class ActionItemReminderDelivery(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"

    action_item = models.ForeignKey(
        ActionItem,
        related_name="reminder_deliveries",
        on_delete=models.CASCADE,
    )
    assignee = models.ForeignKey(
        User,
        related_name="action_item_reminder_deliveries",
        on_delete=models.CASCADE,
    )
    reminder_date = models.DateField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["action_item", "assignee", "reminder_date"],
                name="unique_action_item_reminder_delivery",
            ),
        ]
        indexes = [
            models.Index(
                fields=["reminder_date", "status"],
                name="ai_reminder_date_status_idx",
            ),
        ]


class ActionItemComment(models.Model):
    action_item = models.ForeignKey(
        ActionItem,
        on_delete=models.CASCADE,
        related_name="comments",
    )
    sender = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="action_item_comments",
    )
    content = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list, blank=True)
    linked_objects = models.JSONField(default=list, blank=True)
    mentions = models.JSONField(default=list, blank=True)
    replied_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )
    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_action_item_comments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["action_item", "created_at"]),
            models.Index(fields=["action_item", "id"]),
            models.Index(fields=["replied_to", "created_at"]),
            models.Index(fields=["sender", "created_at"]),
        ]

    def __str__(self):
        preview = (self.content or "").strip()
        if len(preview) > 50:
            preview = f"{preview[:47]}..."
        return f"{self.action_item_id}:{self.sender_id}:{preview or 'comment'}"


class ActionItemCommentReaction(models.Model):
    comment = models.ForeignKey(
        ActionItemComment,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="action_item_comment_reactions",
    )
    key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("comment", "user", "key")
        indexes = [
            models.Index(fields=["comment", "key"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.comment_id}:{self.key}:{self.user_id}"


class ObjectAssessment(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='object_assessments')
    record_type = models.CharField(max_length=500)
    record_title = models.CharField(max_length=500, null=True, blank=True)
    file = models.FileField(upload_to=get_upload_path, null=True, blank=True)
    notes_content_json = models.JSONField(null=True, blank=True)
    notes_content_html = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    documents = models.ManyToManyField(
        'Document', related_name='object_assesments', blank=True
    )

    author = models.ForeignKey(User, related_name='authored_object_assessments', on_delete=models.SET_NULL, null=True, blank=True)
    document_type = models.CharField(max_length=255, null=True, blank=True)
    identification_number = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=255, null=True, blank=True)
    file_name = models.CharField(max_length=500, null=True, blank=True)
    history = HistoricalRecords()
    revision = models.IntegerField(null=True, blank=True, default=0)

    created_by = models.ForeignKey(User, related_name='created_object_assessments', on_delete=models.SET_NULL, null=True, blank=True)
    assignee = models.ForeignKey(User, related_name='assigned_object_assessments', on_delete=models.SET_NULL, null=True, blank=True) # same as author
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()


class ProjectMatrixConfig(models.Model):
    project = models.OneToOneField(Project, on_delete=models.CASCADE, related_name='matrix_config')
    demand_columns = models.JSONField(default=list, blank=True)
    requirement_columns = models.JSONField(default=list, blank=True)
    transport_columns = models.JSONField(default=default_transport_columns, blank=True)
    created_by = models.ForeignKey(User, related_name='created_project_matrix_configs', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_project_matrix_configs', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Matrix config for project {self.project_id}"


def default_parts_columns():
    return [
        {
            "key": "manufacturer",
            "label": "Manufacturer",
            "label_en": "Manufacturer",
            "label_de": "Hersteller",
            "label_fr": "Fabricant",
            "type": "text",
            "order": 0,
            "idta_semantic_id": "0173-1#02-AAO677#002",
            "system": True,
            "protected": True,
            "aas_field": "ManufacturerName",
            "aas_ownership": "aas",
        },
        {
            "key": "description",
            "label": "Description",
            "type": "text",
            "order": 1,
            "idta_semantic_id": "",
            "aas_ownership": "links",
        },
        {
            "key": "quantity",
            "label": "Quantity",
            "type": "text",
            "order": 2,
            "idta_semantic_id": "",
            "aas_ownership": "links",
        },
    ]


def default_machine_columns():
    return []


def default_equipment_columns():
    return []


def default_solution_space_columns():
    return [
        {
            "key": "assembly_name",
            "label": "Assembly Name",
            "label_en": "Assembly Name",
            "label_de": "Baugruppenname",
            "label_fr": "Nom de l'ensemble",
            "type": "text",
            "scope": "both",
            "order": 0,
            "visible": True,
            "system": True,
            "required": True,
            "idta_semantic_id": "",
        },
        {
            "key": "assembly_type",
            "label": "Assembly Type",
            "label_en": "Assembly Type",
            "label_de": "Baugruppentyp",
            "label_fr": "Type d'ensemble",
            "type": "assembly_type",
            "scope": "both",
            "order": 1,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "required_skills",
            "label": "Required Skill",
            "label_en": "Required Skill",
            "label_de": "Erforderliche Fähigkeit",
            "label_fr": "Compétence requise",
            "type": "skills",
            "scope": "both",
            "order": 2,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "provided_skills",
            "label": "Provided Skills",
            "label_en": "Provided Skills",
            "label_de": "Bereitgestellte Fähigkeiten",
            "label_fr": "Compétences fournies",
            "type": "skills",
            "scope": "both",
            "order": 3,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "responsible_engineer",
            "label": "Responsible Engineer",
            "label_en": "Responsible Engineer",
            "label_de": "Verantwortlicher Ingenieur",
            "label_fr": "Ingénieur responsable",
            "type": "user",
            "scope": "both",
            "order": 4,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "projects",
            "label": "Assembly used in Project",
            "label_en": "Assembly used in Project",
            "label_de": "In Projekten verwendete Baugruppe",
            "label_fr": "Ensemble utilisé dans le projet",
            "type": "projects",
            "scope": "company",
            "order": 5,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "customers",
            "label": "Customer Name",
            "label_en": "Customer Name",
            "label_de": "Kundenname",
            "label_fr": "Nom du client",
            "type": "customers",
            "scope": "both",
            "order": 6,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
        {
            "key": "year_of_construction",
            "label": "Year of Construction",
            "label_en": "Year of Construction",
            "label_de": "Baujahr",
            "label_fr": "Année de construction",
            "type": "year",
            "scope": "both",
            "order": 7,
            "visible": True,
            "system": True,
            "idta_semantic_id": "",
        },
    ]


def default_solution_space_skill_options():
    """Retained for historical migrations that created the legacy field."""
    return default_requirement_skill_options()


class PartsListConfig(models.Model):
    company_profile = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name='parts_list_configs',
        null=True,
        blank=True,
    )
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name='parts_list_config',
        null=True,
        blank=True,
    )
    available_product_columns = models.JSONField(default=default_parts_columns, blank=True)
    bom_columns = models.JSONField(default=default_parts_columns, blank=True)
    created_by = models.ForeignKey(User, related_name='created_parts_list_configs', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_parts_list_configs', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['company_profile'],
                condition=Q(project__isnull=True),
                name='unique_company_parts_config',
            ),
            models.UniqueConstraint(
                fields=['project'],
                condition=Q(project__isnull=False),
                name='unique_project_parts_config',
            ),
        ]

    def __str__(self):
        if self.project_id:
            return f"Parts list config for project {self.project_id}"
        return f"Parts list config for company {self.company_profile_id}"


class SolutionSpaceConfig(models.Model):
    company_profile = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="solution_space_configs",
        null=True,
        blank=True,
    )
    project = models.OneToOneField(
        Project,
        on_delete=models.CASCADE,
        related_name="solution_space_config",
        null=True,
        blank=True,
    )
    columns = models.JSONField(default=default_solution_space_columns, blank=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_solution_space_configs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_solution_space_configs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile"],
                condition=Q(project__isnull=True),
                name="unique_company_solution_space_config",
            ),
            models.UniqueConstraint(
                fields=["project"],
                condition=Q(project__isnull=False),
                name="unique_project_solution_space_config",
            ),
        ]

    def __str__(self):
        if self.project_id:
            return f"Solution Space config for project {self.project_id}"
        return f"Solution Space config for company {self.company_profile_id}"


class MachineConfig(models.Model):
    company_profile = models.OneToOneField(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="machine_config",
    )
    machine_columns = models.JSONField(default=default_machine_columns, blank=True)
    created_by = models.ForeignKey(User, related_name="created_machine_configs", on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name="modified_machine_configs", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Machine config for company {self.company_profile_id}"


class EquipmentConfig(models.Model):
    company_profile = models.OneToOneField(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name="equipment_config",
    )
    equipment_columns = models.JSONField(
        default=default_equipment_columns,
        blank=True,
    )
    created_by = models.ForeignKey(User, related_name="created_equipment_configs", on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name="modified_equipment_configs", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Equipment config for company {self.company_profile_id}"


class MatrixTreeItem(models.Model):
    MAX_LEVEL = 10

    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    parent = models.ForeignKey('self', related_name='children', on_delete=models.CASCADE, null=True, blank=True)
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=1)
    custom_fields = models.JSONField(default=dict, blank=True)
    object_assessments = models.ManyToManyField(ObjectAssessment, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ['sort_order', 'id']

    def get_sequence(self):
        current = self
        parts = []
        while current:
            siblings = current.__class__.objects.filter(
                project=current.project,
                parent=current.parent,
            ).order_by('sort_order', 'id').values_list('id', flat=True)
            sibling_ids = list(siblings)
            parts.append(str(sibling_ids.index(current.id) + 1 if current.id in sibling_ids else 1))
            current = current.parent
        return ".".join(reversed(parts))


class SolutionDemand(MatrixTreeItem):
    project = models.ForeignKey(Project, related_name='solution_demands', on_delete=models.CASCADE)
    parent = models.ForeignKey('self', related_name='sub_demands', on_delete=models.CASCADE, null=True, blank=True)
    object_assessments = models.ManyToManyField(ObjectAssessment, related_name='solution_demands', blank=True)
    documents = models.ManyToManyField('Document', related_name='solution_demands', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='solution_demand_linked_folders', blank=True)
    created_by = models.ForeignKey(User, related_name='created_solution_demands', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_solution_demands', on_delete=models.SET_NULL, null=True, blank=True)
    subject = models.CharField(max_length=255)
    demand_number = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=255, blank=True, null=True, default=None)

    class Meta(MatrixTreeItem.Meta):
        indexes = [
            models.Index(fields=['project', 'parent', 'sort_order']),
            models.Index(fields=['project', 'level']),
        ]

    def __str__(self):
        return self.subject


class Requirement(MatrixTreeItem):
    project = models.ForeignKey(Project, related_name='requirements', on_delete=models.CASCADE)
    parent = models.ForeignKey('self', related_name='sub_requirements', on_delete=models.CASCADE, null=True, blank=True)
    object_assessments = models.ManyToManyField(ObjectAssessment, related_name='requirements', blank=True)
    documents = models.ManyToManyField('Document', related_name='requirements', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='requirement_linked_folders', blank=True)
    solution_demands = models.ManyToManyField(SolutionDemand, related_name='requirements', blank=True)
    created_by = models.ForeignKey(User, related_name='created_requirements', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_requirements', on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    object_number = models.CharField(max_length=255, blank=True, null=True)
    skill = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=255, blank=True, null=True, default=None)

    class Meta(MatrixTreeItem.Meta):
        indexes = [
            models.Index(fields=['project', 'parent', 'sort_order']),
            models.Index(fields=['project', 'level']),
        ]

    def __str__(self):
        return self.name


class Transport(MatrixTreeItem):
    project = models.ForeignKey(Project, related_name='transports', on_delete=models.CASCADE)
    parent = models.ForeignKey('self', related_name='sub_transports', on_delete=models.CASCADE, null=True, blank=True)
    object_assessments = models.ManyToManyField(ObjectAssessment, related_name='transports', blank=True)
    documents = models.ManyToManyField('Document', related_name='transports', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='transport_linked_folders', blank=True)
    created_by = models.ForeignKey(User, related_name='created_transports', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_transports', on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    transport_start_date = models.DateField()
    transport_end_date = models.DateField()
    transportation_lanes = models.TextField(blank=True, null=True)
    carrier_per_transportation_lane_and_means_of_transport = models.TextField(blank=True, null=True)
    location = models.TextField(blank=True, null=True)
    location_with_address = models.TextField(blank=True, null=True)
    equipment = models.TextField(blank=True, null=True)
    freight_settlement_document = models.TextField(blank=True, null=True)
    unlocode = models.TextField(blank=True, null=True)
    unlocode_text = models.TextField(blank=True, null=True)
    stops_for_transportation_stages = models.TextField(blank=True, null=True)
    freight_order_freight_booking_freight_unit_root_or_header = models.TextField(blank=True, null=True)
    freight_order_freight_booking_freight_unit_item_or_line_item = models.TextField(blank=True, null=True)
    document_references = models.TextField(blank=True, null=True)
    freight_order_types = models.TextField(blank=True, null=True)
    freight_unit_types = models.TextField(blank=True, null=True)
    transportation_unit_types = models.TextField(blank=True, null=True)
    order_based_transportation_requirement = models.TextField(blank=True, null=True)
    delivery_based_transportation_requirement = models.TextField(blank=True, null=True)
    forwarding_order_types = models.TextField(blank=True, null=True)
    freight_agreement_types = models.TextField(blank=True, null=True)
    freight_agreement_root = models.TextField(blank=True, null=True)
    freight_agreement_positions = models.TextField(blank=True, null=True)
    block_reason_codes = models.TextField(blank=True, null=True)
    texts_for_block_reason_codes = models.TextField(blank=True, null=True)
    freight_unit_formation_rule_header = models.TextField(blank=True, null=True)
    tcc_solution_basis = models.TextField(blank=True, null=True)
    rate_tables = models.TextField(blank=True, null=True)
    transportation_charges = models.TextField(blank=True, null=True)
    freight_order_freight_booking_freight_unit = models.TextField(blank=True, null=True)
    transportation_requirement = models.TextField(blank=True, null=True)
    forwarding_settlement_document = models.TextField(blank=True, null=True)
    vehicle_resource = models.TextField(blank=True, null=True)
    driver = models.TextField(blank=True, null=True)

    class Meta(MatrixTreeItem.Meta):
        indexes = [
            models.Index(fields=['project', 'parent', 'sort_order']),
            models.Index(fields=['project', 'level']),
        ]

    def __str__(self):
        return self.name


class AvailableProductPart(models.Model):
    MAX_LEVEL = 10

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name='available_product_parts',
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        'self',
        related_name='sub_parts',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=1)
    name = models.CharField(max_length=255)
    part_number = models.CharField(max_length=255, blank=True, null=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    object_assessments = models.ManyToManyField(
        ObjectAssessment,
        related_name='available_product_parts',
        blank=True,
    )
    documents = models.ManyToManyField('Document', related_name='available_product_parts', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='available_product_part_linked_folders', blank=True)
    created_by = models.ForeignKey(User, related_name='created_available_product_parts', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_available_product_parts', on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'id']
        indexes = [
            models.Index(fields=['company_profile', 'parent', 'sort_order']),
            models.Index(fields=['company_profile', 'level']),
        ]

    def get_sequence(self):
        current = self
        parts = []
        while current:
            siblings = AvailableProductPart.objects.filter(
                company_profile=current.company_profile,
                parent=current.parent,
            ).order_by('sort_order', 'id').values_list('id', flat=True)
            sibling_ids = list(siblings)
            parts.append(str(sibling_ids.index(current.id) + 1 if current.id in sibling_ids else 1))
            current = current.parent
        return ".".join(reversed(parts))

    def __str__(self):
        return self.name


class Machine(models.Model):
    MAX_LEVEL = 10

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="machines",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="sub_machines",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=1)
    name = models.CharField(max_length=255)
    custom_fields = models.JSONField(default=dict, blank=True)
    projects = models.ManyToManyField("Project", related_name="machines", blank=True)
    documents = models.ManyToManyField("Document", related_name="machines", blank=True)
    linked_folders = models.ManyToManyField("Folder", related_name="machine_linked_folders", blank=True)
    created_by = models.ForeignKey(User, related_name="created_machines", on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name="modified_machines", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["company_profile", "parent", "sort_order"]),
            models.Index(fields=["company_profile", "level"]),
        ]

    def get_sequence(self):
        current = self
        parts = []
        while current:
            siblings = Machine.objects.filter(
                company_profile=current.company_profile,
                parent=current.parent,
            ).order_by("sort_order", "id").values_list("id", flat=True)
            sibling_ids = list(siblings)
            parts.append(str(sibling_ids.index(current.id) + 1 if current.id in sibling_ids else 1))
            current = current.parent
        return ".".join(reversed(parts))

    def __str__(self):
        return self.name


class Equipment(models.Model):
    MAX_LEVEL = 10

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="equipments",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="sub_equipments",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=1)
    name = models.CharField(max_length=255)
    custom_fields = models.JSONField(default=dict, blank=True)
    documents = models.ManyToManyField("Document", related_name="equipments", blank=True)
    linked_folders = models.ManyToManyField("Folder", related_name="equipment_linked_folders", blank=True)
    created_by = models.ForeignKey(User, related_name="created_equipments", on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name="modified_equipments", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(fields=["company_profile", "parent", "sort_order"]),
            models.Index(fields=["company_profile", "level"]),
        ]

    def get_sequence(self):
        current = self
        parts = []
        while current:
            siblings = Equipment.objects.filter(
                company_profile=current.company_profile,
                parent=current.parent,
            ).order_by("sort_order", "id").values_list("id", flat=True)
            sibling_ids = list(siblings)
            parts.append(str(sibling_ids.index(current.id) + 1 if current.id in sibling_ids else 1))
            current = current.parent
        return ".".join(reversed(parts))

    def __str__(self):
        return self.name


class BillOfMaterialPart(MatrixTreeItem):
    project = models.ForeignKey(Project, related_name='bom_parts', on_delete=models.CASCADE)
    parent = models.ForeignKey('self', related_name='sub_parts', on_delete=models.CASCADE, null=True, blank=True)
    object_assessments = models.ManyToManyField(ObjectAssessment, related_name='bom_parts', blank=True)
    documents = models.ManyToManyField('Document', related_name='bom_parts', blank=True)
    linked_folders = models.ManyToManyField('Folder', related_name='bom_part_linked_folders', blank=True)
    requirements = models.ManyToManyField(Requirement, related_name='bom_parts', blank=True)
    available_product = models.ForeignKey(
        AvailableProductPart,
        related_name='bom_parts',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    source_project_assembly = models.ForeignKey(
        'ProjectSolutionSpaceItem',
        related_name='copied_bom_parts',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(User, related_name='created_bom_parts', on_delete=models.SET_NULL, null=True, blank=True)
    modified_by = models.ForeignKey(User, related_name='modified_bom_parts', on_delete=models.SET_NULL, null=True, blank=True)
    name = models.CharField(max_length=255)
    part_number = models.CharField(max_length=255, blank=True, null=True)
    status = models.CharField(max_length=255, blank=True, null=True, default=None)
    source_workstation = models.ForeignKey(
        'ProjectWorkstation',
        related_name='bom_parts',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    source_workstation_snapshot = models.ForeignKey(
        'WorkstationSnapshot',
        related_name='bom_parts',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    source_occurrence_id = models.UUIDField(null=True, blank=True, db_index=True)
    engineering_quantity = models.DecimalField(
        max_digits=20,
        decimal_places=6,
        null=True,
        blank=True,
    )
    engineering_unit = models.CharField(max_length=64, blank=True, default='')
    is_purchasable = models.BooleanField(default=False)
    engineering_detached = models.BooleanField(default=False, db_index=True)

    class Meta(MatrixTreeItem.Meta):
        indexes = [
            models.Index(fields=['project', 'parent', 'sort_order']),
            models.Index(fields=['project', 'level']),
            models.Index(
                fields=['project', 'source_workstation', 'source_occurrence_id'],
                name='bom_part_workstation_occ_idx',
            ),
        ]

    def __str__(self):
        return self.name


class CompanySolutionSpaceItem(models.Model):
    MAX_LEVEL = 10

    ASSEMBLY_TYPE_COMPONENTS = "components"
    ASSEMBLY_TYPE_PRODUCTION_LINES = "production_lines"
    ASSEMBLY_TYPE_CHOICES = (
        (ASSEMBLY_TYPE_COMPONENTS, "Components"),
        (ASSEMBLY_TYPE_PRODUCTION_LINES, "Production Lines"),
    )

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="company_solution_space_items",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="sub_items",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    aas_product = models.OneToOneField(
        AvailableProductPart,
        related_name="solution_space_assembly",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    sort_order = models.PositiveIntegerField(default=0, db_index=True)
    level = models.PositiveSmallIntegerField(default=1)
    assembly_name = models.CharField(max_length=255, db_index=True)
    assembly_type = models.CharField(
        max_length=32,
        choices=ASSEMBLY_TYPE_CHOICES,
        default=ASSEMBLY_TYPE_COMPONENTS,
        blank=True,
        null=True,
        db_index=True,
    )
    required_skills = models.JSONField(default=list, blank=True)
    provided_skills = models.JSONField(default=list, blank=True)
    responsible_engineer = models.ForeignKey(
        User,
        related_name="responsible_company_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    projects = models.ManyToManyField(
        Project,
        related_name="company_solution_space_items",
        blank=True,
    )
    customers = models.ManyToManyField(
        "ContactCompany",
        related_name="company_solution_space_items",
        blank=True,
    )
    year_of_construction = models.PositiveSmallIntegerField(null=True, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    documents = models.ManyToManyField(
        "Document",
        related_name="company_solution_space_items",
        blank=True,
    )
    linked_folders = models.ManyToManyField(
        "Folder",
        related_name="company_solution_space_item_linked_folders",
        blank=True,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_company_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_company_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "id"]
        indexes = [
            models.Index(
                fields=["company_profile", "parent", "sort_order"],
                name="company_ss_parent_order_idx",
            ),
            models.Index(
                fields=["company_profile", "level"],
                name="company_ss_level_idx",
            ),
        ]

    def get_sequence(self):
        current = self
        parts = []
        while current:
            sibling_ids = list(
                CompanySolutionSpaceItem.objects.filter(
                    company_profile=current.company_profile,
                    parent=current.parent,
                ).order_by("sort_order", "id").values_list("id", flat=True)
            )
            parts.append(
                str(sibling_ids.index(current.id) + 1 if current.id in sibling_ids else 1)
            )
            current = current.parent
        return ".".join(reversed(parts))

    def __str__(self):
        return self.assembly_name


class CompanyAssemblyProductionLink(models.Model):
    product_assembly = models.ForeignKey(
        CompanySolutionSpaceItem,
        related_name="process_assembly_links",
        on_delete=models.CASCADE,
    )
    process_assembly = models.ForeignKey(
        CompanySolutionSpaceItem,
        related_name="product_assembly_links",
        on_delete=models.CASCADE,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_company_assembly_production_links",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product_assembly", "process_assembly"],
                name="company_assembly_unique_production_link",
            ),
            models.CheckConstraint(
                condition=~models.Q(product_assembly=models.F("process_assembly")),
                name="company_assembly_distinct_production_link",
            ),
        ]
        indexes = [
            models.Index(
                fields=["product_assembly", "process_assembly"],
                name="company_assembly_prod_link_idx",
            ),
        ]

    def clean(self):
        errors = {}
        if (
            self.product_assembly_id
            and self.process_assembly_id
            and self.product_assembly.company_profile_id
            != self.process_assembly.company_profile_id
        ):
            errors["process_assembly"] = "Linked assemblies must belong to the same company."
        if (
            self.product_assembly_id
            and self.product_assembly.assembly_type
            != CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS
        ):
            errors["product_assembly"] = "Select an assembly from the Product Assembly Catalog."
        if (
            self.process_assembly_id
            and self.process_assembly.assembly_type
            != CompanySolutionSpaceItem.ASSEMBLY_TYPE_PRODUCTION_LINES
        ):
            errors["process_assembly"] = "Select an assembly from the Process Assembly Catalog."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.product_assembly_id} -> {self.process_assembly_id}"


class ProjectSolutionSpaceItem(MatrixTreeItem):
    project = models.ForeignKey(
        Project,
        related_name="project_solution_space_items",
        on_delete=models.CASCADE,
    )
    parent = models.ForeignKey(
        "self",
        related_name="sub_items",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    object_assessments = models.ManyToManyField(
        ObjectAssessment,
        related_name="project_solution_space_items",
        blank=True,
    )
    documents = models.ManyToManyField(
        "Document",
        related_name="project_solution_space_items",
        blank=True,
    )
    linked_folders = models.ManyToManyField(
        "Folder",
        related_name="project_solution_space_item_linked_folders",
        blank=True,
    )
    requirements = models.ManyToManyField(
        Requirement,
        related_name="project_solution_space_items",
        blank=True,
    )
    customers = models.ManyToManyField(
        "ContactCompany",
        related_name="project_solution_space_items",
        blank=True,
    )
    source_company_item = models.ForeignKey(
        CompanySolutionSpaceItem,
        related_name="project_copies",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    assembly_name = models.CharField(max_length=255, db_index=True)
    assembly_type = models.CharField(
        max_length=32,
        choices=CompanySolutionSpaceItem.ASSEMBLY_TYPE_CHOICES,
        default=CompanySolutionSpaceItem.ASSEMBLY_TYPE_COMPONENTS,
        blank=True,
        null=True,
        db_index=True,
    )
    required_skills = models.JSONField(default=list, blank=True)
    provided_skills = models.JSONField(default=list, blank=True)
    responsible_engineer = models.ForeignKey(
        User,
        related_name="responsible_project_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    year_of_construction = models.PositiveSmallIntegerField(null=True, blank=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_project_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    modified_by = models.ForeignKey(
        User,
        related_name="modified_project_solution_space_items",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    class Meta(MatrixTreeItem.Meta):
        indexes = [
            models.Index(
                fields=["project", "parent", "sort_order"],
                name="project_ss_parent_order_idx",
            ),
            models.Index(
                fields=["project", "level"],
                name="project_ss_level_idx",
            ),
        ]

    def __str__(self):
        return self.assembly_name


def default_part_purchase_items():
    return []


def default_part_purchase_snapshot():
    return {}


def default_part_purchase_address():
    return {}


def default_part_purchase_general_information():
    return {}


def part_purchase_reference():
    return f"PP-{timezone.now():%Y%m%d}-{uuid4().hex[:8].upper()}"


class PartPurchaseIntent(models.Model):
    STATUS_DRAFT = "draft"
    STATUS_CODE_SENT = "code_sent"
    STATUS_VERIFIED = "verified"
    STATUS_CONFIRMED = "confirmed"
    STATUS_CANCELLED = "cancelled"

    SOURCE_AI_CHAT = "ai_chat"
    SOURCE_AVAILABLE_PRODUCT = "available_product"
    SOURCE_BOM_PART = "bom_part"
    SOURCE_MANUAL = "manual"

    PRICE_ON_REQUEST = "price_on_request"
    PRICE_AVAILABLE = "price_available"
    PRICE_NOT_AVAILABLE = "not_available"

    STATUS_CHOICES = (
        (STATUS_DRAFT, "Draft"),
        (STATUS_CODE_SENT, "Code sent"),
        (STATUS_VERIFIED, "Verified"),
        (STATUS_CONFIRMED, "Confirmed"),
        (STATUS_CANCELLED, "Cancelled"),
    )
    SOURCE_CHOICES = (
        (SOURCE_AI_CHAT, "AI chat"),
        (SOURCE_AVAILABLE_PRODUCT, "Available product"),
        (SOURCE_BOM_PART, "Bill of material part"),
        (SOURCE_MANUAL, "Manual"),
    )
    PRICE_STATUS_CHOICES = (
        (PRICE_ON_REQUEST, "Price on request"),
        (PRICE_AVAILABLE, "Price available"),
        (PRICE_NOT_AVAILABLE, "Not available"),
    )

    reference = models.CharField(max_length=40, unique=True, default=part_purchase_reference)
    buyer = models.ForeignKey(User, related_name="part_purchase_intents", on_delete=models.CASCADE)
    buyer_company = models.ForeignKey(
        CompanyProfile,
        related_name="part_purchase_intents",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default=STATUS_DRAFT, db_index=True)
    source = models.CharField(max_length=40, choices=SOURCE_CHOICES, default=SOURCE_MANUAL)
    source_product_id = models.CharField(max_length=255, blank=True, null=True)
    project = models.ForeignKey(
        Project,
        related_name="part_purchase_intents",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    supplier = models.CharField(max_length=255, blank=True, null=True)
    currency = models.CharField(max_length=10, default="EUR")
    price_status = models.CharField(max_length=30, choices=PRICE_STATUS_CHOICES, default=PRICE_ON_REQUEST)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    items = models.JSONField(default=default_part_purchase_items, blank=True)
    product_snapshot = models.JSONField(default=default_part_purchase_snapshot, blank=True)
    general_information = models.JSONField(default=default_part_purchase_general_information, blank=True)
    delivery_method = models.CharField(max_length=100, blank=True, null=True)
    delivery_address = models.JSONField(default=default_part_purchase_address, blank=True)
    invoice_address = models.JSONField(default=default_part_purchase_address, blank=True)
    payment_snapshot = models.JSONField(default=default_part_purchase_snapshot, blank=True)
    confirmation_code_hash = models.CharField(max_length=255, blank=True, null=True)
    code_sent_at = models.DateTimeField(null=True, blank=True)
    code_expires_at = models.DateTimeField(null=True, blank=True)
    code_verified_at = models.DateTimeField(null=True, blank=True)
    code_attempts = models.PositiveSmallIntegerField(default=0)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["buyer", "status", "created_at"],
                name="part_intent_buyer_status_idx",
            ),
            models.Index(
                fields=["buyer_company", "created_at"],
                name="part_intent_company_idx",
            ),
            models.Index(
                fields=["project", "status", "updated_at"],
                name="part_intent_project_status_idx",
            ),
            models.Index(fields=["reference"], name="part_intent_reference_idx"),
        ]

    def __str__(self):
        return self.reference


class MatrixComment(models.Model):
    TARGET_SOLUTION_DEMAND = "solution_demand"
    TARGET_REQUIREMENT = "requirement"
    TARGET_TRANSPORT = "transport"
    TARGET_AVAILABLE_PRODUCT_PART = "available_product_part"
    TARGET_MACHINE = "machine"
    TARGET_EQUIPMENT = "equipment"
    TARGET_BOM_PART = "bom_part"
    TARGET_COMPANY_SOLUTION_SPACE = "company_solution_space"
    TARGET_PROJECT_SOLUTION_SPACE = "project_solution_space"
    TARGET_CHOICES = (
        (TARGET_SOLUTION_DEMAND, "Solution Demand"),
        (TARGET_REQUIREMENT, "Requirement"),
        (TARGET_TRANSPORT, "Transport"),
        (TARGET_AVAILABLE_PRODUCT_PART, "Available Product Part"),
        (TARGET_MACHINE, "Machine"),
        (TARGET_EQUIPMENT, "Equipment"),
        (TARGET_BOM_PART, "Bill of Material Part"),
        (TARGET_COMPANY_SOLUTION_SPACE, "Company Solution Space"),
        (TARGET_PROJECT_SOLUTION_SPACE, "Project Solution Space"),
    )

    target_type = models.CharField(max_length=32, choices=TARGET_CHOICES)
    solution_demand = models.ForeignKey(
        SolutionDemand,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    requirement = models.ForeignKey(
        Requirement,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    transport = models.ForeignKey(
        Transport,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    available_product_part = models.ForeignKey(
        AvailableProductPart,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    machine = models.ForeignKey(
        Machine,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    bom_part = models.ForeignKey(
        BillOfMaterialPart,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    company_solution_space_item = models.ForeignKey(
        CompanySolutionSpaceItem,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    project_solution_space_item = models.ForeignKey(
        ProjectSolutionSpaceItem,
        on_delete=models.CASCADE,
        related_name="comments",
        null=True,
        blank=True,
    )
    sender = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="matrix_comments",
    )
    content = models.TextField(blank=True, default="")
    attachments = models.JSONField(default=list, blank=True)
    linked_objects = models.JSONField(default=list, blank=True)
    mentions = models.JSONField(default=list, blank=True)
    replied_to = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="replies",
    )
    is_deleted = models.BooleanField(default=False)
    is_edited = models.BooleanField(default=False)
    edited_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deleted_matrix_comments",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["target_type", "solution_demand", "created_at"]),
            models.Index(fields=["target_type", "requirement", "created_at"]),
            models.Index(fields=["target_type", "transport", "created_at"]),
            models.Index(fields=["target_type", "available_product_part", "created_at"]),
            models.Index(fields=["target_type", "machine", "created_at"]),
            models.Index(fields=["target_type", "equipment", "created_at"]),
            models.Index(fields=["target_type", "bom_part", "created_at"]),
            models.Index(
                fields=["target_type", "company_solution_space_item", "created_at"],
                name="matrix_comment_company_ss_idx",
            ),
            models.Index(
                fields=["target_type", "project_solution_space_item", "created_at"],
                name="matrix_comment_project_ss_idx",
            ),
            models.Index(fields=["replied_to", "created_at"]),
            models.Index(fields=["sender", "created_at"]),
        ]

    def __str__(self):
        target_id = (
            self.solution_demand_id
            or self.requirement_id
            or self.transport_id
            or self.available_product_part_id
            or self.machine_id
            or self.equipment_id
            or self.bom_part_id
            or self.company_solution_space_item_id
            or self.project_solution_space_item_id
        )
        preview = (self.content or "").strip()
        if len(preview) > 50:
            preview = f"{preview[:47]}..."
        return f"{self.target_type}:{target_id}:{self.sender_id}:{preview or 'comment'}"


class MatrixCommentReaction(models.Model):
    comment = models.ForeignKey(
        MatrixComment,
        on_delete=models.CASCADE,
        related_name="reactions",
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="matrix_comment_reactions",
    )
    key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("comment", "user", "key")
        indexes = [
            models.Index(fields=["comment", "key"]),
            models.Index(fields=["user", "created_at"]),
        ]

    def __str__(self):
        return f"{self.comment_id}:{self.key}:{self.user_id}"


class ProjectPhase(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='project_phases')
    name = models.CharField(max_length=255)
    date = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, related_name='created_project_phases', on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=255, default="open")
    amount = models.FloatField(null=True, blank=True)
    purchase_order = models.ForeignKey(
        "PurchaseOrder",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_payment_project_phases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

class ProjectFunding(models.Model):

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='fundings')
    name = models.CharField(max_length=255)
    budget = models.FloatField(default=1)
    documents = models.ManyToManyField('Document', related_name='fundings', blank=True)
    created_by = models.ForeignKey(User, related_name='created_project_fundings', on_delete=models.SET_NULL, null=True, blank=True)


    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

class PurchaseOrder(models.Model):
    class POType(models.TextChoices):
        INCOMING = "incoming", "Incoming PO"
        OUTGOING = "outgoing", "Outgoing PO"

    class DocumentType(models.TextChoices):
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        CHANGE_NOTICE = "change_notice", "Change Notice"

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='purchase_orders',
        null=True,
        blank=True,
    )
    company_profile = models.ForeignKey(
        CompanyProfile,
        on_delete=models.CASCADE,
        related_name='purchase_orders',
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='freelancer_purchase_orders',
        null=True,
        blank=True,
    )
    payment_milestones = models.ManyToManyField(
        Milestone,
        related_name='purchase_orders',
        blank=True,
    )
    documents = models.ManyToManyField(
        'Document',
        related_name='purchase_orders',
        blank=True,
    )
    linked_folders = models.ManyToManyField(
        'Folder',
        related_name='purchase_order_linked_folders',
        blank=True,
    )
    po_type = models.CharField(
        max_length=20,
        choices=POType.choices,
        default=POType.INCOMING,
    )
    document_type = models.CharField(
        max_length=20,
        choices=DocumentType.choices,
        default=DocumentType.PURCHASE_ORDER,
    )
    source_po = models.ForeignKey(
        "self",
        on_delete=models.PROTECT,
        related_name="change_notices",
        null=True,
        blank=True,
    )
    reference = models.CharField(max_length=255, db_index=True)
    customer_reference = models.CharField(max_length=255, null=True, blank=True)
    po_date = models.DateField()
    currency = models.CharField(max_length=10)
    status = models.CharField(max_length=255)
    amount_excl_vat = models.DecimalField(max_digits=12, decimal_places=2)
    change_amount_excl_vat = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=12)
    vat_amount = models.DecimalField(max_digits=12, decimal_places=2)
    amount_incl_vat = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.TextField(null=True, blank=True)
    receiving_user = models.ForeignKey(
        "ContactUser",
        on_delete=models.SET_NULL,
        related_name="purchase_orders_as_receiving_user",
        blank=True,
        null=True,
    )
    receiving_company = models.ForeignKey(
        "ContactCompany",
        on_delete=models.SET_NULL,
        related_name="purchase_orders_as_receiving_company",
        blank=True,
        null=True,
    )
    emitting_user = models.ForeignKey(
        "ContactUser",
        on_delete=models.SET_NULL,
        related_name="purchase_orders_as_emitting_user",
        blank=True,
        null=True,
    )
    emitting_company = models.ForeignKey(
        "ContactCompany",
        on_delete=models.SET_NULL,
        related_name="purchase_orders_as_emitting_company",
        blank=True,
        null=True,
    )
    created_by = models.ForeignKey(
        User,
        related_name='created_purchase_orders',
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    (
                        Q(project__isnull=False)
                        & Q(company_profile__isnull=True)
                        & Q(freelancer__isnull=True)
                    )
                    | (
                        Q(project__isnull=True)
                        & Q(company_profile__isnull=False)
                        & Q(freelancer__isnull=True)
                    )
                    | (
                        Q(project__isnull=True)
                        & Q(company_profile__isnull=True)
                        & Q(freelancer__isnull=False)
                    )
                ),
                name="purchase_order_single_owner_scope",
            ),
            models.CheckConstraint(
                check=(
                    Q(document_type="purchase_order", source_po__isnull=True)
                    | Q(document_type="change_notice", source_po__isnull=False)
                ),
                name="purchase_order_source_po_matches_document_type",
            ),
        ]

    def __str__(self):
        return self.reference


class PurchaseOrderLineItem(models.Model):
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        on_delete=models.CASCADE,
        related_name="line_items",
    )
    available_product = models.ForeignKey(
        AvailableProductPart,
        on_delete=models.SET_NULL,
        related_name="purchase_order_line_items",
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


class POReferenceCounter(models.Model):
    class Prefix(models.TextChoices):
        PO = "PO", "Purchase Order"
        CHN = "CHN", "Change Notice"

    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="po_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    freelancer = models.ForeignKey(
        User,
        related_name="po_reference_counters",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    period = models.CharField(max_length=6)
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
                name="po_reference_counter_single_owner",
            ),
            models.UniqueConstraint(
                fields=["company_profile", "period"],
                condition=Q(company_profile__isnull=False, freelancer__isnull=True),
                name="unique_po_reference_counter_company_period",
            ),
            models.UniqueConstraint(
                fields=["freelancer", "period"],
                condition=Q(company_profile__isnull=True, freelancer__isnull=False),
                name="unique_po_reference_counter_freelancer_period",
            ),
        ]
