import os

from django.conf import settings
from rest_framework import serializers

from .models import ChatPushDevice, ChatRoom, Message, MessageRead
from models.models import User
from .object_links import (
    extract_linked_objects_from_text,
    filter_accessible_linked_objects,
    merge_linked_objects,
    normalize_linked_object_refs,
    normalize_linked_objects,
    resolve_linked_objects,
)

from .matrix_service import matrix_service
from .metrics import emit_chat_metric
from .utils import (
    are_users_connected,
    can_open_direct_chat,
    find_direct_chat_room,
    get_user_chat_rooms,
)


def build_chat_search_snippet(content, query, *, radius=48):
    text = str(content or "").strip()
    if not text:
        return ""

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return text[: radius * 2]

    lower_text = text.lower()
    lower_query = normalized_query.lower()
    match_index = lower_text.find(lower_query)

    if match_index < 0:
        return text[: radius * 2]

    start = max(0, match_index - radius)
    end = min(len(text), match_index + len(normalized_query) + radius)
    snippet = text[start:end].strip()

    if start > 0:
        snippet = f"...{snippet}"
    if end < len(text):
        snippet = f"{snippet}..."
    return snippet


def get_chat_room_type(room):
    if getattr(room, "is_group", False):
        return "group"
    if getattr(room, "is_direct", False):
        return "direct"
    if getattr(room, "company_id", None) and not getattr(room, "project_id", None):
        return "company"
    return "unknown"


def get_chat_room_display_name(room, request_user=None):
    room_type = get_chat_room_type(room)
    if room_type == "company":
        return (
            getattr(getattr(room, "company", None), "company_name", "") or room.name or "Company chat"
        )
    if room_type == "group":
        return room.name or "Group chat"
    if room_type == "direct":
        participants = list(getattr(room, "_prefetched_objects_cache", {}).get("participants", []) or room.participants.all())
        other_participants = [
            participant.full_name
            for participant in participants
            if request_user is None or participant.id != request_user.id
        ]
        return ", ".join([name for name in other_participants if name]) or room.name or "Direct chat"
    return room.name or ""

class MessageReadSerializer(serializers.ModelSerializer):
    user = serializers.SerializerMethodField()
    read_at = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S")

    class Meta:
        model = MessageRead
        fields = ["user", "read_at"]

    def get_user(self, obj):
        return {
            "id": obj.user.id,
            "name": obj.user.full_name,
            "user_image": obj.user.user_image
        }

class MessageSerializer(serializers.ModelSerializer):
    content = serializers.CharField(required=False, allow_blank=True)
    sender = serializers.IntegerField(source="sender_id", read_only=True)
    sender_username = serializers.CharField(
        source='sender.full_name',
        read_only=True
        )
    read_by = MessageReadSerializer(source='messageread_set', many=True, read_only=True)
    sender_image = serializers.CharField(
        source='sender.user_image',
        read_only=True
    )
    reactions = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()
    voice_note_public_url = serializers.SerializerMethodField()
    linked_object_refs = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        write_only=True,
    )
    mentions = serializers.ListField(
        child=serializers.DictField(),
        required=False,
    )

    class Meta:
        model = Message
        fields = [
            "id",
            "room",
            "sender",
            "sender_username",
            "content",
            "message_type",
            "voice_note_url",
            "voice_note_public_url",
            "voice_note_duration_ms",
            "attachments",
            "linked_objects",
            "linked_object_refs",
            "mentions",
            "created_at",
            "status",
            "replied_to",
            "reply_to",
            "read_by",
            "sender_image",
            "is_deleted",
            "is_edited",
            "edited_at",
            "reactions",
        ]

    def validate_room(self, value):
        request_user = self.context.get("user")
        if request_user is None:
            request = self.context.get("request")
            request_user = getattr(request, "user", None)

        if not request_user or not getattr(request_user, "is_authenticated", False):
            raise serializers.ValidationError("Authenticated user is required.")

        if value.is_group and value.is_closed:
            raise serializers.ValidationError("This group chat is closed.")

        accessible_room_ids = set(get_user_chat_rooms(request_user))
        if value.id in accessible_room_ids:
            return value

        if value.project:
            raise serializers.ValidationError("User is not part of the chat room's project.")
        if value.company:
            raise serializers.ValidationError("User is not part of the chat room's company.")
        raise serializers.ValidationError("User is not part of this chat room.")
        
    def validate_sender(self, value):
        request_user = self.context.get("user")
        if request_user is None:
            request = self.context.get("request")
            request_user = getattr(request, "user", None)
        if value != request_user:
            raise serializers.ValidationError("Message sender must be the authenticated user.")
        else:
            return value

    def _normalize_mentions(self, room, mentions_raw):
        if mentions_raw in (None, serializers.empty):
            return serializers.empty

        if not isinstance(mentions_raw, list):
            raise serializers.ValidationError({"mentions": "Mentions must be a list."})

        if not mentions_raw:
            return []

        if room is None:
            raise serializers.ValidationError({"mentions": "Room is required for mentions."})

        is_company_chat = bool(
            room.company_id and not room.project_id and not room.is_group and not room.is_direct
        )
        is_group_chat = bool(room.is_group)

        if not is_company_chat and not is_group_chat:
            raise serializers.ValidationError(
                {"mentions": "Mentions are only supported in company and group chats."}
            )

        ordered_user_ids = []
        for index, item in enumerate(mentions_raw, start=1):
            if not isinstance(item, dict):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} must be an object."}
                )

            raw_user_id = item.get("user_id") or item.get("id")
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} must include a valid user_id."}
                )

            if user_id not in ordered_user_ids:
                ordered_user_ids.append(user_id)

        allowed_users = User.objects.filter(
            id__in=ordered_user_ids,
            is_active=True,
            is_deleted=False,
            is_permanently_deleted=False,
        )

        if is_group_chat:
            allowed_users = allowed_users.filter(direct_chatrooms=room)
        else:
            allowed_users = allowed_users.filter(company_id=room.company_id)

        allowed_user_map = {user.id: user for user in allowed_users.distinct()}

        if len(allowed_user_map) != len(ordered_user_ids):
            raise serializers.ValidationError(
                {"mentions": "One or more mentioned users do not belong to this conversation."}
            )

        return [
            {
                "user_id": user_id,
                "name": allowed_user_map[user_id].full_name,
                "user_image": allowed_user_map[user_id].user_image or None,
            }
            for user_id in ordered_user_ids
        ]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        replied_to = attrs.get("replied_to")
        room = attrs.get("room") or getattr(self.instance, "room", None)
        message_type = attrs.get("message_type") or Message.TYPE_TEXT
        content = str(attrs.get("content") or "").strip()
        voice_note_url = str(attrs.get("voice_note_url") or "").strip()
        voice_note_duration_ms = attrs.get("voice_note_duration_ms")
        attachments_raw = attrs.get("attachments") or []
        linked_objects_raw = attrs.get("linked_objects") or []
        linked_object_refs_raw = attrs.get("linked_object_refs") or []
        mentions_raw = attrs.get("mentions", serializers.empty)
        request_user = self.context.get("user")
        if request_user is None:
            request = self.context.get("request")
            request_user = getattr(request, "user", None)

        if attachments_raw is None:
            attachments_raw = []
        if not isinstance(attachments_raw, list):
            raise serializers.ValidationError({"attachments": "Attachments must be a list."})

        try:
            normalized_linked_objects = normalize_linked_objects(linked_objects_raw)
        except ValueError as exc:
            raise serializers.ValidationError({"linked_objects": str(exc)})

        normalized_linked_object_refs = normalize_linked_object_refs(linked_object_refs_raw)
        normalized_linked_object_refs_from_text = extract_linked_objects_from_text(content)
        normalized_mentions = self._normalize_mentions(room, mentions_raw)
        merged_linked_objects = merge_linked_objects(
            normalized_linked_objects,
            normalized_linked_object_refs,
            normalized_linked_object_refs_from_text,
        )
        accessible_linked_objects = filter_accessible_linked_objects(
            merged_linked_objects,
            request_user,
        )

        max_linked_objects = int(getattr(settings, "CHAT_MAX_LINKED_OBJECTS", 5) or 5)
        if len(accessible_linked_objects) > max_linked_objects:
            raise serializers.ValidationError(
                {
                    "linked_objects": (
                        f"Maximum {max_linked_objects} linked objects are allowed per message."
                    )
                }
            )

        max_attachment_files = int(getattr(settings, "CHAT_ATTACHMENT_MAX_FILES", 5) or 5)
        max_total_attachment_size_mb = int(
            getattr(settings, "CHAT_ATTACHMENT_MAX_TOTAL_SIZE_MB", 25) or 25
        )
        max_total_attachment_size = max_total_attachment_size_mb * 1024 * 1024
        blocked_extensions = {
            str(ext).lower()
            for ext in getattr(
                settings,
                "CHAT_BLOCKED_ATTACHMENT_EXTENSIONS",
                [
                    ".exe",
                    ".bat",
                    ".cmd",
                    ".msi",
                    ".sql",
                    ".com",
                    ".scr",
                    ".pif",
                    ".vb",
                    ".vbs",
                    ".jar",
                ],
            )
        }

        if len(attachments_raw) > max_attachment_files:
            raise serializers.ValidationError(
                {
                    "attachments": (
                        f"Maximum {max_attachment_files} attachments are allowed per message."
                    )
                }
            )

        normalized_attachments = []
        total_attachment_size = 0

        for index, attachment in enumerate(attachments_raw, start=1):
            if not isinstance(attachment, dict):
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} must be an object."}
                )

            attachment_url = str(
                attachment.get("url")
                or attachment.get("file_url")
                or attachment.get("attachment_url")
                or ""
            ).strip()
            if not attachment_url:
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} URL is required."}
                )

            attachment_name = str(
                attachment.get("file_name") or attachment.get("name") or ""
            ).strip()
            if not attachment_name:
                attachment_name = os.path.basename(attachment_url.split("?")[0])
            if not attachment_name:
                attachment_name = f"attachment-{index}"

            attachment_extension = str(attachment.get("extension") or "").strip().lower()
            if not attachment_extension:
                attachment_extension = os.path.splitext(attachment_name)[1].lower()
            if attachment_extension and not attachment_extension.startswith("."):
                attachment_extension = f".{attachment_extension}"

            if attachment_extension and attachment_extension in blocked_extensions:
                raise serializers.ValidationError(
                    {
                        "attachments": (
                            f"Attachment #{index} uses a blocked file type ({attachment_extension})."
                        )
                    }
                )

            raw_size = attachment.get("size")
            if raw_size in (None, ""):
                attachment_size = 0
            else:
                try:
                    attachment_size = int(raw_size)
                except (TypeError, ValueError):
                    raise serializers.ValidationError(
                        {"attachments": f"Attachment #{index} size must be an integer."}
                    )

            if attachment_size < 0:
                raise serializers.ValidationError(
                    {"attachments": f"Attachment #{index} size must be non-negative."}
                )

            total_attachment_size += attachment_size
            normalized_attachments.append(
                {
                    "url": attachment_url,
                    "file_name": attachment_name,
                    "mime_type": str(attachment.get("mime_type") or "").strip().lower() or None,
                    "size": attachment_size,
                    "extension": attachment_extension or None,
                }
            )

        if total_attachment_size > max_total_attachment_size:
            raise serializers.ValidationError(
                {
                    "attachments": (
                        f"Total attachment size cannot exceed {max_total_attachment_size_mb}MB."
                    )
                }
            )

        if replied_to is None:
            pass
        elif room is None or replied_to.room_id != room.id:
            raise serializers.ValidationError({"replied_to": "Reply target must be in the same room."})

        if message_type == Message.TYPE_VOICE:
            if normalized_attachments:
                raise serializers.ValidationError(
                    {"attachments": "Voice notes cannot include file attachments."}
                )

            if merged_linked_objects:
                raise serializers.ValidationError(
                    {"linked_objects": "Voice notes cannot include linked objects."}
                )

            if not voice_note_url:
                raise serializers.ValidationError({"voice_note_url": "Voice note URL is required."})
            if voice_note_duration_ms is not None:
                try:
                    duration = int(voice_note_duration_ms)
                except (TypeError, ValueError):
                    raise serializers.ValidationError(
                        {"voice_note_duration_ms": "Duration must be a positive integer."}
                    )
                if duration <= 0:
                    raise serializers.ValidationError(
                        {"voice_note_duration_ms": "Duration must be a positive integer."}
                    )
                attrs["voice_note_duration_ms"] = duration

            attrs["content"] = content or "Voice note"
            attrs["attachments"] = []
            attrs["linked_objects"] = []
        else:
            if not content and not normalized_attachments and not accessible_linked_objects:
                raise serializers.ValidationError(
                    {"content": "Message content cannot be empty."}
                )

            attrs["content"] = content
            attrs["message_type"] = Message.TYPE_TEXT
            attrs["voice_note_url"] = None
            attrs["voice_note_duration_ms"] = None
            attrs["attachments"] = normalized_attachments
            attrs["linked_objects"] = accessible_linked_objects

        if normalized_mentions is not serializers.empty:
            attrs["mentions"] = normalized_mentions

        attrs.pop("linked_object_refs", None)

        return attrs

    def create(self, validated_data):
        request_user = self.context.get("user")
        if request_user is None:
            request = self.context.get("request")
            request_user = getattr(request, "user", None)

        if request_user is not None and getattr(request_user, "is_authenticated", False):
            validated_data["sender"] = request_user

        message = Message.objects.create(**validated_data)
        MessageRead.objects.create(message=message, user=request_user)
        matrix_service.sync_message(message)
        emit_chat_metric("chat_message_created", room_id=message.room_id)
        return message

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.is_deleted:
            representation["content"] = ""
            representation["attachments"] = []
            representation["linked_objects"] = []
            representation["mentions"] = []
            return representation

        attachments = representation.get("attachments") or []
        if isinstance(attachments, list):
            representation["attachments"] = self._with_public_attachment_urls(attachments)

        request = self.context.get("request")
        viewer = None
        if request is not None:
            viewer = getattr(request, "user", None)
        if viewer is None:
            viewer = self.context.get("user")

        try:
            representation["linked_objects"] = resolve_linked_objects(
                getattr(instance, "linked_objects", None) or [],
                viewer,
            )
        except Exception:
            representation["linked_objects"] = []

        return representation

    def _with_public_attachment_urls(self, attachments):
        request = self.context.get("request")
        normalized = []

        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue

            next_attachment = dict(attachment)
            attachment_url = str(next_attachment.get("url") or "").strip()
            if attachment_url and not attachment_url.startswith("http") and request is not None:
                next_attachment["url"] = request.build_absolute_uri(attachment_url)

            normalized.append(next_attachment)

        return normalized

    def get_reactions(self, obj):
        if getattr(obj, "is_deleted", False):
            return []

        request = self.context.get("request")
        viewer = None
        if request is not None:
            viewer = getattr(request, "user", None)
        if viewer is None:
            viewer = self.context.get("user")

        viewer_id = getattr(viewer, "id", None)

        grouped = {}
        for reaction in obj.reactions.all():
            key = reaction.key
            bucket = grouped.get(key)
            if not bucket:
                bucket = {
                    "emoji": key,
                    "count": 0,
                    "users": [],
                    "reacted_by_me": False,
                }
                grouped[key] = bucket

            bucket["count"] += 1
            bucket["users"].append(
                {
                    "id": reaction.user.id,
                    "name": reaction.user.full_name,
                    "user_image": reaction.user.user_image,
                }
            )
            if viewer_id is not None and reaction.user_id == viewer_id:
                bucket["reacted_by_me"] = True

        reactions = list(grouped.values())
        reactions.sort(key=lambda item: (-item["count"], item["emoji"]))
        return reactions

    def get_reply_to(self, obj):
        target = getattr(obj, "replied_to", None)
        if not target:
            return None

        content = "" if target.is_deleted else target.content
        return {
            "id": target.id,
            "sender": target.sender_id,
            "sender_username": target.sender.full_name,
            "content": content,
            "is_deleted": target.is_deleted,
            "created_at": target.created_at,
        }

    def get_voice_note_public_url(self, obj):
        voice_note_url = getattr(obj, "voice_note_url", None)
        if not voice_note_url:
            return None

        if str(voice_note_url).startswith("http"):
            return voice_note_url

        request = self.context.get("request")
        if request is not None:
            return request.build_absolute_uri(voice_note_url)
        return voice_note_url

class ParticipantUserSerializer(serializers.ModelSerializer):
    presence = serializers.SerializerMethodField()

    class Meta:
        model = User

        fields = ['id', 'first_name', 'last_name', 'user_image', 'company_id', 'presence']

    def get_presence(self, obj):
        online_user_id_set = self.context.get("online_user_id_set") or set()
        busy_user_id_set = self.context.get("busy_user_id_set") or set()
        return {
            "is_online": obj.id in online_user_id_set,
            "has_active_calendar_event": obj.id in busy_user_id_set,
        }


class ChatRoomHeaderMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.CharField(source="sender.full_name", read_only=True)
    attachment_count = serializers.SerializerMethodField()
    linked_object_type_label = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "id",
            "sender_name",
            "content",
            "message_type",
            "is_deleted",
            "attachment_count",
            "linked_object_type_label",
            "created_at",
        ]

    def get_attachment_count(self, obj):
        return len(obj.attachments or [])

    def get_linked_object_type_label(self, obj):
        linked_objects = obj.linked_objects or []
        if not linked_objects:
            return None
        return linked_objects[0].get("type_label")


class ChatRoomHeaderPreviewSerializer(serializers.ModelSerializer):
    room_type = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = [
            "id",
            "room_type",
            "display_name",
            "avatar_url",
            "unread_count",
            "last_message",
        ]

    def get_room_type(self, obj):
        return get_chat_room_type(obj)

    def get_display_name(self, obj):
        request = self.context.get("request")
        return get_chat_room_display_name(obj, getattr(request, "user", None))

    def get_avatar_url(self, obj):
        if not obj.is_direct:
            return None

        request = self.context.get("request")
        request_user_id = getattr(getattr(request, "user", None), "id", None)
        participant = next(
            (
                participant
                for participant in obj.participants.all()
                if participant.id != request_user_id
            ),
            None,
        )
        return participant.user_image if participant else None

    def get_unread_count(self, obj):
        return int(getattr(obj, "header_unread_count", 0) or 0)

    def get_last_message(self, obj):
        last_message_map = self.context.get("last_message_map") or {}
        last_message = last_message_map.get(obj.id)
        if not last_message:
            return None
        return ChatRoomHeaderMessageSerializer(last_message).data


class ChatPushDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatPushDevice
        fields = [
            "id",
            "token",
            "platform",
            "user_agent",
            "is_active",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "is_active",
            "last_seen_at",
            "created_at",
            "updated_at",
        ]

    def validate_token(self, value):
        token = str(value or "").strip()
        if not token:
            raise serializers.ValidationError("Token is required.")
        if len(token) > 512:
            raise serializers.ValidationError("Token is too long.")
        return token

class ChatRoomSerializer(serializers.ModelSerializer):
    last_message = serializers.SerializerMethodField()
    created_by = serializers.IntegerField(source='created_by_id', read_only=True)
    can_manage = serializers.SerializerMethodField()
    unread_count = serializers.SerializerMethodField()
    project_id = serializers.IntegerField(source="project.id", read_only=True)
    company_id = serializers.IntegerField(source="company.id", read_only=True)
    scope_label = serializers.SerializerMethodField()
    online_count = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = [
            'id',
            'name',
            'participants',
            'is_direct',
            'is_group',
            'is_closed',
            'closed_at',
            'closed_by',
            'created_by',
            'can_manage',
            'unread_count',
            'project_id',
            'company_id',
            'scope_label',
            'online_count',
            'created_at',
            'last_message',
        ]
        read_only_fields = ['last_message', 'is_closed', 'closed_at', 'closed_by']
        extra_kwargs = {
            'name': {'required': False, 'allow_blank': True},
        }

    def get_last_message(self, obj):
        last_message_map = self.context.get("last_message_map") or {}
        last_msg = last_message_map.get(obj.id)
        if last_msg:
            return MessageSerializer(last_msg, context=self.context).data

        last_msg = obj.messages.order_by('-created_at').first()
        if last_msg:
            return MessageSerializer(last_msg, context=self.context).data
        return None

    def get_can_manage(self, obj):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False
        return bool(obj.is_group and obj.created_by_id == user.id)

    def get_unread_count(self, obj):
        unread_count_map = self.context.get("unread_count_map") or {}
        return int(unread_count_map.get(obj.id, 0))

    def get_scope_label(self, obj):
        if obj.project_id and obj.project:
            return (obj.project.project_name or "").strip() or f"Project {obj.project_id}"
        if obj.company_id and obj.company:
            return (obj.company.company_name or "").strip() or "Company chat"
        return None

    def get_online_count(self, obj):
        if not obj.is_group:
            return 0

        request = self.context.get("request")
        request_user_id = getattr(getattr(request, "user", None), "id", None)
        online_user_id_set = self.context.get("online_user_id_set") or set()

        return sum(
            1
            for participant in obj.participants.all()
            if participant.id != request_user_id and participant.id in online_user_id_set
        )
    
    def validate(self, data):
        participants = data.get('participants') or []
        is_group = data.get('is_group', False)

        request_user = self.context.get("request").user
        if not request_user:
            raise serializers.ValidationError("Authenticated user not provided in serializer context.")
        if request_user.id not in [user.id for user in participants]:
            raise serializers.ValidationError("You are not allowed to create chats for other users.")

        if is_group:
            if len(participants) < 3:
                raise serializers.ValidationError("Group chat must have at least three participants.")
            for participant in participants:
                if participant.id == request_user.id:
                    continue
                if not are_users_connected(request_user, participant):
                    raise serializers.ValidationError(
                        "All group participants must have an active connection with the creator."
                    )
            return data

        if len(participants) != 2:
            raise serializers.ValidationError("Direct chat must have exactly two participants.")
        user1, user2 = participants
        if can_open_direct_chat(user1, user2):
            return data
        raise serializers.ValidationError("Users must share a company, share a project, or have an accepted connection.")

    def create(self, validated_data):
        request_user = self.context.get("request").user
        participants = validated_data.pop('participants', [])
        is_group = validated_data.pop('is_group', False)

        if is_group:
            room_name = validated_data.get('name') or f"Group chat ({len(participants)})"
            room = ChatRoom.objects.create(
                name=room_name,
                is_group=True,
                is_direct=False,
                created_by=request_user,
            )
            room.participants.set(participants)
            matrix_service.ensure_room(room)
            emit_chat_metric("chat_room_created", room_type="group")
            return room

        user1, user2 = participants
        existing_room = find_direct_chat_room(user1, user2)
        if existing_room:
            return existing_room

        room_name = f"{user1.full_name}-{user2.full_name}_direct_chat"
        room = ChatRoom.objects.create(
            name=room_name,
            is_direct=True,
            is_group=False,
            created_by=request_user,
        )
        room.participants.set(participants)
        matrix_service.ensure_room(room)
        emit_chat_metric("chat_room_created", room_type="direct")
        return room

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        representation["participants"] = ParticipantUserSerializer(
            instance.participants.all(),
            many=True,
            context=self.context,
        ).data
        return representation


class GlobalChatMessageSearchResultSerializer(serializers.ModelSerializer):
    message_id = serializers.IntegerField(source="id", read_only=True)
    room_id = serializers.IntegerField(read_only=True)
    room_type = serializers.SerializerMethodField()
    room_name = serializers.SerializerMethodField()
    sender_id = serializers.IntegerField(read_only=True)
    sender_name = serializers.CharField(source="sender.full_name", read_only=True)
    snippet = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "message_id",
            "room_id",
            "room_type",
            "room_name",
            "sender_id",
            "sender_name",
            "created_at",
            "snippet",
        ]

    def get_room_type(self, obj):
        return get_chat_room_type(obj.room)

    def get_room_name(self, obj):
        request = self.context.get("request")
        request_user = getattr(request, "user", None)
        return get_chat_room_display_name(obj.room, request_user=request_user)

    def get_snippet(self, obj):
        return build_chat_search_snippet(
            obj.content,
            self.context.get("query"),
        )


class RoomChatMessageSearchResultSerializer(serializers.ModelSerializer):
    message_id = serializers.IntegerField(source="id", read_only=True)
    sender_id = serializers.IntegerField(read_only=True)
    sender_name = serializers.CharField(source="sender.full_name", read_only=True)
    snippet = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = [
            "message_id",
            "created_at",
            "sender_id",
            "sender_name",
            "snippet",
        ]

    def get_snippet(self, obj):
        return build_chat_search_snippet(
            obj.content,
            self.context.get("query"),
        )


class MessageContextSerializer(serializers.Serializer):
    room_id = serializers.IntegerField()
    anchor_message_id = serializers.IntegerField()
    before = MessageSerializer(many=True)
    anchor = MessageSerializer()
    after = MessageSerializer(many=True)
    has_older = serializers.BooleanField()
    has_newer = serializers.BooleanField()
        
