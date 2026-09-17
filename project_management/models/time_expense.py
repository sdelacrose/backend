from django.db import models
from django.utils import timezone
from django.core.exceptions import ValidationError
from model_utils import FieldTracker
from calendar import monthrange
from datetime import timedelta

from .contact import Contact
from .project import Milestone, Project, ActionItem, PurchaseOrder
from models.models import User, CompanyProfile
from django.contrib.postgres.fields import ArrayField
from ecpmpapp.constants import StatusType
from project_management.currency_converter import ECBCurrencyConverter
from project_management.utils import get_currency_from_record, symbol_to_currency_code
import logging

logger = logging.getLogger(__name__)


def default_start_date():
    return timezone.now().date()

STATUSES = [
    ('pending', 'Pending'),
    ('approved', 'Approved'),
    ('rejected', 'Rejected')
]


def get_week_start(date_value):
    return date_value - timedelta(days=date_value.weekday())


def get_week_end(date_value):
    return get_week_start(date_value) + timedelta(days=6)


def _get_root_action_item(action_item):
    return action_item.parent_action_item or action_item


def _get_action_item_project_context(action_item):
    root_action_item = _get_root_action_item(action_item)
    milestone = getattr(root_action_item, "milestone", None)
    project = getattr(milestone, "project", None)
    return project, milestone


class TimesheetSourceType(models.TextChoices):
    COMPANY_ACTION_ITEM = "company_action_item", "Company Action Item"
    COMPANY_CALENDAR_EVENT = "company_calendar_event", "Company Calendar Event"
    PROJECT = "project", "Project"
    PROJECT_PURCHASE_ORDER = "project_purchase_order", "Project Purchase Order"
    PROJECT_PHASE = "project_phase", "Project Phase"
    PROJECT_ACTION_ITEM = "project_action_item", "Project Action Item"


class MonthlyTimesheetStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


class MonthlyTimesheetFeedbackType(models.TextChoices):
    REJECTION = "rejection", "Rejection"
    RESUBMISSION = "resubmission", "Resubmission"
    COMMENT = "comment", "Comment"


class PublicHolidaySource(models.TextChoices):
    MANUAL = "manual", "Manual"
    IMPORTED = "imported", "Imported"
    GLOBAL_SYNC = "global_sync", "Global Sync"


class CompanyPublicHoliday(models.Model):
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="public_holidays",
        on_delete=models.CASCADE,
    )
    date = models.DateField()
    name = models.CharField(max_length=255)
    source = models.CharField(
        max_length=20,
        choices=PublicHolidaySource.choices,
        default=PublicHolidaySource.MANUAL,
    )
    country_code = models.CharField(max_length=10, null=True, blank=True)
    external_id = models.CharField(max_length=255, null=True, blank=True)
    synced_global_public_holiday_id = models.PositiveIntegerField(
        null=True,
        blank=True,
    )
    created_by = models.ForeignKey(
        User,
        related_name="created_public_holidays",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company_profile", "date"],
                name="unique_public_holiday_per_company_date",
            ),
        ]
        indexes = [
            models.Index(
                fields=["company_profile", "date"],
                name="pub_holiday_company_date_idx",
            ),
            models.Index(fields=["source"], name="pub_holiday_source_idx"),
        ]

    def __str__(self):
        return f"{self.name} ({self.date})"

    def clean(self):
        if not self.name:
            raise ValidationError("Public holiday name is required.")
        if not self.date:
            raise ValidationError("Public holiday date is required.")


class TimeCard(models.Model):
    freelancer = models.ForeignKey(User, related_name="time_cards", on_delete=models.CASCADE, null=True, blank=True)    
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="time_cards",  null=True, blank=True)
    company_profile = models.ForeignKey(CompanyProfile, related_name='time_cards', on_delete=models.CASCADE, null=True, blank=True)
    name = models.CharField(max_length=500)
    date = models.DateField(default=default_start_date)
    description = models.CharField(max_length=1000, null=True, blank=True)
    hours = models.FloatField()
    milestone = models.ForeignKey(
        Milestone, on_delete=models.CASCADE, related_name="time_cards", null=True, blank=True
    )
    action_item = models.ForeignKey(
        ActionItem, on_delete=models.CASCADE, related_name="time_cards",  null=True, blank=True
    )

    status = models.CharField(choices=STATUSES, default=StatusType.PENDING.value)
    rejection_reason = models.CharField(max_length=500, null=True, blank=True)
    approved_by = models.ForeignKey(User, related_name="approved_time_cards", on_delete=models.SET_NULL, null=True, blank=True)
    rejected_by = models.ForeignKey(User, related_name="rejected_time_cards", on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(User, related_name="created_time_cards", on_delete=models.SET_NULL, null=True, blank=True)
    timesheet_entry = models.ForeignKey(
        "TimesheetEntry",
        related_name="generated_time_cards",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    timesheet_submission = models.ForeignKey(
        "MonthlyTimesheetSubmission",
        related_name="generated_time_cards",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    is_generated_from_timesheet = models.BooleanField(default=False)
    is_invoiceable_from_timesheet = models.BooleanField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()
    def clean(self):
        """
        Custom validation to enforce the business rules for project, company_profile/freelancer, and related fields.
        """
        # Ensure only one of (project) or (company_profile/user) is set
        if self.project and (self.freelancer or self.company_profile):
            raise ValidationError("TimeCard can have either a project or a freelancer/company profile, not both.")
        
        if not self.project and not (self.freelancer or self.company_profile):
            raise ValidationError("TimeCard must have either a project or a freelancer/company profile.")

        # If user or company_profile is provided
        if (self.freelancer or self.company_profile) and self.milestone:
            raise ValidationError("TimeCard with a freelancer or company profile can only associate an action item, not a milestone.")

    def __str__(self):
        return f"{self.name} - {self.date}"


class ExpenseCard(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="expense_cards", null=True, blank=True)
    company_profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE, related_name="expense_cards", null=True, blank=True)
    freelancer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="expense_cards", null=True, blank=True)
    name = models.CharField(max_length=500)
    description = models.TextField()
    category = models.CharField(max_length=255)
    milestone = models.ForeignKey(Milestone, on_delete=models.CASCADE, related_name="expense_cards", null=True, blank=True)
    action_item = models.ForeignKey(ActionItem, on_delete=models.CASCADE, related_name="expense_cards", null=True, blank=True)
    rate = models.FloatField()
    quantity = models.FloatField()
    total = models.FloatField()
    expense_date = models.DateField(default=default_start_date)
    currency = models.CharField(max_length=10, null=True, blank=True)  # Storing original currency
    converted_amount = models.FloatField(null=True, blank=True)  # Storing converted amount
    converted_currency = models.CharField(max_length=10, null=True, blank=True)  # Storing target currency
    
    status = models.CharField(choices=STATUSES, default=StatusType.PENDING.value)
    rejection_reason = models.CharField(max_length=500, null=True, blank=True)
    approved_by = models.ForeignKey(User, related_name="approved_expense_cards", on_delete=models.SET_NULL, null=True, blank=True)
    rejected_by = models.ForeignKey(User, related_name="rejected_expense_cards", on_delete=models.SET_NULL, null=True, blank=True)
    created_by = models.ForeignKey(User, related_name="created_expense_cards", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    def save(self, *args, **kwargs):
        self.total = float(self.rate) * self.quantity
        super().save(*args, **kwargs)


class TimesheetActivity(models.Model):
    user = models.ForeignKey(
        User,
        related_name="timesheet_activities",
        on_delete=models.CASCADE,
    )
    week_start_date = models.DateField()
    week_end_date = models.DateField()
    source_type = models.CharField(
        max_length=40,
        choices=TimesheetSourceType.choices,
    )
    company_action_item = models.ForeignKey(
        ActionItem,
        related_name="company_timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    company_calendar_event = models.ForeignKey(
        "userapis.CalendarEvent",
        related_name="timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        Project,
        related_name="timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        related_name="timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project_milestone = models.ForeignKey(
        Milestone,
        related_name="timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project_action_item = models.ForeignKey(
        ActionItem,
        related_name="project_timesheet_activities",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    activity_code = models.CharField(max_length=255, blank=True, default="")
    activity_name = models.CharField(max_length=500)
    is_invoiceable = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        User,
        related_name="created_timesheet_activities",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["week_start_date", "id"]
        indexes = [
            models.Index(
                fields=["user", "week_start_date"],
                name="ts_act_user_week_idx",
            ),
            models.Index(
                fields=["user", "source_type"],
                name="ts_act_user_src_idx",
            ),
        ]

    def __str__(self):
        return f"{self.activity_name} ({self.week_start_date})"

    @property
    def target_object(self):
        if self.source_type == TimesheetSourceType.COMPANY_ACTION_ITEM:
            return self.company_action_item
        if self.source_type == TimesheetSourceType.COMPANY_CALENDAR_EVENT:
            return self.company_calendar_event
        if self.source_type == TimesheetSourceType.PROJECT:
            return self.project
        if self.source_type == TimesheetSourceType.PROJECT_PURCHASE_ORDER:
            return self.purchase_order
        if self.source_type == TimesheetSourceType.PROJECT_PHASE:
            return self.project_milestone
        if self.source_type == TimesheetSourceType.PROJECT_ACTION_ITEM:
            return self.project_action_item
        return None

    def clean(self):
        company_targets_present = any(
            [self.company_action_item_id, self.company_calendar_event_id]
        )

        if self.source_type == TimesheetSourceType.COMPANY_ACTION_ITEM:
            if not self.company_action_item_id:
                raise ValidationError(
                    "Company action item timesheet activity must reference a company action item."
                )
            if any(
                [
                    self.company_calendar_event_id,
                    self.project_id,
                    self.purchase_order_id,
                    self.project_milestone_id,
                    self.project_action_item_id,
                ]
            ):
                raise ValidationError(
                    "Company action item timesheet activity cannot reference project-related targets."
                )

        elif self.source_type == TimesheetSourceType.COMPANY_CALENDAR_EVENT:
            if not self.company_calendar_event_id:
                raise ValidationError(
                    "Company calendar event timesheet activity must reference a company calendar event."
                )
            if any(
                [
                    self.company_action_item_id,
                    self.project_id,
                    self.purchase_order_id,
                    self.project_milestone_id,
                    self.project_action_item_id,
                ]
            ):
                raise ValidationError(
                    "Company calendar event timesheet activity cannot reference project-related targets."
                )

        elif self.source_type == TimesheetSourceType.PROJECT:
            if not self.project_id:
                raise ValidationError(
                    "Project timesheet activity must reference a project."
                )
            if any(
                [
                    company_targets_present,
                    self.purchase_order_id,
                    self.project_milestone_id,
                    self.project_action_item_id,
                ]
            ):
                raise ValidationError(
                    "Project timesheet activity cannot reference other timesheet targets."
                )

        elif self.source_type == TimesheetSourceType.PROJECT_PHASE:
            if not self.project_milestone_id:
                raise ValidationError(
                    "Project phase timesheet activity must reference a project phase."
                )
            if any(
                [
                    company_targets_present,
                    self.purchase_order_id,
                    self.project_action_item_id,
                ]
            ):
                raise ValidationError(
                    "Project phase timesheet activity cannot reference other timesheet targets."
                )
            if self.project_id and self.project_milestone.project_id != self.project_id:
                raise ValidationError(
                    "Selected project phase must belong to the selected project."
                )

        elif self.source_type == TimesheetSourceType.PROJECT_ACTION_ITEM:
            if not self.project_action_item_id:
                raise ValidationError(
                    "Project action item timesheet activity must reference a project action item."
                )
            if any(
                [
                    company_targets_present,
                    self.purchase_order_id,
                ]
            ):
                raise ValidationError(
                    "Project action item timesheet activity cannot reference a purchase order or company targets."
                )

            action_item_project, action_item_milestone = _get_action_item_project_context(
                self.project_action_item
            )
            if not action_item_project:
                raise ValidationError(
                    "Selected project action item must belong to a project."
                )
            if self.project_id and action_item_project.id != self.project_id:
                raise ValidationError(
                    "Selected project action item must belong to the selected project."
                )
            if (
                self.project_milestone_id
                and action_item_milestone
                and action_item_milestone.id != self.project_milestone_id
            ):
                raise ValidationError(
                    "Selected project action item must belong to the selected project phase."
                )

        elif self.source_type == TimesheetSourceType.PROJECT_PURCHASE_ORDER:
            if not self.project or not self.purchase_order:
                raise ValidationError(
                    "Project purchase order timesheet activity must reference both a project and a purchase order."
                )
            if company_targets_present:
                raise ValidationError(
                    "Project purchase order timesheet activity cannot reference company targets."
                )
            if self.purchase_order.document_type == PurchaseOrder.DocumentType.CHANGE_NOTICE:
                raise ValidationError(
                    "Timesheet activity cannot reference a purchase order change notice."
                )
            if self.purchase_order.project_id != self.project_id:
                raise ValidationError(
                    "Timesheet purchase order must belong to the selected project."
                )
            if (
                self.project_milestone_id
                and self.project_milestone.project_id != self.project_id
            ):
                raise ValidationError(
                    "Selected project phase must belong to the selected project."
                )
            if self.project_action_item_id:
                action_item_project, action_item_milestone = _get_action_item_project_context(
                    self.project_action_item
                )
                if not action_item_project or action_item_project.id != self.project_id:
                    raise ValidationError(
                        "Selected project action item must belong to the selected project."
                    )
                if (
                    self.project_milestone_id
                    and action_item_milestone
                    and action_item_milestone.id != self.project_milestone_id
                ):
                    raise ValidationError(
                        "Selected project action item must belong to the selected project phase."
                    )
        else:
            raise ValidationError("Invalid timesheet activity source type.")

        expected_week_end = get_week_end(self.week_start_date)
        if self.week_end_date != expected_week_end:
            raise ValidationError("week_end_date must match the end of the selected week.")


class TimesheetEntry(models.Model):
    activity = models.ForeignKey(
        TimesheetActivity,
        related_name="entries",
        on_delete=models.CASCADE,
    )
    entry_date = models.DateField()
    hours = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["entry_date", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["activity", "entry_date"],
                name="unique_timesheet_entry_per_activity_date",
            ),
        ]
        indexes = [
            models.Index(fields=["entry_date"], name="timesheet_entry_date_idx"),
        ]

    def __str__(self):
        return f"{self.activity_id} - {self.entry_date}"

    def clean(self):
        if self.hours < 0:
            raise ValidationError("Timesheet entry hours cannot be negative.")
        if self.activity_id:
            if not (self.activity.week_start_date <= self.entry_date <= self.activity.week_end_date):
                raise ValidationError("Timesheet entry date must fall within the activity week.")


class MonthlyTimesheetSubmission(models.Model):
    user = models.ForeignKey(
        User,
        related_name="monthly_timesheet_submissions",
        on_delete=models.CASCADE,
    )
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    status = models.CharField(
        max_length=20,
        choices=MonthlyTimesheetStatus.choices,
        default=MonthlyTimesheetStatus.DRAFT,
    )
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        User,
        related_name="reviewed_monthly_timesheet_submissions",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["-year", "-month", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "month", "year"],
                name="unique_monthly_timesheet_submission_per_user_period",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "year", "month"],
                name="mts_user_period_idx",
            ),
            models.Index(fields=["status"], name="monthly_timesheet_status_idx"),
        ]

    def __str__(self):
        return f"{self.user_id} - {self.month}/{self.year}"

    @property
    def period_start(self):
        return timezone.datetime(self.year, self.month, 1).date()

    @property
    def period_end(self):
        return timezone.datetime(
            self.year,
            self.month,
            monthrange(self.year, self.month)[1],
        ).date()

    def clean(self):
        if self.month < 1 or self.month > 12:
            raise ValidationError("month must be between 1 and 12.")


class MonthlyTimesheetFeedback(models.Model):
    submission = models.ForeignKey(
        MonthlyTimesheetSubmission,
        related_name="feedback_entries",
        on_delete=models.CASCADE,
    )
    author = models.ForeignKey(
        User,
        related_name="monthly_timesheet_feedback_entries",
        on_delete=models.CASCADE,
    )
    feedback_type = models.CharField(
        max_length=20,
        choices=MonthlyTimesheetFeedbackType.choices,
        default=MonthlyTimesheetFeedbackType.REJECTION,
    )
    comment = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(
                fields=["submission", "created_at"],
                name="mts_feedback_sub_idx",
            ),
        ]

    def __str__(self):
        return f"{self.submission_id} - {self.feedback_type}"


class MonthlyTimesheetActivityDocumentLink(models.Model):
    user = models.ForeignKey(
        User,
        related_name="monthly_timesheet_activity_document_links",
        on_delete=models.CASCADE,
    )
    month = models.PositiveSmallIntegerField()
    year = models.PositiveSmallIntegerField()
    row_signature = models.CharField(max_length=500)
    source_type = models.CharField(
        max_length=40,
        choices=TimesheetSourceType.choices,
    )
    company_action_item = models.ForeignKey(
        ActionItem,
        related_name="monthly_timesheet_document_links_company_action_item",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    company_calendar_event = models.ForeignKey(
        "userapis.CalendarEvent",
        related_name="monthly_timesheet_document_links_calendar_event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project = models.ForeignKey(
        Project,
        related_name="monthly_timesheet_document_links",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    purchase_order = models.ForeignKey(
        PurchaseOrder,
        related_name="monthly_timesheet_document_links",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project_milestone = models.ForeignKey(
        Milestone,
        related_name="monthly_timesheet_document_links",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    project_action_item = models.ForeignKey(
        ActionItem,
        related_name="monthly_timesheet_document_links_project_action_item",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
    )
    document = models.ForeignKey(
        "project_management.Document",
        related_name="monthly_timesheet_activity_links",
        on_delete=models.CASCADE,
    )
    is_invoiceable = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    tracker = FieldTracker()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "month", "year", "row_signature", "document"],
                name="unique_monthly_timesheet_activity_document_link",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "year", "month", "row_signature"],
                name="mts_doc_link_usr_sig_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.user_id} {self.month}/{self.year} "
            f"{self.row_signature} -> {self.document_id}"
        )

    def clean(self):
        if self.month < 1 or self.month > 12:
            raise ValidationError("month must be between 1 and 12.")
    
class ProjectTime(models.Model):
    user = models.ForeignKey(User, related_name="time_expenses", on_delete=models.CASCADE)    
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="project_times", default=None
    )
    total_project_hours = models.FloatField(null=True, blank=True)
    total_general_hours = models.FloatField(null=True, blank=True)
    total_project_expenses = models.FloatField(null=True, blank=True)
    total_expenses = models.FloatField(null=True, blank=True)
    date = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)


class ProjectWork(models.Model):
    time_expense = models.ForeignKey(ProjectTime, related_name="project_works", on_delete=models.CASCADE)
    description = models.TextField(blank=True, null=True)
    hours = models.FloatField(blank=True, null=True)
    milestone = models.ForeignKey(
        Milestone, on_delete=models.CASCADE, related_name="project_works", null=True, blank=True
    )
    action_item = models.ForeignKey(
        ActionItem, on_delete=models.CASCADE, related_name="project_works",  null=True, blank=True
    )
    date = models.DateTimeField(default=timezone.now)
    files = ArrayField(models.CharField(max_length=255), blank=True, default=list)

class GeneralWork(models.Model):
    time_expense = models.ForeignKey(ProjectTime, related_name="general_works", on_delete=models.CASCADE)
    description = models.TextField(blank=True, null=True)
    hours = models.FloatField(blank=True, null=True)
    date = models.DateTimeField(default=timezone.now)
    category = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    amount = models.FloatField(blank=True, null=True)

class ProjectExpense(models.Model):
    time_expense = models.ForeignKey(ProjectTime, related_name="project_expenses", on_delete=models.CASCADE)
    expense_description = models.TextField(blank=True, null=True)
    expense_category = models.CharField(max_length=255)
    expense_rate = models.FloatField(blank=True, null=True)
    expense_quantity = models.IntegerField(blank=True, null=True)
    expense_total = models.FloatField()
    description = models.TextField(blank=True, null=True)
    files = ArrayField(models.CharField(max_length=255), blank=True, default=list)

class GeneralExpense(models.Model):
    time_expense = models.ForeignKey(ProjectTime, related_name="general_expenses", on_delete=models.CASCADE)
    expense_description = models.TextField(blank=True, null=True)
    expense_category = models.CharField(max_length=255)
    expense_rate = models.FloatField(blank=True, null=True)
    expense_quantity = models.IntegerField(blank=True, null=True)
    expense_total = models.FloatField()
    description = models.TextField(blank=True, null=True)
    contact = models.ForeignKey(
        Contact, on_delete=models.CASCADE, related_name="general_expenses", null=True, blank=True
    )
    files = ArrayField(models.CharField(max_length=255), blank=True, default=list)
