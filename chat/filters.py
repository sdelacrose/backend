import django_filters
from .models import Message
class MessageFilter(django_filters.FilterSet):
    room_project = django_filters.NumberFilter(field_name="room__project", lookup_expr="exact")
    room_company = django_filters.NumberFilter(field_name="room__company", lookup_expr="exact")
    room = django_filters.NumberFilter(field_name="room", lookup_expr="exact")

    class Meta:
        model = Message
        fields = ["room_project", "room_company", "room"]
