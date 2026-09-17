from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from project_management.models import ProjectFundingMatch


class FundingOpportunityMatchSerializer(serializers.ModelSerializer):
    authority = serializers.CharField(source="revision.opportunity.source.authority", read_only=True)
    region = serializers.CharField(source="revision.opportunity.source.region", read_only=True)
    title = serializers.CharField(source="revision.title", read_only=True)
    summary = serializers.CharField(source="revision.summary", read_only=True)
    source_url = serializers.URLField(source="revision.source_url", read_only=True)
    retrieved_at = serializers.DateTimeField(source="revision.retrieved_at", read_only=True)
    application_start = serializers.DateField(source="revision.application_start", read_only=True)
    application_end = serializers.DateField(source="revision.application_end", read_only=True)
    evidence = serializers.JSONField(source="revision.evidence", read_only=True)
    eligible = serializers.SerializerMethodField()
    is_stale = serializers.SerializerMethodField()
    source_available = serializers.SerializerMethodField()
    source_error = serializers.CharField(source="revision.opportunity.source.last_error", read_only=True)

    class Meta:
        model = ProjectFundingMatch
        fields = (
            "id",
            "project",
            "revision",
            "authority",
            "region",
            "title",
            "summary",
            "source_url",
            "retrieved_at",
            "application_start",
            "application_end",
            "evidence",
            "relevance_score",
            "eligibility_state",
            "eligible",
            "eligibility_conditions",
            "match_reasons",
            "is_stale",
            "source_available",
            "source_error",
            "status",
            "converted_funding",
            "created_at",
            "updated_at",
        )
        read_only_fields = fields

    def get_eligible(self, obj):
        return obj.eligibility_state == "eligible"

    def get_is_stale(self, obj):
        freshness_days = getattr(settings, "FUNDING_SOURCE_FRESHNESS_DAYS", 30)
        return obj.revision.retrieved_at < timezone.now() - timedelta(days=freshness_days)

    def get_source_available(self, obj):
        return not bool(obj.revision.opportunity.source.last_error)


class FundingOpportunityMatchStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=("saved", "dismissed"))
