from django.db import models

from models.models import CompanyProfile, User


class AIToolAuditLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_CHOICES = (
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    )

    user = models.ForeignKey(
        User,
        related_name="ai_tool_audit_logs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    company_profile = models.ForeignKey(
        CompanyProfile,
        related_name="ai_tool_audit_logs",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    tool_name = models.CharField(max_length=255)
    request_payload = models.JSONField(default=dict, blank=True)
    result_count = models.PositiveIntegerField(default=0)
    source_object_refs = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=32, choices=STATUS_CHOICES, default=STATUS_SUCCESS)
    error_message = models.TextField(blank=True, default="")
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["company_profile", "created_at"]),
            models.Index(fields=["tool_name", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self):
        return f"{self.tool_name}:{self.user_id}:{self.status}"

