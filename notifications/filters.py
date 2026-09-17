import django_filters
from models.models import Notification, NotificationSeen

class NotificationFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(method='filter_status')
    important = django_filters.BooleanFilter(field_name='important')
    search = django_filters.CharFilter(field_name='message', lookup_expr='icontains', label='Search in message')

    class Meta:
        model = Notification
        fields = ['status', 'important', 'search']

    def filter_status(self, queryset, name, value):
        user = getattr(self.request, 'user', None)
        if user is None:
            return queryset.none()
        status_filter = value
        matching_ids = NotificationSeen.objects.filter(
            user=user,
            status=status_filter
        ).values_list("notification_id", flat=True)
        if value == 'read':
            return queryset.filter(id__in=matching_ids)
        elif value == 'unread':
            return queryset.exclude(id__in=matching_ids)
        return queryset
