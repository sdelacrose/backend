from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db.models import (
    Case,
    CharField,
    IntegerField,
    OuterRef,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend

from models.models import Notification, NotificationSeen
from notifications.serializers import NotificationSerializer, PreferenceSerializer
from notifications.filters import NotificationFilter
from models.models import Preference

class NotificationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    list: GET /api/notifications/            -> list unread & read notifications
    retrieve: GET /api/notifications/{id}/   -> get single notification
    mark_read: POST /api/notifications/{id}/mark_read/ -> mark as read
    """
    serializer_class = NotificationSerializer
    filterset_class = NotificationFilter
    filter_backends = [DjangoFilterBackend]
    queryset = Notification.objects.all()

    def get_queryset(self):
        user = self.request.user
        unread_status = NotificationSeen.STATUS_CHOICES[0][0]
        seen_status = NotificationSeen.objects.filter(
            user=user,
            notification=OuterRef("pk"),
        ).values("status")[:1]
        queryset = (
            self.queryset.filter(user=user, backend_type='backend')
            .annotate(
                seen_status=Coalesce(
                    Subquery(seen_status),
                    Value(unread_status),
                    output_field=CharField(),
                )
            )
        )
        return queryset.annotate(
            status_order=Case(
                When(seen_status=unread_status, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by('status_order', '-created_at', '-id')

    def _filter_queryset_without_status(self, request, queryset):
        data = request.query_params.copy()
        data.pop("status", None)
        return self.filterset_class(
            data=data,
            queryset=queryset,
            request=request,
        ).qs

    def _mark_notifications_as_read(self, user, notification_ids):
        if not notification_ids:
            return 0

        now = timezone.now()
        existing_statuses = {
            status.notification_id: status
            for status in NotificationSeen.objects.filter(
                user=user,
                notification_id__in=notification_ids,
            )
        }

        to_create = []
        to_update = []
        for notification_id in notification_ids:
            existing = existing_statuses.get(notification_id)
            if existing is None:
                to_create.append(
                    NotificationSeen(
                        user=user,
                        notification_id=notification_id,
                        status=NotificationSeen.STATUS_CHOICES[1][0],
                    )
                )
                continue

            if existing.status != NotificationSeen.STATUS_CHOICES[1][0]:
                existing.status = NotificationSeen.STATUS_CHOICES[1][0]
                existing.updated_at = now
                to_update.append(existing)

        if to_create:
            NotificationSeen.objects.bulk_create(to_create)

        if to_update:
            NotificationSeen.objects.bulk_update(to_update, ["status", "updated_at"])

        return len(notification_ids)

    @action(detail=True, methods=['patch'], url_path='mark_unimportant')
    def mark_unimportant(self, request, pk=None):
        """
        PATCH /api/notifications/{id}/mark_unimportant/
        Sets important=False for this notification.
        """
        try:
            notification = self.get_queryset().get(pk=pk)
        except Notification.DoesNotExist:
            return Response(
                {"error": "Invalid IDs or not yours."},
                status=status.HTTP_400_BAD_REQUEST
            )

        notification.important = not notification.important
        notification.save(update_fields=['important'])

        serializer = self.get_serializer(notification)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['patch'], url_path='mark_read_bulk')
    def mark_read_bulk(self, request):
        """
        Expects JSON:
          { "notification_ids": [1, 2, 3] }
        Marks each as 'read' for the current user.
        """
        ids = request.data.get('notification_ids')
        if not isinstance(ids, list):
            return Response(
                {"error": "`notification_ids` must be a list of IDs."},
                status=status.HTTP_400_BAD_REQUEST
            )
        query_set = self.get_queryset().filter(id__in=ids)
        if query_set.count() != len(ids):
            return Response(
                {"error": "Some IDs are invalid or not yours."},
                status=status.HTTP_400_BAD_REQUEST
            )

        marked_count = self._mark_notifications_as_read(
            request.user,
            list(query_set.values_list("id", flat=True)),
        )

        return Response(
            {"status": f"{marked_count} notifications marked as read"},
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['patch'], url_path='mark_all_read')
    def mark_all_read(self, request):
        query_set = self._filter_queryset_without_status(
            request,
            self.get_queryset(),
        ).filter(seen_status=NotificationSeen.STATUS_CHOICES[0][0])
        notification_ids = list(query_set.values_list("id", flat=True))
        marked_count = self._mark_notifications_as_read(request.user, notification_ids)

        return Response(
            {"status": f"{marked_count} notifications marked as read"},
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=['get'], url_path='unread_count')
    def unread_count(self, request):
        unread_count = (
            self._filter_queryset_without_status(request, self.get_queryset())
            .filter(seen_status=NotificationSeen.STATUS_CHOICES[0][0])
            .count()
        )
        return Response({"unread_count": unread_count}, status=status.HTTP_200_OK)


class PreferenceViewSet(viewsets.ModelViewSet):
    """
    list, retrieve, create, update user notification preferences
    """
    serializer_class = PreferenceSerializer
    queryset = Preference.objects.all()

    def get_queryset(self):
        user = self.request.user
        return self.queryset.filter(user=user)
