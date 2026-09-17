from rest_framework import serializers

from project_management.models import CompanyPublicHoliday


class CompanyPublicHolidaySerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyPublicHoliday
        fields = "__all__"
        read_only_fields = [
            "company_profile",
            "source",
            "country_code",
            "external_id",
            "synced_global_public_holiday_id",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def validate(self, attrs):
        company_profile = attrs.get(
            "company_profile",
            getattr(self.instance, "company_profile", None),
        )
        holiday_date = attrs.get("date", getattr(self.instance, "date", None))

        if company_profile and holiday_date:
            queryset = CompanyPublicHoliday.objects.filter(
                company_profile=company_profile,
                date=holiday_date,
            )
            if self.instance:
                queryset = queryset.exclude(pk=self.instance.pk)
            if queryset.exists():
                raise serializers.ValidationError(
                    {
                        "date": "A public holiday already exists for this company on this date.",
                    }
                )

        return attrs


class CompanyPublicHolidayImportRequestSerializer(serializers.Serializer):
    country_code = serializers.CharField(max_length=10)
    year = serializers.IntegerField(min_value=1900, max_value=2100)

    def validate_country_code(self, value):
        return value.strip().upper()


class CompanyPublicHolidayImportResultSerializer(serializers.Serializer):
    total_received = serializers.IntegerField()
    added_count = serializers.IntegerField()
    skipped_existing_count = serializers.IntegerField()
    skipped_non_national_count = serializers.IntegerField()
    skipped_invalid_count = serializers.IntegerField()
