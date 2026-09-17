from channels.generic.websocket import AsyncJsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth.models import AnonymousUser
from models.models import NotificationSeen

class NotificationConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("user")
        if not user or isinstance(user, AnonymousUser):
            return await self.close(code=4401)

        self.group_name = f"notifications_{user.id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()

        unread_list, unread_count = await database_sync_to_async(
            self._get_unread_payloads_and_count
        )(user)
        read_list = await database_sync_to_async(self._get_read_payloads)(user)

        await self.send_json({
            "type": "initial",
            "data": {
                "unread_count": unread_count,
                "unread_notifications": unread_list,
                "read_notifications": read_list,
            }
        })

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def notification_message(self, event):
        await self.send_json(event["payload"])

    def _get_unread_payloads_and_count(self, user):
        """
        Returns tuple(list_of_unread_payloads, unread_count)
        """
        qs = (
            NotificationSeen.objects
            .filter(user=user, status="unread", notification__backend_type="backend")
            .select_related("notification")
            .order_by("-notification__created_at")
        )
        payloads = []
        for seen in qs:
            notif = seen.notification
            payloads.append({
                "id": notif.id,
                "message": notif.message,
                "type": notif.type,
                "status": seen.status,
                "created_at": notif.created_at.isoformat(),
            })
        return payloads, qs.count()


    def _get_read_payloads(self, user):
        """
        Returns list_of_read_payloads
        """
        qs = (
            NotificationSeen.objects
            .filter(user=user, status="read", notification__backend_type="backend")
            .select_related("notification")
            .order_by("-notification__updated_at")
        )
        payloads = []
        for seen in qs:
            notif = seen.notification
            payloads.append({
                "id": notif.id,
                "message": notif.message,
                "type": notif.type,
                "status": seen.status,
                "created_at": notif.created_at.isoformat(),
                "read_at": seen.updated_at.isoformat(),
            })
        return payloads
