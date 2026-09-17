from django.db import models
from django.utils import timezone
from django.contrib.postgres.fields import ArrayField
from models.models import User

from .project import Project, ProjectTeamMembers

class SiteReport(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="site_reports")
    date_time = models.DateTimeField(default=timezone.now)
    observer = models.ForeignKey(ProjectTeamMembers, on_delete=models.CASCADE, related_name="observed_site_reports")
    weather_condition = models.CharField(max_length=500, null=True, blank=True)
    location = models.CharField(max_length=500, null=True, blank=True)
    present = models.ManyToManyField(ProjectTeamMembers, related_name="presented_site_reports", blank=True)
    distribution = models.ManyToManyField(ProjectTeamMembers, related_name="distributed_site_reports", blank=True)
    name = models.CharField(max_length=100, default="Default Name")
    created_by = models.ForeignKey(User, related_name='created_site_reports', on_delete=models.SET_NULL, null=True, blank=True)
    assignees = models.ManyToManyField(User, related_name='assigned_site_reports', blank=True) # same as observer

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

class Observation(models.Model):
    site_report = models.ForeignKey(SiteReport, on_delete=models.CASCADE, related_name="observations")
    contractor = models.ForeignKey(ProjectTeamMembers, on_delete=models.CASCADE, related_name="observations")
    observation = models.CharField(max_length=1000)
    role = models.CharField(max_length=500, null=True, blank=True)
    rectification = models.CharField(max_length=1000, null=True, blank=True)
    timescale = models.CharField(max_length=500, null=True, blank=True)
    deadline = models.DateTimeField(null=True, blank=True)
    agreed = models.BooleanField(null=True, blank=True)
    photos = ArrayField(
        models.CharField(max_length=1000),
        blank=True,
        default=list,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
