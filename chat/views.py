from project_management.models.constants import COLLABORATIVE_SOLUTION
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework.response import Response
from rest_framework import viewsets, filters
from rest_framework import status
from rest_framework.decorators import action
from django.utils import timezone
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.db.models import (
    DateTimeField,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    Value,
)
from django.db.models import Count
from django.db.models.functions import Coalesce
from django.core.files.storage import default_storage
from django_filters.rest_framework import DjangoFilterBackend
from models.models import User
from userapis.models import CalendarEvent
from uuid import uuid4
import os
import logging

from .models import (
    ChatPushDevice,
    ChatPresence,
    Message,
    MessageRead,
    ChatRoom,
    MatrixMessageMap,
    MessageReaction,
)
from .serializers import (
    ChatPushDeviceSerializer,
    ChatRoomHeaderPreviewSerializer,
    ChatRoomSerializer,
    GlobalChatMessageSearchResultSerializer,
    MessageSerializer,
    MessageContextSerializer,
    RoomChatMessageSearchResultSerializer,
)
from .presence import (
    close_user_presence_session,
    get_online_user_ids,
    heartbeat_user_presence,
)
from .filters import MessageFilter
from .utils import (
    are_users_connected,
    can_access_group_chat,
    find_direct_chat_room,
    get_user_chat_rooms,
    has_access_to_messages,
)
from .metrics import emit_chat_metric
from .matrix_service import matrix_service
from .object_links import search_linked_objects as search_chat_linked_objects
from .read_service import count_unread_messages, mark_rooms_as_read
from .tasks import (
    send_chat_message_push_notifications,
    send_chat_reaction_push_notifications,
)
from ecpmpapp.constants import ChatStatusType


logger = logging.getLogger(__name__)


class MessageViewSet(viewsets.ModelViewSet):
    """
    API View for marking messages as read and retrieving chat history.
    """
    queryset = Message.objects.all()
    serializer_class = MessageSerializer
    filter_backends = [filters.OrderingFilter, DjangoFilterBackend]
    filterset_class = MessageFilter
    ordering_fields = ["created_at"]

    SEARCHABLE_ROOM_TYPE_MAP = {
        "company": Q(
            room__company__isnull=False,
            room__project__isnull=True,
            room__is_direct=False,
            room__is_group=False,
        ),
        "direct": Q(room__is_direct=True),
        "group": Q(room__is_group=True),
    }

    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return Message.objects.none()
        product_name = self.request.query_params.get("solution", "Energy Consultant Solution")
        if product_name in COLLABORATIVE_SOLUTION:
            products = COLLABORATIVE_SOLUTION
        else:
            products = [product_name]
        user_room_ids = get_user_chat_rooms(user)
        if not user_room_ids:
            return Message.objects.none()

        filters = Q(room_id__in=user_room_ids)
        project_filter = Q(room__project__isnull=True) | Q(room__project__solution__in=products)
        return (
            Message.objects.filter(filters)
            .filter(project_filter)
            .select_related(
                "room",
                "room__company",
                "room__project",
                "sender",
                "replied_to",
                "replied_to__sender",
            )
            .prefetch_related("messageread_set__user", "reactions__user")
            .distinct()
        )

    def _should_use_matrix_read(self):
        matrix_read_param = self.request.query_params.get("matrix_read")
        if str(matrix_read_param).strip().lower() not in {"1", "true", "yes"}:
            return False
        return bool(
            getattr(settings, "MATRIX_ENABLED", False)
            and getattr(settings, "MATRIX_READ_FROM_PROVIDER", True)
        )

    def _resolve_target_room(self, request):
        room_id = request.query_params.get("room")
        room_project = request.query_params.get("room_project")
        room_company = request.query_params.get("room_company")

        if room_id:
            return ChatRoom.objects.filter(id=room_id).first()
        if room_project:
            return ChatRoom.objects.filter(
                project_id=room_project,
                is_direct=False,
                is_group=False,
            ).first()
        if room_company:
            return ChatRoom.objects.filter(
                company_id=room_company,
                is_direct=False,
                is_group=False,
            ).first()
        return None

    def _get_supported_search_rooms_filter(self):
        return (
            self.SEARCHABLE_ROOM_TYPE_MAP["company"]
            | self.SEARCHABLE_ROOM_TYPE_MAP["direct"]
            | self.SEARCHABLE_ROOM_TYPE_MAP["group"]
        )

    def _validate_search_query(self, request):
        query = str(request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return None, Response(
                {"detail": "Query parameter 'q' must be at least 2 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return query, None

    def _list_from_local(self, request):
        messages = self.filter_queryset(self.get_queryset())

        read_param = request.query_params.get('read')
        if read_param is not None:
            if read_param.lower() not in ['true', 'false']:
                return Response(
                    {"detail": "Invalid value for 'read' parameter. Use 'true' or 'false'."},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if read_param.lower() == 'true':
                messages = messages.filter(read_by=request.user)
            else:
                messages = messages.exclude(read_by=request.user)

        page = self.paginate_queryset(messages)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            response_data = {
                "read": read_param,
                "total_pages": self.paginator.page.paginator.num_pages,
                "data": serializer.data
            }
            return self.get_paginated_response(response_data)

        serializer = self.get_serializer(messages, many=True)
        response_data = {
            "read": read_param,
            "total_pages": 1,
            "data": serializer.data
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def _list_from_matrix(self, request):
        room = self._resolve_target_room(request)
        if not room:
            return self._list_from_local(request)

        accessible_room_ids = set(get_user_chat_rooms(request.user))
        if room.id not in accessible_room_ids:
            return Response({"detail": "You do not have access to this room."}, status=status.HTTP_403_FORBIDDEN)

        room_map = getattr(room, "matrix_room_map", None) or matrix_service.ensure_room(room)
        if not room_map:
            return self._list_from_local(request)

        events = matrix_service.fetch_room_messages(room_map.matrix_room_id)
        if not events:
            return self._list_from_local(request)

        event_ids = [
            event.get("event_id")
            for event in events
            if event.get("type") == "m.room.message" and event.get("event_id")
        ]

        message_maps = (
            MatrixMessageMap.objects.filter(matrix_event_id__in=event_ids)
            .select_related("message", "message__sender", "message__replied_to", "message__replied_to__sender")
            .prefetch_related("message__read_by")
            .prefetch_related("message__reactions__user")
        )
        map_by_event_id = {item.matrix_event_id: item for item in message_maps}

        ordered_messages = []
        message_content_from_matrix = {}

        for event in events:
            if event.get("type") != "m.room.message":
                continue
            event_id = event.get("event_id")
            matrix_map = map_by_event_id.get(event_id)
            if not matrix_map:
                continue

            message = matrix_map.message
            ordered_messages.append(message)

            content = event.get("content", {})
            new_content = content.get("m.new_content")
            body = None
            if isinstance(new_content, dict):
                body = new_content.get("body")
            if body is None:
                body = content.get("body")
            if body is not None:
                has_local_attachments = bool(getattr(message, "attachments", None))
                has_local_linked_objects = bool(getattr(message, "linked_objects", None))
                if not (
                    (has_local_attachments or has_local_linked_objects)
                    and not (message.content or "").strip()
                ):
                    message_content_from_matrix[message.id] = body

        if not ordered_messages:
            return self._list_from_local(request)

        read_param = request.query_params.get('read')
        if read_param is not None and read_param.lower() not in ['true', 'false']:
            return Response(
                {"detail": "Invalid value for 'read' parameter. Use 'true' or 'false'."},
                status=status.HTTP_400_BAD_REQUEST
            )

        if read_param is not None:
            message_ids = [message.id for message in ordered_messages]
            read_message_ids = set(
                MessageRead.objects.filter(user=request.user, message_id__in=message_ids)
                .values_list("message_id", flat=True)
            )

            if read_param.lower() == "true":
                ordered_messages = [
                    message for message in ordered_messages if message.id in read_message_ids
                ]
            else:
                ordered_messages = [
                    message for message in ordered_messages if message.id not in read_message_ids
                ]

        ordering = request.query_params.get("ordering", "created_at")
        if ordering.startswith("-"):
            ordered_messages = list(reversed(ordered_messages))

        page = self.paginate_queryset(ordered_messages)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            serialized_data = serializer.data
            for item in serialized_data:
                message_id = item.get("id")
                if not item.get("is_deleted") and message_id in message_content_from_matrix:
                    item["content"] = message_content_from_matrix[message_id]

            response_data = {
                "read": read_param,
                "total_pages": self.paginator.page.paginator.num_pages,
                "data": serialized_data,
            }
            return self.get_paginated_response(response_data)

        serializer = self.get_serializer(ordered_messages, many=True)
        serialized_data = serializer.data
        for item in serialized_data:
            message_id = item.get("id")
            if not item.get("is_deleted") and message_id in message_content_from_matrix:
                item["content"] = message_content_from_matrix[message_id]

        return Response(
            {
                "read": read_param,
                "total_pages": 1,
                "data": serialized_data,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="search")
    def search(self, request):
        query, error_response = self._validate_search_query(request)
        if error_response is not None:
            return error_response

        room_type = str(request.query_params.get("room_type") or "").strip().lower()
        queryset = (
            self.get_queryset()
            .filter(self._get_supported_search_rooms_filter())
            .filter(is_deleted=False, content__icontains=query)
            .select_related("room", "room__company", "sender")
            .prefetch_related("room__participants")
            .order_by("-created_at", "-id")
            .distinct()
        )

        if room_type:
            room_filter = self.SEARCHABLE_ROOM_TYPE_MAP.get(room_type)
            if room_filter is None:
                return Response(
                    {"detail": "room_type must be one of: company, direct, group."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            queryset = queryset.filter(room_filter)

        page = self.paginate_queryset(queryset)
        serializer_context = {
            "request": request,
            "query": query,
        }
        serializer_class = GlobalChatMessageSearchResultSerializer

        if page is not None:
            serializer = serializer_class(page, many=True, context=serializer_context)
            return self.get_paginated_response(serializer.data)

        serializer = serializer_class(queryset, many=True, context=serializer_context)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['patch'])
    def mark_read_bulk(self, request):
        """Mark MULTIPLE messages as read using an array of IDs"""
        request_user = request.user
        message_ids = request.data.get('message_ids', [])
        
        if not isinstance(message_ids, list):
            return Response(
                {"error": "Expected a list of message IDs in `message_ids`"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        #TODO: Saqlain to add validation to make sure if user can access these messages and then update its status to read.
        unique_message_ids = list(dict.fromkeys(message_ids))
        messages = Message.objects.filter(id__in=unique_message_ids)

        # check if user has access to all messages
        if not has_access_to_messages(request_user, messages):
            return Response(
                {"error": "User does not have access to these messages"},
                status=status.HTTP_403_FORBIDDEN
            )

        if messages.count() != len(unique_message_ids):
            return Response(
                {"error": "Some of the provided message IDs do not exist"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        message_reads = [
            MessageRead(user=request.user, message=message)
            for message in messages
        ]
        MessageRead.objects.bulk_create(message_reads, ignore_conflicts=True)
        
        messages.update(status=ChatStatusType.READ.value)

        return Response(
            {"status": f"{len(unique_message_ids)} messages marked as read"},
            status=status.HTTP_200_OK
        )

    @action(detail=False, methods=["post"], url_path="voice-notes")
    def upload_voice_note(self, request):
        audio_file = request.FILES.get("file")
        room_id = request.data.get("room_id")
        duration_ms = request.data.get("duration_ms")

        if not audio_file:
            return Response(
                {"error": "file is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not room_id:
            return Response(
                {"error": "room_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "room_id must be a valid integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        accessible_room_ids = set(get_user_chat_rooms(request.user))
        if room_id not in accessible_room_ids:
            return Response(
                {"error": "You do not have access to this room."},
                status=status.HTTP_403_FORBIDDEN,
            )

        content_type = (audio_file.content_type or "").lower()
        allowed_mime_types = {
            "audio/webm",
            "audio/ogg",
            "audio/mpeg",
            "audio/mp3",
            "audio/wav",
            "audio/x-wav",
            "audio/mp4",
            "audio/aac",
        }
        if content_type and content_type not in allowed_mime_types:
            return Response(
                {"error": "Unsupported audio format."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_size_mb = int(getattr(settings, "CHAT_VOICE_NOTE_MAX_SIZE_MB", 25) or 25)
        max_size_bytes = max_size_mb * 1024 * 1024
        if audio_file.size > max_size_bytes:
            return Response(
                {"error": f"Voice note is too large. Maximum size is {max_size_mb}MB."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        extension = os.path.splitext(audio_file.name or "")[1].lower()
        if extension and extension not in {".webm", ".ogg", ".mp3", ".wav", ".m4a", ".aac", ".mp4"}:
            return Response(
                {"error": "Unsupported audio file extension."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        normalized_duration = None
        if duration_ms not in (None, ""):
            try:
                normalized_duration = int(duration_ms)
            except (TypeError, ValueError):
                return Response(
                    {"error": "duration_ms must be a positive integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if normalized_duration <= 0:
                return Response(
                    {"error": "duration_ms must be a positive integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        file_name = f"chat/voice_notes/{room_id}/{uuid4().hex}{extension or '.webm'}"
        stored_path = default_storage.save(file_name, audio_file)
        voice_note_url = default_storage.url(stored_path)
        if not str(voice_note_url).startswith("http"):
            voice_note_url = request.build_absolute_uri(voice_note_url)

        return Response(
            {
                "url": voice_note_url,
                "duration_ms": normalized_duration,
                "mime_type": content_type,
                "size": audio_file.size,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="attachments")
    def upload_attachments(self, request):
        room_id = request.data.get("room_id")
        files = list(request.FILES.getlist("files") or [])

        if not files:
            single_file = request.FILES.get("file")
            if single_file:
                files = [single_file]

        if not files:
            return Response(
                {"error": "At least one file is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not room_id:
            return Response(
                {"error": "room_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "room_id must be a valid integer"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        accessible_room_ids = set(get_user_chat_rooms(request.user))
        if room_id not in accessible_room_ids:
            return Response(
                {"error": "You do not have access to this room."},
                status=status.HTTP_403_FORBIDDEN,
            )

        max_attachment_files = int(getattr(settings, "CHAT_ATTACHMENT_MAX_FILES", 5) or 5)
        if len(files) > max_attachment_files:
            return Response(
                {
                    "error": (
                        f"You can upload at most {max_attachment_files} files per message."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        max_total_size_mb = int(getattr(settings, "CHAT_ATTACHMENT_MAX_TOTAL_SIZE_MB", 25) or 25)
        max_total_size_bytes = max_total_size_mb * 1024 * 1024
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

        validated_files = []
        total_size = 0

        for upload in files:
            original_name = os.path.basename(upload.name or "attachment")
            extension = os.path.splitext(original_name)[1].lower()

            if extension and extension in blocked_extensions:
                return Response(
                    {
                        "error": (
                            f"File type '{extension}' is not allowed."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            file_size = int(upload.size or 0)
            total_size += file_size
            if total_size > max_total_size_bytes:
                return Response(
                    {
                        "error": (
                            f"Total attachments size cannot exceed {max_total_size_mb}MB."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            validated_files.append(
                {
                    "upload": upload,
                    "original_name": original_name,
                    "extension": extension,
                    "size": file_size,
                    "mime_type": (upload.content_type or "").lower() or None,
                }
            )

        uploaded_attachments = []
        for file_item in validated_files:
            extension = file_item["extension"]
            storage_path = default_storage.save(
                f"chat/attachments/{room_id}/{uuid4().hex}{extension}",
                file_item["upload"],
            )
            attachment_url = default_storage.url(storage_path)
            if not str(attachment_url).startswith("http"):
                attachment_url = request.build_absolute_uri(attachment_url)

            uploaded_attachments.append(
                {
                    "url": attachment_url,
                    "file_name": file_item["original_name"],
                    "mime_type": file_item["mime_type"],
                    "size": file_item["size"],
                    "extension": extension or None,
                }
            )

        return Response(
            {
                "attachments": uploaded_attachments,
                "total_size": total_size,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], url_path="linked-objects/search")
    def search_linked_objects(self, request):
        room_id = request.data.get("room_id")
        query = request.data.get("query") or ""
        object_types = request.data.get("object_types")
        limit = request.data.get("limit", 20)

        room = None
        if room_id not in (None, ""):
            try:
                room = ChatRoom.objects.filter(id=int(room_id)).first()
            except (TypeError, ValueError):
                return Response(
                    {"error": "room_id must be a valid integer"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        if room is None:
            room = self._resolve_target_room(request)

        if room is None:
            return Response(
                {"error": "room_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        accessible_room_ids = set(get_user_chat_rooms(request.user))
        if room.id not in accessible_room_ids:
            return Response(
                {"error": "You do not have access to this room."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            results = search_chat_linked_objects(
                request.user,
                room,
                query=query,
                object_types=object_types,
                limit=limit,
            )
        except ValueError as exc:
            return Response(
                {"error": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "results": results,
            },
            status=status.HTTP_200_OK,
        )

    def list(self, request):
        """Retrieve chat history for a given room with read/unread filtering"""
        if self._should_use_matrix_read():
            return self._list_from_matrix(request)
        return self._list_from_local(request)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        message = serializer.save()
        message = Message.objects.select_related(
            "room",
            "sender",
            "replied_to",
            "replied_to__sender",
        ).get(id=message.id)

        self._broadcast_created_message(message)
        self._dispatch_created_message_push(message.id)

        response_serializer = self.get_serializer(message)
        headers = self.get_success_headers(response_serializer.data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED, headers=headers)

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

    def _broadcast_created_message(self, message):
        channel_layer = get_channel_layer()
        if not channel_layer:
            return

        try:
            async_to_sync(channel_layer.group_send)(
                f"chat_{message.room_id}",
                {
                    "id": message.id,
                    "room": message.room_id,
                    "type": "chat_message",
                    "content": message.content,
                    "sender": message.sender_id,
                    "sender_username": message.sender.full_name,
                    "created_at": str(message.created_at),
                    "status": ChatStatusType.DELIVERED.value,
                    "read_by": [],
                    "sender_image": message.sender.user_image,
                    "is_deleted": False,
                    "is_edited": False,
                    "edited_at": None,
                    "reply_to": self._build_reply_to_payload(message.replied_to),
                    "message_type": message.message_type,
                    "voice_note_url": message.voice_note_url,
                    "voice_note_duration_ms": message.voice_note_duration_ms,
                    "attachments": message.attachments or [],
                    "linked_objects": message.linked_objects or [],
                },
            )
        except Exception:
            logger.exception(
                "Failed to broadcast created message over websocket",
                extra={"message_id": message.id, "room_id": message.room_id},
            )

    def _dispatch_created_message_push(self, message_id):
        try:
            if getattr(settings, "CHAT_PUSH_USE_CELERY", True):
                send_chat_message_push_notifications.delay(message_id)
            else:
                send_chat_message_push_notifications.apply(args=[message_id])
        except Exception:
            logger.exception(
                "Failed to dispatch chat push notifications from message create",
                extra={"message_id": message_id},
            )

    def destroy(self, request, *args, **kwargs):
        message = self.get_object()

        matrix_map = getattr(message, "matrix_message_map", None)

        if message.sender_id != request.user.id:
            return Response(
                {"error": "Only the message sender can delete this message."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if message.is_deleted:
            return Response(status=status.HTTP_204_NO_CONTENT)

        message.is_deleted = True
        message.deleted_at = timezone.now()
        message.deleted_by = request.user
        message.content = ""
        message.save(update_fields=["is_deleted", "deleted_at", "deleted_by", "content", "updated_at"])

        emit_chat_metric("chat_message_soft_deleted", message_id=message.id)

        if matrix_map:
            matrix_service.redact_event(
                matrix_map.matrix_room_id,
                matrix_map.matrix_event_id,
                reason="sharemundo_soft_delete",
            )

        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="reactions/toggle")
    def toggle_reaction(self, request, pk=None):
        message = self.get_object()
        emoji = (request.data or {}).get("emoji")
        if not emoji:
            return Response({"error": "emoji is required"}, status=status.HTTP_400_BAD_REQUEST)

        if message.is_deleted:
            return Response({"error": "Cannot react to deleted message"}, status=status.HTTP_400_BAD_REQUEST)

        key = str(emoji)
        if len(key) > 255:
            return Response({"error": "emoji too long"}, status=status.HTTP_400_BAD_REQUEST)
        existing = MessageReaction.objects.filter(message=message, user=request.user, key=key).first()
        reaction_added = False
        if existing:
            matrix_map = getattr(existing, "matrix_reaction_map", None)
            if matrix_map:
                matrix_service.redact_event(
                    matrix_map.matrix_room_id,
                    matrix_map.matrix_event_id,
                    reason="reaction_removed",
                )
            existing.delete()
        else:
            reaction = MessageReaction.objects.create(message=message, user=request.user, key=key)
            reaction_added = True
            matrix_message_map = getattr(message, "matrix_message_map", None)
            if matrix_message_map:
                reaction_event_id = matrix_service.send_reaction(
                    matrix_message_map.matrix_room_id,
                    matrix_message_map.matrix_event_id,
                    key,
                )
                if reaction_event_id:
                    from .models import MatrixReactionMap

                    MatrixReactionMap.objects.create(
                        reaction=reaction,
                        matrix_event_id=reaction_event_id,
                        matrix_room_id=matrix_message_map.matrix_room_id,
                        relates_to_event_id=matrix_message_map.matrix_event_id,
                        key=key,
                    )

        if reaction_added:
            if getattr(settings, "CHAT_PUSH_USE_CELERY", True):
                send_chat_reaction_push_notifications.delay(message.id, request.user.id, key)
            else:
                send_chat_reaction_push_notifications.apply(args=[message.id, request.user.id, key])

        serializer = self.get_serializer(message, context={"request": request})
        return Response(serializer.data, status=status.HTTP_200_OK)
    
class ChatRoomViewSet(viewsets.ModelViewSet):
    queryset = ChatRoom.objects.all()
    serializer_class = ChatRoomSerializer

    def _get_accessible_room(self, user, room_id):
        if isinstance(user, AnonymousUser):
            return None

        room = (
            ChatRoom.objects.select_related("company")
            .prefetch_related("participants")
            .filter(id=room_id)
            .first()
        )
        if not room:
            return None

        accessible_room_ids = set(get_user_chat_rooms(user))
        if room.id not in accessible_room_ids:
            return None

        return room

    def _can_access_participating_direct_or_group_room(self, user, room):
        if not room.is_group:
            return True

        owner = room.created_by
        if not owner:
            return True

        if owner.id == user.id:
            return True

        return are_users_connected(owner, user)

    def create(self, request, *args, **kwargs):
        """
        Create a chat room on demand.
        For direct chats, if a room with the exact participants exists, it returns that room.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chat_room = serializer.save()
        return Response(self.get_serializer(chat_room).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"], url_path="open-direct-chat")
    def open_direct_chat(self, request):
        """
        Open a direct chat with another user.
        Returns the existing room when one already exists, otherwise creates it.
        """
        participant_id = request.data.get("participant_id")
        if participant_id in (None, ""):
            return Response(
                {"detail": "participant_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            participant_id = int(participant_id)
        except (TypeError, ValueError):
            return Response(
                {"detail": "participant_id must be a valid integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if participant_id == request.user.id:
            return Response(
                {"detail": "You cannot open a direct chat with yourself."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user = User.objects.filter(id=participant_id).first()
        if not target_user:
            return Response(
                {"detail": "User not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        existing_room = find_direct_chat_room(request.user, target_user)
        serializer = self.get_serializer(
            data={"participants": [request.user.id, target_user.id]},
        )
        serializer.is_valid(raise_exception=True)
        room = serializer.save()

        response_status = (
            status.HTTP_200_OK if existing_room else status.HTTP_201_CREATED
        )
        return Response(
            {"id": room.id},
            status=response_status,
        )

    @action(detail=False, methods=["post"], url_path="group")
    def create_group(self, request):
        payload = request.data.copy()
        payload["is_group"] = True

        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        chat_room = serializer.save()

        return Response(self.get_serializer(chat_room).data, status=status.HTTP_201_CREATED)
    
    def get_queryset(self):
        user = self.request.user
        if isinstance(user, AnonymousUser):
            return ChatRoom.objects.none()

        latest_message_id = (
            Message.objects.filter(room=OuterRef("pk"))
            .order_by("-created_at", "-id")
            .values("id")[:1]
        )

        include_scoped = str(self.request.query_params.get("include_scoped") or "").lower() in {
            "1",
            "true",
            "yes",
        }

        if include_scoped:
            room_ids = get_user_chat_rooms(user)
            if not room_ids:
                return ChatRoom.objects.none()
            return (
                ChatRoom.objects.filter(id__in=room_ids)
                .annotate(last_message_id=Subquery(latest_message_id))
                .select_related("company", "project", "created_by", "closed_by")
                .prefetch_related("participants")
                .order_by("-updated_at", "-id")
                .distinct()
            )

        all_rooms = (
            ChatRoom.objects.filter(participants=user)
            .filter(Q(is_direct=True) | Q(is_group=True))
            .select_related("created_by", "created_by__company")
            .distinct()
        )

        accessible_room_ids = [
            room.id
            for room in all_rooms
            if self._can_access_participating_direct_or_group_room(user, room)
        ]
        return (
            ChatRoom.objects.filter(id__in=accessible_room_ids)
            .annotate(last_message_id=Subquery(latest_message_id))
            .select_related("company", "project", "created_by", "closed_by")
            .prefetch_related("participants")
            .order_by("-updated_at", "-id")
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        user = self.request.user
        if isinstance(user, AnonymousUser):
            context["unread_count_map"] = {}
            context["online_user_id_set"] = set()
            return context

        context_rooms = getattr(self, "_serializer_context_rooms", None)
        if context_rooms is not None:
            context_room_list = list(context_rooms)
            room_ids = [room.id for room in context_room_list]
            latest_message_ids = [
                getattr(room, "last_message_id", None)
                for room in context_room_list
                if getattr(room, "last_message_id", None)
            ]
        else:
            queryset = self.get_queryset()
            room_ids = list(queryset.values_list("id", flat=True))
            latest_message_ids = [
                value
                for value in queryset.values_list("last_message_id", flat=True)
                if value
            ]
        if not room_ids:
            context["unread_count_map"] = {}
            context["online_user_id_set"] = set()
            context["last_message_map"] = {}
            return context

        read_message_ids = MessageRead.objects.filter(
            user=user,
            message__room_id__in=room_ids,
        ).values_list("message_id", flat=True)

        unread_rows = (
            Message.objects.filter(room_id__in=room_ids)
            .exclude(sender=user)
            .exclude(id__in=read_message_ids)
            .values("room_id")
            .annotate(total=Count("id"))
        )

        context["unread_count_map"] = {
            row["room_id"]: row["total"] for row in unread_rows
        }
        latest_messages = (
            Message.objects.filter(id__in=latest_message_ids)
            .select_related("room", "sender", "replied_to", "replied_to__sender")
            .prefetch_related("messageread_set__user", "reactions__user")
        )
        context["last_message_map"] = {
            message.room_id: message for message in latest_messages
        }
        participant_user_ids = set(
            User.objects.filter(direct_chatrooms__id__in=room_ids)
            .values_list("id", flat=True)
            .distinct()
        )
        now = timezone.now()
        busy_user_ids = set(
            CalendarEvent.objects.filter(
                start_date__lte=now,
                end_date__gte=now,
            )
            .filter(
                Q(user_id__in=participant_user_ids)
                | Q(assignees__id__in=participant_user_ids)
            )
            .values_list("user_id", flat=True)
            .distinct()
        )
        busy_user_ids.update(
            CalendarEvent.objects.filter(
                start_date__lte=now,
                end_date__gte=now,
                assignees__id__in=participant_user_ids,
            )
            .values_list("assignees__id", flat=True)
            .distinct()
        )
        context["online_user_id_set"] = get_online_user_ids(participant_user_ids)
        context["busy_user_id_set"] = busy_user_ids
        return context

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            self._serializer_context_rooms = page
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        self._serializer_context_rooms = queryset
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def _get_header_preview_queryset(self, user):
        latest_message = Message.objects.filter(room_id=OuterRef("pk")).order_by(
            "-created_at",
            "-id",
        )
        is_read_by_user = MessageRead.objects.filter(
            user=user,
            message_id=OuterRef("pk"),
        )
        unread_messages = (
            Message.objects.filter(room_id=OuterRef("pk"))
            .exclude(sender=user)
            .annotate(is_read_by_user=Exists(is_read_by_user))
            .filter(is_read_by_user=False)
            .values("room_id")
            .annotate(total=Count("id"))
            .values("total")[:1]
        )

        return self.get_queryset().annotate(
            header_latest_message_at=Subquery(
                latest_message.values("created_at")[:1],
                output_field=DateTimeField(),
            ),
            header_unread_count=Coalesce(
                Subquery(unread_messages, output_field=IntegerField()),
                Value(0),
            ),
            header_activity_at=Coalesce(
                "header_latest_message_at",
                "created_at",
            ),
        )

    @action(detail=False, methods=["get"], url_path="header-preview")
    def header_preview(self, request):
        queryset = self._get_header_preview_queryset(request.user)
        unread_queryset = queryset.filter(header_unread_count__gt=0)
        has_unread_rooms = unread_queryset.exists()
        rooms = list(
            unread_queryset.order_by(
                F("header_activity_at").desc(nulls_last=True),
                F("id").desc(),
            )[:5]
        )

        if len(rooms) < 5:
            unread_room_ids = [room.id for room in rooms]
            recent_rooms = queryset.exclude(id__in=unread_room_ids).order_by(
                F("header_activity_at").desc(nulls_last=True),
                F("id").desc(),
            )[: 5 - len(rooms)]
            rooms.extend(recent_rooms)

        latest_message_ids = [
            room.last_message_id
            for room in rooms
            if getattr(room, "last_message_id", None)
        ]
        latest_messages = Message.objects.filter(
            id__in=latest_message_ids,
        ).select_related("sender")
        last_message_map = {
            message.room_id: message for message in latest_messages
        }

        serializer = ChatRoomHeaderPreviewSerializer(
            rooms,
            many=True,
            context={
                "request": request,
                "last_message_map": last_message_map,
            },
        )
        return Response(
            {
                "mode": "unread" if has_unread_rooms else "recent",
                "results": serializer.data,
            },
            status=status.HTTP_200_OK,
        )

    def _get_direct_group_room_ids(self):
        return list(self.get_queryset().values_list("id", flat=True))

    @action(detail=True, methods=["patch"], url_path="mark-read")
    def mark_read(self, request, pk=None):
        room = self.get_object()
        all_room_ids = self._get_direct_group_room_ids()
        marked_count = mark_rooms_as_read(request.user, [room.id])

        return Response(
            {
                "room_id": room.id,
                "marked_count": marked_count,
                "unread_count": count_unread_messages(request.user, [room.id]),
                "total_unread_count": count_unread_messages(
                    request.user,
                    all_room_ids,
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["patch"], url_path="mark-all-read")
    def mark_all_read(self, request):
        room_ids = self._get_direct_group_room_ids()
        marked_count = mark_rooms_as_read(request.user, room_ids)

        return Response(
            {
                "marked_count": marked_count,
                "total_unread_count": count_unread_messages(
                    request.user,
                    room_ids,
                ),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["get"], url_path="unread_count")
    def unread_count(self, request):
        user = request.user
        if isinstance(user, AnonymousUser):
            return Response({"unread_count": 0}, status=status.HTTP_200_OK)

        room_ids = list(self.get_queryset().values_list("id", flat=True))
        if not room_ids:
            return Response({"unread_count": 0}, status=status.HTTP_200_OK)

        read_message_ids = MessageRead.objects.filter(
            user=user,
            message__room_id__in=room_ids,
        ).values_list("message_id", flat=True)

        unread_count = (
            Message.objects.filter(room_id__in=room_ids)
            .exclude(sender=user)
            .exclude(id__in=read_message_ids)
            .count()
        )
        return Response({"unread_count": unread_count}, status=status.HTTP_200_OK)

    def _validate_group_owner(self, request, room, *, allow_closed=False):
        if not room.is_group:
            return Response(
                {"error": "This operation is only available for group chats."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if room.created_by_id != request.user.id:
            return Response(
                {"error": "Only the group creator can manage this group."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if room.is_closed and not allow_closed:
            return Response(
                {"error": "This group chat is closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return None

    def _is_supported_search_room(self, room):
        return bool(
            room.is_direct
            or room.is_group
            or (room.company_id and not room.project_id and not room.is_direct and not room.is_group)
        )

    @action(detail=True, methods=["get"], url_path="mention-candidates")
    def mention_candidates(self, request, pk=None):
        room = self._get_accessible_room(request.user, pk)
        if room is None:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if room.is_group:
            candidates = room.participants.all()
        elif room.company_id and not room.project_id and not room.is_direct:
            candidates = User.objects.filter(company_id=room.company_id)
        else:
            return Response(
                {"detail": "Mention candidates are only available for company and group chats."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        candidates = candidates.filter(
            is_active=True,
            is_deleted=False,
            is_permanently_deleted=False,
        ).distinct()

        search = (request.query_params.get("search") or "").strip()
        if search:
            search_terms = [term for term in search.split() if term]
            search_filter = (
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(username__icontains=search)
            )
            for term in search_terms:
                search_filter &= (
                    Q(first_name__icontains=term)
                    | Q(last_name__icontains=term)
                    | Q(username__icontains=term)
                )
            candidates = candidates.filter(search_filter)

        try:
            limit = int(request.query_params.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(limit, 25))

        candidates = list(candidates.order_by("first_name", "last_name", "id")[:limit])

        return Response(
            [
                {
                    "user_id": candidate.id,
                    "name": candidate.full_name,
                    "user_image": candidate.user_image or None,
                }
                for candidate in candidates
            ],
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["get"], url_path="search-messages")
    def search_messages(self, request, pk=None):
        room = self._get_accessible_room(request.user, pk)
        if room is None:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not self._is_supported_search_room(room):
            return Response(
                {"detail": "Search is only available for company, direct, and group chats."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        query = str(request.query_params.get("q") or "").strip()
        if len(query) < 2:
            return Response(
                {"detail": "Query parameter 'q' must be at least 2 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = (
            Message.objects.filter(room=room, is_deleted=False, content__icontains=query)
            .select_related("sender")
            .order_by("-created_at", "-id")
        )

        page = self.paginate_queryset(queryset)
        serializer_context = {
            "request": request,
            "query": query,
        }

        if page is not None:
            serializer = RoomChatMessageSearchResultSerializer(
                page,
                many=True,
                context=serializer_context,
            )
            return self.get_paginated_response(serializer.data)

        serializer = RoomChatMessageSearchResultSerializer(
            queryset,
            many=True,
            context=serializer_context,
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="message-context")
    def message_context(self, request, pk=None):
        room = self._get_accessible_room(request.user, pk)
        if room is None:
            return Response(
                {"detail": "Not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not self._is_supported_search_room(room):
            return Response(
                {"detail": "Message context is only available for company, direct, and group chats."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message_id = request.query_params.get("message_id")
        if not message_id:
            return Response(
                {"detail": "message_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            message_id = int(message_id)
        except (TypeError, ValueError):
            return Response(
                {"detail": "message_id must be a valid integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            before_limit = int(request.query_params.get("before_limit") or 20)
            after_limit = int(request.query_params.get("after_limit") or 20)
        except (TypeError, ValueError):
            return Response(
                {"detail": "before_limit and after_limit must be valid integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        before_limit = max(0, min(before_limit, 50))
        after_limit = max(0, min(after_limit, 50))

        anchor = (
            Message.objects.select_related("sender", "replied_to", "replied_to__sender")
            .prefetch_related("reactions__user", "messageread_set__user")
            .filter(id=message_id)
            .first()
        )
        if anchor is None:
            return Response(
                {"detail": "Message not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if anchor.room_id != room.id:
            return Response(
                {"detail": "The requested message does not belong to this room."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        before_qs = Message.objects.filter(room=room).filter(
            Q(created_at__lt=anchor.created_at)
            | (Q(created_at=anchor.created_at) & Q(id__lt=anchor.id))
        ).select_related("sender", "replied_to", "replied_to__sender").prefetch_related(
            "reactions__user",
            "messageread_set__user",
        ).order_by("-created_at", "-id")

        after_qs = Message.objects.filter(room=room).filter(
            Q(created_at__gt=anchor.created_at)
            | (Q(created_at=anchor.created_at) & Q(id__gt=anchor.id))
        ).select_related("sender", "replied_to", "replied_to__sender").prefetch_related(
            "reactions__user",
            "messageread_set__user",
        ).order_by("created_at", "id")

        before_messages = list(before_qs[:before_limit])
        before_messages.reverse()
        after_messages = list(after_qs[:after_limit])

        has_older = bool(before_qs[before_limit : before_limit + 1])
        has_newer = bool(after_qs[after_limit : after_limit + 1])

        serializer = MessageContextSerializer(
            {
                "room_id": room.id,
                "anchor_message_id": anchor.id,
                "before": before_messages,
                "anchor": anchor,
                "after": after_messages,
                "has_older": has_older,
                "has_newer": has_newer,
            },
            context={"request": request},
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["patch"], url_path="rename_group")
    def rename_group(self, request, pk=None):
        room = self.get_object()
        validation_response = self._validate_group_owner(request, room)
        if validation_response is not None:
            return validation_response

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response(
                {"error": "Group name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room.name = name
        room.save(update_fields=["name", "updated_at"])

        return Response(self.get_serializer(room).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="add_participants")
    def add_participants(self, request, pk=None):
        room = self.get_object()
        validation_response = self._validate_group_owner(request, room)
        if validation_response is not None:
            return validation_response

        participant_ids = request.data.get("participant_ids") or []
        if not isinstance(participant_ids, list):
            return Response(
                {"error": "participant_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cleaned_ids = sorted({int(pid) for pid in participant_ids if str(pid).isdigit()})
        if not cleaned_ids:
            return Response(
                {"error": "At least one participant is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        creator = room.created_by
        candidates = User.objects.filter(id__in=cleaned_ids)
        if candidates.count() != len(cleaned_ids):
            return Response(
                {"error": "One or more participants do not exist."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        for candidate in candidates:
            if candidate.id == creator.id:
                continue
            if not are_users_connected(creator, candidate):
                return Response(
                    {
                        "error": "All participants must have an active connection with the group creator."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        room.participants.add(*candidates)

        return Response(self.get_serializer(room).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="remove_participants")
    def remove_participants(self, request, pk=None):
        room = self.get_object()
        validation_response = self._validate_group_owner(request, room)
        if validation_response is not None:
            return validation_response

        participant_ids = request.data.get("participant_ids") or []
        if not isinstance(participant_ids, list):
            return Response(
                {"error": "participant_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cleaned_ids = sorted({int(pid) for pid in participant_ids if str(pid).isdigit()})
        if not cleaned_ids:
            return Response(
                {"error": "At least one participant is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if room.created_by_id in cleaned_ids:
            return Response(
                {"error": "The group creator cannot be removed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        removable_users = room.participants.filter(id__in=cleaned_ids)

        remaining_count = room.participants.exclude(id__in=cleaned_ids).count()
        if remaining_count < 2:
            return Response(
                {"error": "A group chat must keep at least two participants."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room.participants.remove(*removable_users)

        return Response(self.get_serializer(room).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="leave_group")
    def leave_group(self, request, pk=None):
        room = self.get_object()
        if not room.is_group:
            return Response(
                {"error": "This operation is only available for group chats."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if room.created_by_id == request.user.id:
            return Response(
                {
                    "error": "Group creator cannot leave the group. Close the group instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not room.participants.filter(id=request.user.id).exists():
            return Response(
                {"error": "You are not a participant of this group."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room.participants.remove(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="close_group")
    def close_group(self, request, pk=None):
        room = self.get_object()
        validation_response = self._validate_group_owner(request, room, allow_closed=True)
        if validation_response is not None:
            return validation_response

        if room.is_closed:
            return Response(
                {"error": "Group chat is already closed."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room.is_closed = True
        room.closed_at = timezone.now()
        room.closed_by = request.user
        room.save(update_fields=["is_closed", "closed_at", "closed_by", "updated_at"])

        matrix_map = getattr(room, "matrix_room_map", None)
        if matrix_map:
            matrix_map.state = "closed"
            matrix_map.save(update_fields=["state", "updated_at"])

        return Response(self.get_serializer(room).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["delete"], url_path="delete_closed_group")
    def delete_closed_group(self, request, pk=None):
        room = self.get_object()
        validation_response = self._validate_group_owner(request, room, allow_closed=True)
        if validation_response is not None:
            return validation_response

        if not room.is_closed:
            return Response(
                {"error": "Only closed group chats can be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        room.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ChatPushTokenViewSet(viewsets.GenericViewSet):
    serializer_class = ChatPushDeviceSerializer
    queryset = ChatPushDevice.objects.none()

    def get_queryset(self):
        return ChatPushDevice.objects.filter(user=self.request.user).order_by("-updated_at")

    def list(self, request):
        serializer = self.get_serializer(self.get_queryset(), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def create(self, request):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        device_token = serializer.validated_data["token"]
        platform = serializer.validated_data.get("platform") or ChatPushDevice.PLATFORM_WEB
        user_agent = serializer.validated_data.get("user_agent")

        push_device, _ = ChatPushDevice.objects.update_or_create(
            user=request.user,
            token=device_token,
            defaults={
                "platform": platform,
                "user_agent": user_agent,
                "is_active": True,
            },
        )

        logger.info(
            "Chat push token registered",
            extra={
                "user_id": request.user.id,
                "device_id": push_device.id,
                "platform": push_device.platform,
            },
        )

        response_serializer = self.get_serializer(push_device)
        return Response(response_serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="unregister")
    def unregister(self, request):
        token = str(request.data.get("token") or "").strip()
        if not token:
            return Response(
                {"error": "token is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updated = ChatPushDevice.objects.filter(user=request.user, token=token).update(
            is_active=False,
            updated_at=timezone.now(),
        )

        logger.info(
            "Chat push token unregistered",
            extra={"user_id": request.user.id, "updated": int(updated)},
        )

        return Response(
            {"status": "ok", "updated": int(updated)},
            status=status.HTTP_200_OK,
        )


class ChatPresenceViewSet(viewsets.ViewSet):
    @action(detail=False, methods=["post", "patch"], url_path="heartbeat")
    def heartbeat(self, request):
        session_id = request.data.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if request.method.lower() == "patch":
            close_user_presence_session(request.user, session_id)
        else:
            heartbeat_user_presence(request.user, session_id)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)
