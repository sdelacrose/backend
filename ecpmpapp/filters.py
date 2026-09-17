from django_filters import rest_framework as filters


class ArrayOverlapFilter(filters.CharFilter):
    """Match rows containing at least one comma-separated query value."""

    def filter(self, queryset, value):
        selected_values = [
            item.strip()
            for item in str(value or "").split(",")
            if item.strip()
        ]
        if not selected_values:
            return queryset
        return queryset.filter(
            **{f"{self.field_name}__overlap": selected_values}
        )
