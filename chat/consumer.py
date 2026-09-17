import django
django.setup()
import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.conf import settings
from .models import Message, ChatRoom, MessageRead, MessageReaction, MatrixReactionMap
from .serializers import MessageSerializer
from ecpmpapp.constants import ChatStatusType
from django.utils import timezone

from .utils import get_user_chat_rooms
from .matrix_service import matrix_service
from .metrics import emit_chat_metric
from .tasks import (
    send_chat_message_push_notifications,
    send_chat_reaction_push_notifications,
)
from .object_links import resolve_linked_objects


logger = logging.getLogger(__name__)


class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        """Connect user to WebSocket"""
        user = self.scope["user"]
        self.room_id = self.scope["url_route"]["kwargs"]["room_id"]
        self.room_group_name = f"chat_{self.room_id}"
        
        # Check if user is part of the chat room
        is_group_member = await self.check_for_membership()
        if not is_group_member:
            logger.warning(
                "Chat socket rejected due to room access",
                extra={"room_id": self.room_id, "user_id": getattr(user, "id", None)},
            )
            await self.close()
            return

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, code):
        """Remove user from chat room"""
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        """Handle incoming messages"""
        if not text_data:
            return
        data = json.loads(text_data)
        event_type = data.get("type")

        user = self.scope["user"]

        if event_type == "chat_message":
            message = data["message"]
            reply_to_id = data.get("reply_to_id")
            message_type = data.get("message_type") or Message.TYPE_TEXT
            voice_note_url = data.get("voice_note_url")
            voice_note_duration_ms = data.get("voice_note_duration_ms")
            attachments = data.get("attachments") or []
            linked_objects = data.get("linked_objects") or []
            mentions = data.get("mentions") or []
            linked_object_refs = data.get("linked_object_refs") or []

            try:
                saved_message = await self.save_message(
                    user,
                    self.room_id,
                    message,
                    reply_to_id,
                    message_type,
                    voice_note_url,
                    voice_note_duration_ms,
                    attachments,
                    linked_objects,
                    mentions,
                    linked_object_refs,
                )
            except ValueError as exc:
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "error",
                            "message": str(exc),
                        }
                    )
                )
                return

            reply_to = self._build_reply_to_payload(saved_message.replied_to)

            # Broadcast message to room
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "id": saved_message.id,
                    "room": saved_message.room.id,
                    "type": "chat_message",
                    "content": saved_message.content,
                    "sender": user.id,
                    "sender_username": user.full_name,
                    "created_at": str(saved_message.created_at),
                    "status": ChatStatusType.DELIVERED.value,
                    "read_by": [],
                    "sender_image": user.user_image,
                    "is_deleted": False,
                    "is_edited": False,
                    "edited_at": None,
                    "reply_to": reply_to,
                    "message_type": saved_message.message_type,
                    "voice_note_url": saved_message.voice_note_url,
                    "voice_note_duration_ms": saved_message.voice_note_duration_ms,
                    "attachments": saved_message.attachments or [],
                    "linked_objects": saved_message.linked_objects or [],
                    "mentions": saved_message.mentions or [],
                },
            )

            try:
                await self.dispatch_chat_push_notifications(saved_message.id)
            except Exception:
                logger.exception(
                    "Failed to dispatch chat push notifications",
                    extra={"message_id": getattr(saved_message, "id", None)},
                )
        elif event_type == "edit_message":
            message_id = data.get("message_id")
            updated_content = data.get("message")
            updated_mentions = data.get("mentions", None)
            result = await self.edit_message_db(
                user,
                message_id,
                updated_content,
                updated_mentions,
            )

            if not result.get("ok"):
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "error",
                            "message": result.get("error", "Edit failed."),
                        }
                    )
                )
                return

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "edit_message",
                    "message_id": result["message_id"],
                    "content": result["content"],
                    "mentions": result.get("mentions", []),
                    "is_edited": result["is_edited"],
                    "edited_at": result["edited_at"],
                },
            )
        elif event_type == "read_message":
            message_id = data.get("message_id")
            if message_id:
                read_message, read = await self.mark_message_as_read(user, message_id)

                if read:
                    # Broadcast read status update to the group
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "read_message",
                            "reader": read_message.user.id,
                            "reader_image": read_message.user.user_image,
                            "reader_username": read_message.user.full_name,
                            "message_id": read_message.message_id,
                            "read_at": read_message.read_at.isoformat(),
                        },
                    )
        elif event_type == "delete_message":
            msg_id = data.get("message_id")
            if msg_id:
                success = await self.delete_message_db(user, msg_id)
                if success:
                    await self.channel_layer.group_send(
                        self.room_group_name,
                        {
                            "type": "delete_message",
                            "message_id": msg_id,
                        },
                    )
                else:
                    # Optionally, you could send an error back to the requester:
                    await self.send(text_data=json.dumps({
                        "type": "error",
                        "message": "Delete failed: not found or no permission."
                    }))
        elif event_type == "toggle_reaction":
            message_id = data.get("message_id")
            emoji = data.get("emoji")
            if not message_id or not emoji:
                await self.send(
                    text_data=json.dumps(
                        {
                            "type": "error",
                            "message": "Reaction failed: message_id and emoji are required.",
                        }
                    )
                )
                return

            result = await self.toggle_reaction_db(user, message_id, emoji)
            if not result.get("ok"):
                await self.send(text_data=json.dumps({"type": "error", "message": result.get("error")}))
                return

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "reaction_update",
                    "message_id": message_id,
                    "reactions": result.get("reactions", []),
                },
            )

            if result.get("reaction_added"):
                await self.dispatch_chat_reaction_push_notifications(
                    result.get("message_id") or message_id,
                    user.id,
                    result.get("emoji") or emoji,
                )


    async def chat_message(self, event):
        """Send message to WebSocket client"""
        resolved_linked_objects = await self.resolve_linked_objects_for_current_user(
            event.get("linked_objects", []),
        )

        await self.send(text_data=json.dumps({
            "type": "chat_message",
            "id": event["id"],
            "room": event["room"],
            "sender_username": event["sender_username"],
            "content": event["content"],
            "created_at": event["created_at"],
            "sender": event["sender"],
            "status": event["status"],
            "read_by": event["read_by"],
            "sender_image": event["sender_image"],
            "is_deleted": event.get("is_deleted", False),
            "is_edited": event.get("is_edited", False),
            "edited_at": event.get("edited_at"),
            "reply_to": event.get("reply_to"),
            "message_type": event.get("message_type", Message.TYPE_TEXT),
            "voice_note_url": event.get("voice_note_url"),
            "voice_note_duration_ms": event.get("voice_note_duration_ms"),
            "attachments": event.get("attachments", []),
            "linked_objects": resolved_linked_objects,
            "mentions": event.get("mentions", []),
        }))

    async def edit_message(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "edit_message",
                    "message_id": event.get("message_id"),
                    "content": event.get("content", ""),
                    "mentions": event.get("mentions", []),
                    "is_edited": event.get("is_edited", True),
                    "edited_at": event.get("edited_at"),
                }
            )
        )

    async def read_message(self, event):
        """Send read status update to WebSocket client"""
        await self.send(text_data=json.dumps({
            "type": "read_message",
            "reader": event["reader"],
            "reader_username": event["reader_username"],
            "message_id": event["message_id"],
            "read_at": event["read_at"],
            "reader_image": event["reader_image"],
        }))

    async def delete_message(self, event):
        """Send delete event to WebSocket client"""
        await self.send(
            text_data=json.dumps(
                {
                    "type": "delete_message",
                    "message_id": event.get("message_id"),
                }
            )
        )

    async def reaction_update(self, event):
        await self.send(
            text_data=json.dumps(
                {
                    "type": "reaction_update",
                    "message_id": event.get("message_id"),
                    "reactions": event.get("reactions", []),
                }
            )
        )

    def _build_reply_to_payload(self, replied_to):
        if not replied_to:
            return None

        return {
            "id": replied_to.id,
            "sender": replied_to.sender_id,
            "sender_username": replied_to.sender.full_name,
            "content": "" if replied_to.is_deleted else replied_to.content,
            "is_deleted": replied_to.is_deleted,
            "created_at": replied_to.created_at.isoformat(),
        }

    @database_sync_to_async
    def save_message(
        self,
        sender,
        room_id,
        message,
        reply_to_id=None,
        message_type=Message.TYPE_TEXT,
        voice_note_url=None,
        voice_note_duration_ms=None,
        attachments=None,
        linked_objects=None,
        mentions=None,
        linked_object_refs=None,
    ):
        """Save message to the database asynchronously"""
        room = ChatRoom.objects.get(id=room_id)

        payload = {
            'sender': sender.id,
            'room': room.id,
            'content': message,
            'message_type': message_type,
            'voice_note_url': voice_note_url,
            'voice_note_duration_ms': voice_note_duration_ms,
            'attachments': attachments or [],
            'linked_objects': linked_objects or [],
            'mentions': mentions or [],
            'linked_object_refs': linked_object_refs or [],
            'status': ChatStatusType.DELIVERED.value,
        }

        if reply_to_id is not None:
            payload['replied_to'] = reply_to_id

        serializer = MessageSerializer(data=payload, context={'user': sender})

        if serializer.is_valid():
            saved_message = serializer.save()
            saved_message = Message.objects.select_related(
                "room",
                "replied_to",
                "replied_to__sender",
            ).get(id=saved_message.id)
            return saved_message
        else:
            raise ValueError(f"Message data is invalid: {serializer.errors}")

    @database_sync_to_async
    def check_for_membership(self):
        """Check if user is part of the chat room asynchronously"""
        room_id = self.scope["url_route"]["kwargs"]["room_id"]
        request_user = self.scope.get("user")

        if not request_user or getattr(request_user, "is_anonymous", True):
            return False

        try:
            target_room_id = int(room_id)
        except (TypeError, ValueError):
            return False

        accessible_room_ids = set(get_user_chat_rooms(request_user))
        return target_room_id in accessible_room_ids

    @database_sync_to_async
    def mark_message_as_read(self, user, message_id):
        """Mark a message as read in the database"""
        try:
            message = Message.objects.get(id=message_id)
            read_message, created = MessageRead.objects.get_or_create(user=user, message=message)
            message.status = ChatStatusType.READ.value
            message.save()
            return read_message, created
        except Message.DoesNotExist:
            return None, False

    @database_sync_to_async
    def delete_message_db(self, user, message_id):
        try:
            message = Message.objects.get(id=message_id)
        except Message.DoesNotExist:
            return False

        try:
            if int(message.room_id) != int(self.room_id):
                return False
        except Exception:
            return False

        if message.sender_id != user.id:
            return False

        if message.is_deleted:
            return True

        message.is_deleted = True
        message.deleted_at = timezone.now()
        message.deleted_by = user
        message.content = ""
        message.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "content", "updated_at"])

        matrix_map = getattr(message, "matrix_message_map", None)
        if matrix_map:
            matrix_service.redact_event(
                matrix_map.matrix_room_id,
                matrix_map.matrix_event_id,
                reason="sharemundo_soft_delete",
            )
            emit_chat_metric("chat_message_redacted_in_matrix", message_id=message.id)
        return True

    @database_sync_to_async
    def edit_message_db(self, user, message_id, content, mentions=None):
        if not message_id:
            return {"ok": False, "error": "Edit failed: message_id is required."}

        try:
            message = Message.objects.select_related("room").get(id=message_id)
        except Message.DoesNotExist:
            return {"ok": False, "error": "Edit failed: message not found."}

        try:
            if int(message.room_id) != int(self.room_id):
                return {"ok": False, "error": "Edit failed: wrong room."}
        except Exception:
            return {"ok": False, "error": "Edit failed."}

        if message.sender_id != user.id:
            return {"ok": False, "error": "Edit failed: no permission."}

        if message.is_deleted:
            return {"ok": False, "error": "Edit failed: message was deleted."}

        cleaned_content = str(content or "").strip()
        if not cleaned_content:
            return {"ok": False, "error": "Edit failed: message cannot be empty."}

        serializer = MessageSerializer(
            message,
            data={
                "content": cleaned_content,
                "mentions": mentions,
            },
            partial=True,
            context={"user": user},
        )

        if not serializer.is_valid():
            return {
                "ok": False,
                "error": f"Edit failed: {serializer.errors}",
            }

        next_mentions = serializer.validated_data.get("mentions", message.mentions or [])

        if (
            message.content == cleaned_content
            and (message.mentions or []) == next_mentions
            and message.is_edited
        ):
            return {
                "ok": True,
                "message_id": message.id,
                "content": message.content,
                "mentions": message.mentions or [],
                "is_edited": message.is_edited,
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
            }

        updated_message = serializer.save()
        updated_message.content = cleaned_content
        updated_message.mentions = next_mentions
        updated_message.is_edited = True
        updated_message.edited_at = timezone.now()
        updated_message.save(
            update_fields=["content", "mentions", "is_edited", "edited_at", "updated_at"]
        )

        matrix_map = getattr(updated_message, "matrix_message_map", None)
        if matrix_map:
            edited_event_id = matrix_service.edit_message(
                matrix_map.matrix_room_id,
                matrix_map.matrix_event_id,
                cleaned_content,
            )
            if edited_event_id:
                matrix_map.matrix_event_id = edited_event_id
                matrix_map.save(update_fields=["matrix_event_id"])

        return {
            "ok": True,
            "message_id": updated_message.id,
            "content": updated_message.content,
            "mentions": updated_message.mentions or [],
            "is_edited": updated_message.is_edited,
            "edited_at": updated_message.edited_at.isoformat() if updated_message.edited_at else None,
        }

    @database_sync_to_async
    def toggle_reaction_db(self, user, message_id, emoji):
        try:
            message = Message.objects.select_related("room").get(id=message_id)
        except Message.DoesNotExist:
            return {"ok": False, "error": "Reaction failed: message not found."}

        try:
            if int(message.room_id) != int(self.room_id):
                return {"ok": False, "error": "Reaction failed: wrong room."}
        except Exception:
            return {"ok": False, "error": "Reaction failed."}

        if message.is_deleted:
            return {"ok": False, "error": "Reaction failed: message was deleted."}

        key = str(emoji)
        if len(key) > 255:
            return {"ok": False, "error": "Reaction failed: emoji too long."}

        existing = (
            MessageReaction.objects.filter(message=message, user=user, key=key)
            .select_related("matrix_reaction_map")
            .first()
        )

        if existing:
            matrix_map = getattr(existing, "matrix_reaction_map", None)
            if matrix_map:
                matrix_service.redact_event(
                    matrix_map.matrix_room_id,
                    matrix_map.matrix_event_id,
                    reason="reaction_removed",
                )
            existing.delete()
            reaction_added = False
        else:
            reaction = MessageReaction.objects.create(message=message, user=user, key=key)
            reaction_added = True

            matrix_message_map = getattr(message, "matrix_message_map", None)
            if matrix_message_map:
                reaction_event_id = matrix_service.send_reaction(
                    matrix_message_map.matrix_room_id,
                    matrix_message_map.matrix_event_id,
                    key,
                )
                if reaction_event_id:
                    MatrixReactionMap.objects.create(
                        reaction=reaction,
                        matrix_event_id=reaction_event_id,
                        matrix_room_id=matrix_message_map.matrix_room_id,
                        relates_to_event_id=matrix_message_map.matrix_event_id,
                        key=key,
                    )

        reactions = (
            MessageReaction.objects.filter(message=message)
            .select_related("user")
            .order_by("created_at")
        )
        grouped = {}
        for reaction in reactions:
            bucket = grouped.get(reaction.key)
            if not bucket:
                bucket = {"emoji": reaction.key, "count": 0, "users": []}
                grouped[reaction.key] = bucket
            bucket["count"] += 1
            bucket["users"].append(
                {
                    "id": reaction.user.id,
                    "name": reaction.user.full_name,
                    "user_image": reaction.user.user_image,
                }
            )

        payload = list(grouped.values())
        payload.sort(key=lambda item: (-item["count"], item["emoji"]))

        return {
            "ok": True,
            "message_id": message.id,
            "emoji": key,
            "reaction_added": reaction_added,
            "reactions": payload,
        }

    @database_sync_to_async
    def dispatch_chat_push_notifications(self, message_id):
        if getattr(settings, "CHAT_PUSH_USE_CELERY", True):
            send_chat_message_push_notifications.delay(message_id)
        else:
            send_chat_message_push_notifications.apply(args=[message_id])

    @database_sync_to_async
    def dispatch_chat_reaction_push_notifications(self, message_id, reactor_id, emoji):
        if getattr(settings, "CHAT_PUSH_USE_CELERY", True):
            send_chat_reaction_push_notifications.delay(message_id, reactor_id, emoji)
        else:
            send_chat_reaction_push_notifications.apply(args=[message_id, reactor_id, emoji])

    @database_sync_to_async
    def resolve_linked_objects_for_current_user(self, linked_objects):
        viewer = self.scope.get("user")
        return resolve_linked_objects(linked_objects, viewer)
