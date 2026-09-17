from django.conf import settings
from django.db import models


class WorkstationComplianceRule(models.Model):
    workstation = models.ForeignKey(
        "ProjectWorkstation", on_delete=models.CASCADE, related_name="compliance_rules"
    )
    requirement = models.ForeignKey("Requirement", on_delete=models.PROTECT)
    occurrence = models.UUIDField()
    title = models.CharField(max_length=255)
    definition = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class WorkstationComplianceEvaluation(models.Model):
    rule = models.ForeignKey(
        WorkstationComplianceRule, on_delete=models.CASCADE, related_name="evaluations"
    )
    rule_version = models.PositiveIntegerField()
    evidence = models.JSONField(default=dict)
    outcome = models.CharField(max_length=32)
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
