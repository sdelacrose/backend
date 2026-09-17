from django.db import models
from django.utils import timezone
from model_utils import FieldTracker
from simple_history.models import HistoricalRecords

from models.models import User, CompanyProfile
from project_management.models import Document, Folder, ObjectAssessment, Project

class UserDashboardConfig(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="dashboard_configs")
    project_dashboard = models.JSONField(default=list)
    company_dashboard = models.JSONField(default=list)
    personal_dashboard = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username}'s Dashboard"


class CalendarEvent(models.Model):
    company_profile = models.ForeignKey(CompanyProfile, related_name="calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    freelancer = models.ForeignKey(User, related_name="freelancer_calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    project = models.ForeignKey(Project, related_name="calendar_events", on_delete=models.CASCADE, null=True, blank=True)
    
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, null=True)
    is_private = models.BooleanField(default=False)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='events', null=True, blank=True)
    outlook_event_id = models.TextField(blank=True, null=True)
    assignees = models.ManyToManyField(User, related_name='assigned_events', blank=True)
    documents = models.ManyToManyField(Document, related_name='attachments', blank=True)
    linked_folders = models.ManyToManyField(
        Folder,
        related_name='calendar_event_linked_folders',
        blank=True,
    )
    object_assessments = models.ManyToManyField(
        ObjectAssessment,
        related_name='calendar_events',
        blank=True,
    )
    meeting_link = models.URLField(max_length=2048, blank=True, null=True)
    room = models.CharField(max_length=255, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, related_name='created_calendar_events', on_delete=models.SET_NULL, null=True, blank=True)
    tracker = FieldTracker()

    def __str__(self):
        return self.name
