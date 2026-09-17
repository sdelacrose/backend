from django.db.models.signals import post_save
from django.dispatch import receiver
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from models.models import NotificationSeen

@receiver(post_save, sender=NotificationSeen)
def notify_user_via_ws(sender, instance, created, **kwargs):
    user = instance.user
    unread_ct = NotificationSeen.objects.filter(
        user=user, status="unread", notification__backend_type="backend"
    ).count()

    channel_layer = get_channel_layer()
    group_name = f"notifications_{user.id}"

    payload = {
        "id": instance.notification.id,
        "message": instance.notification.message,
        "type": instance.notification.type,
        "status": instance.status,
        "created_at": instance.notification.created_at.isoformat(),
        "unread_count": unread_ct,
        "backend_type": instance.notification.backend_type,
    }

    async_to_sync(channel_layer.group_send)(
        group_name,
        {
            "type": "notification.message",
            "payload": payload,
        }
    )