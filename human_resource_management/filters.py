from django_filters import rest_framework as filters
from human_resource_management.models import LeaveRequest

class LeaveRequestFilter(filters.FilterSet):
    status = filters.CharFilter(method='filter_status', label='Status (comma separated)')
    type = filters.CharFilter(method='filter_type', label='Type (comma separated)')
    date_from = filters.DateFilter(field_name='start_date', lookup_expr='gte')
    date_to = filters.DateFilter(field_name='end_date', lookup_expr='lte')

    class Meta:
        model = LeaveRequest
        fields = ['status', 'type', 'employee', 'employee__user', 'date_from', 'date_to']

    def filter_status(self, queryset, name, value):
        if not value:
            return queryset
        
        criteria = [c.strip() for c in value.split(',') if c.strip()]
        if not criteria:
            return queryset
            
        return queryset.filter(status__in=criteria)

    def filter_type(self, queryset, name, value):
        if not value:
            return queryset
        
        criteria = [c.strip() for c in value.split(',') if c.strip()]
        if not criteria:
            return queryset
            
        return queryset.filter(type__in=criteria)
