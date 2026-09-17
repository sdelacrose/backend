from django.conf import settings
from django.db import models


class FundingSourceConfig(models.Model):
    authority = models.CharField(max_length=255)
    region = models.CharField(max_length=100)
    base_url = models.URLField(max_length=1000)
    allowed_hosts = models.JSONField(default=list)
    is_active = models.BooleanField(default=False, db_index=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["authority", "region", "id"]


class FundingOpportunity(models.Model):
    source = models.ForeignKey(FundingSourceConfig, related_name="opportunities", on_delete=models.PROTECT)
    external_id = models.CharField(max_length=255)
    current_revision = models.ForeignKey(
        "FundingOpportunityRevision", related_name="current_for", on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "external_id"], name="funding_source_external_unique")]


class FundingOpportunityRevision(models.Model):
    opportunity = models.ForeignKey(FundingOpportunity, related_name="revisions", on_delete=models.CASCADE)
    source_url = models.URLField(max_length=2000)
    retrieved_at = models.DateTimeField(db_index=True)
    title = models.CharField(max_length=500)
    summary = models.TextField(blank=True, default="")
    application_start = models.DateField(null=True, blank=True)
    application_end = models.DateField(null=True, blank=True)
    eligibility_rules = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    content_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-retrieved_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["opportunity", "content_hash"], name="funding_revision_hash_unique")]


class ProjectFundingMatch(models.Model):
    STATUS_SUGGESTED = "suggested"
    STATUS_SAVED = "saved"
    STATUS_DISMISSED = "dismissed"
    STATUS_CONVERTED = "converted"
    STATUS_CHOICES = tuple((value, value.title()) for value in (STATUS_SUGGESTED, STATUS_SAVED, STATUS_DISMISSED, STATUS_CONVERTED))
    ELIGIBILITY_CHOICES = (("eligible", "Eligible"), ("unknown", "Unknown"), ("disqualified", "Disqualified"))

    project = models.ForeignKey("project_management.Project", related_name="funding_opportunity_matches", on_delete=models.CASCADE)
    revision = models.ForeignKey(FundingOpportunityRevision, related_name="project_matches", on_delete=models.PROTECT)
    relevance_score = models.PositiveSmallIntegerField(default=0)
    eligibility_state = models.CharField(max_length=20, choices=ELIGIBILITY_CHOICES, default="unknown")
    match_reasons = models.JSONField(default=list)
    eligibility_conditions = models.JSONField(default=list)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SUGGESTED)
    converted_funding = models.OneToOneField(
        "project_management.ProjectFunding", related_name="source_opportunity_match",
        on_delete=models.SET_NULL, null=True, blank=True,
    )
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, related_name="created_funding_matches", on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-relevance_score", "-updated_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["project", "revision"], name="project_funding_revision_match_unique")]
