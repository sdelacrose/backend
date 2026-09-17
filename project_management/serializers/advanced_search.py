from rest_framework import serializers


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        unknown = sorted(set(data) - set(self.fields))
        if unknown:
            raise serializers.ValidationError(
                {key: "Unknown advanced-search field." for key in unknown}
            )
        return super().to_internal_value(data)


class AdvancedSearchScopeSerializer(StrictSerializer):
    type = serializers.ChoiceField(
        choices=["company", "project", "global"],
        default="company",
    )
    id = serializers.IntegerField(required=False, min_value=1)
    include_company = serializers.BooleanField(required=False)
    project_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )

    def validate(self, attrs):
        scope_type = attrs.get("type", "company")
        if scope_type == "project" and not attrs.get("id"):
            raise serializers.ValidationError({"id": "A project ID is required."})
        if scope_type == "global":
            if attrs.get("id") is not None:
                raise serializers.ValidationError(
                    {"id": "Project ID is not used for global search scope."}
                )
            if attrs.get("include_company") is None:
                attrs["include_company"] = True
            if attrs.get("project_ids") is None:
                attrs["project_ids"] = []
        else:
            attrs.pop("include_company", None)
            attrs.pop("project_ids", None)
        return attrs


class AdvancedSearchFieldFilterSerializer(StrictSerializer):
    field_ref = serializers.CharField(max_length=500)
    operator = serializers.ChoiceField(
        choices=["equals", "contains", "starts_with"],
        default="contains",
    )
    value = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)


class AdvancedSearchSemanticFieldFilterSerializer(StrictSerializer):
    semantic_id = serializers.CharField(max_length=500)
    operator = serializers.ChoiceField(
        choices=["equals", "contains", "starts_with"],
        default="contains",
    )
    value = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)


class AdvancedSearchLinkedObjectSerializer(StrictSerializer):
    type = serializers.CharField(max_length=64)
    id = serializers.IntegerField(required=False, min_value=1)


class AdvancedSearchFiltersSerializer(StrictSerializer):
    required_skills = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )
    provided_skills = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )
    fields = AdvancedSearchFieldFilterSerializer(many=True, required=False, default=list)
    semantic_fields = AdvancedSearchSemanticFieldFilterSerializer(
        many=True,
        required=False,
        default=list,
    )
    linked_object = AdvancedSearchLinkedObjectSerializer(required=False, default=dict)
    status = serializers.ListField(
        child=serializers.CharField(max_length=255),
        required=False,
        default=list,
    )
    author_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        default=list,
    )
    date_from = serializers.DateField(required=False, allow_null=True)
    date_to = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get("date_from") and attrs.get("date_to"):
            if attrs["date_from"] > attrs["date_to"]:
                raise serializers.ValidationError(
                    {"date_to": "The end date must be on or after the start date."}
                )
        return attrs


class AdvancedSearchQuerySerializer(StrictSerializer):
    query = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)
    scope = AdvancedSearchScopeSerializer(required=False, default={"type": "company"})
    result_types = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
    )
    filters = AdvancedSearchFiltersSerializer(required=False, default=dict)
    ordering = serializers.ChoiceField(
        choices=["relevance", "newest", "oldest", "title"],
        required=False,
        default="relevance",
    )
    page = serializers.IntegerField(required=False, default=1, min_value=1)
    page_size = serializers.IntegerField(required=False, default=10, min_value=1, max_value=100)

    def validate(self, attrs):
        if len((attrs.get("query") or "").strip()) < 2 and not attrs.get("filters"):
            raise serializers.ValidationError(
                {"query": "Enter at least two characters or apply a filter."}
            )
        return attrs


class AdvancedSearchBackfillSerializer(StrictSerializer):
    source_types = serializers.ListField(
        child=serializers.CharField(max_length=64),
        required=False,
        default=list,
    )
    after_id = serializers.IntegerField(required=False, default=0, min_value=0)
    limit = serializers.IntegerField(required=False, default=100, min_value=1, max_value=500)
