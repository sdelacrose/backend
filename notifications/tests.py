from datetime import timedelta
from types import SimpleNamespace

from django.contrib.contenttypes.models import ContentType
from django.core.paginator import Paginator
from django.test import TestCase
from django.utils import timezone

from models.models import Notification, NotificationSeen, User
from notifications.views import NotificationViewSet


class NotificationOrderingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="notification-order@example.com",
            password="password",
        )
        self.content_type = ContentType.objects.get_for_model(User)

    def create_notification(self, message, created_at):
        notification = Notification.objects.create(
            message=message,
            type="create",
            backend_type="backend",
            content_type=self.content_type,
            object_id=self.user.id,
        )
        notification.user.add(self.user)
        Notification.objects.filter(id=notification.id).update(
            created_at=created_at,
        )
        return notification

    def test_unread_notifications_are_ordered_before_read_notifications(self):
        now = timezone.now()
        read_newest = self.create_notification("read newest", now)
        unread_newest = self.create_notification(
            "unread newest",
            now - timedelta(days=1),
        )
        unread_oldest = self.create_notification(
            "unread oldest",
            now - timedelta(days=3),
        )
        read_oldest = self.create_notification(
            "read oldest",
            now - timedelta(days=4),
        )
        NotificationSeen.objects.bulk_create(
            [
                NotificationSeen(
                    user=self.user,
                    notification=read_newest,
                    status="read",
                ),
                NotificationSeen(
                    user=self.user,
                    notification=read_oldest,
                    status="read",
                ),
            ]
        )

        view = NotificationViewSet()
        view.request = SimpleNamespace(user=self.user)
        queryset = view.get_queryset()

        self.assertEqual(
            list(queryset.values_list("id", flat=True)),
            [
                unread_newest.id,
                unread_oldest.id,
                read_newest.id,
                read_oldest.id,
            ],
        )
        paginator = Paginator(queryset, 2)
        self.assertEqual(
            [notification.seen_status for notification in paginator.page(1)],
            ["unread", "unread"],
        )
        self.assertEqual(
            [notification.seen_status for notification in paginator.page(2)],
            ["read", "read"],
        )
