import os
from uuid import uuid4

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.db.models import Prefetch
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from ecpmpapp.utils import (
    get_actor_name,
    get_validate_recipients,
)
from models.models import Notification, NotificationSeen, User
from project_management.action_item_comment_mentions import (
    get_action_item_mention_candidates,
    get_project_for_action_item as _get_project_for_action_item,
    get_root_action_item as _get_root_action_item,
    has_action_item_view_access as _has_action_item_view_access,
    is_active_user as _is_active_notification_user,
)
from project_management.models import (
    ActionItem,
    ActionItemComment,
    ActionItemCommentReaction,
)
from project_management.serializers import (
    ActionItemCommentSerializer,
    ActionItemCommentThreadSerializer,
)
from project_management.signals import get_project_recipients


def _get_comment_notification_recipients(action_item):
    root_action_item = _get_root_action_item(action_item)
    recipients = set()

    project = _get_project_for_action_item(action_item)
    if project:
        recipients.update(get_project_recipients(project))
        if getattr(project, "company_profile_id", None):
            recipients.update(
                User.objects.filter(
                    company=project.company_profile,
                    is_active=True,
                    is_deleted=False,
                    is_permanently_deleted=False,
                )
            )

    company_profile = getattr(root_action_item, "company_profile", None)
    if company_profile:
        recipients.update(
            User.objects.filter(
                company=company_profile,
                is_active=True,
                is_deleted=False,
                is_permanently_deleted=False,
            )
        )

    freelancer = getattr(root_action_item, "freelancer", None)
    if _is_active_notification_user(freelancer):
        recipients.add(freelancer)

    assignees = set(action_item.assignees.all())
    for assignee in assignees:
        if _is_active_notification_user(assignee):
            recipients.add(assignee)

    created_by = getattr(action_item, "created_by", None)
    if _is_active_notification_user(created_by):
        recipients.add(created_by)

    return {
        user
        for user in recipients
        if _has_action_item_view_access(user, action_item)
    }


def _get_comment_preview(comment):
    content = " ".join((comment.content or "").split())
    if content:
        if len(content) > 120:
            return f"{content[:117]}..."
        return content

    if comment.attachments:
        attachment_count = len(comment.attachments)
        if attachment_count == 1:
            return "sent an attachment"
        return f"sent {attachment_count} attachments"

    if comment.linked_objects:
        return "shared linked objects"

    return "added a comment"


def _notify_new_comment(comment, actor):
    action_item = comment.action_item
    recipients = _get_comment_notification_recipients(action_item)
    validated_recipients = get_validate_recipients(recipients, actor)

    if not validated_recipients:
        return

    actor_name = get_actor_name(actor)
    action_item_phrase = "Sub Action Item" if action_item.parent_action_item_id else "Action Item"
    preview = _get_comment_preview(comment)
    message = (
        f"New comment on {action_item_phrase} AI-{action_item.id} "
        f'"{action_item.name}" by {actor_name}: {preview}'
    )

    notification = Notification.objects.create(
        message=message,
        content_type=ContentType.objects.get_for_model(ActionItem),
        object_id=action_item.pk,
        type="create",
        backend_type="backend",
    )
    notification.user.add(*validated_recipients)
    for user in validated_recipients:
        NotificationSeen.objects.get_or_create(user=user, notification=notification)


class ActionItemCommentViewSet(
    mixins.ListModelMixin,
    mixins.CreateModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = ActionItemCommentSerializer
    lookup_url_kwarg = "comment_id"
    ordering = ["-created_at", "-id"]

    def _get_base_queryset(self):
        action_item = self.get_action_item()
        return ActionItemComment.objects.filter(action_item=action_item)

    def get_action_item(self):
        if not hasattr(self, "_action_item"):
            self._action_item = get_object_or_404(
                ActionItem,
                pk=self.kwargs["action_item_id"],
            )
        return self._action_item

    def get_queryset(self):
        return (
            self._get_base_queryset()
            .select_related("sender", "replied_to", "replied_to__sender")
            .prefetch_related("reactions__user")
            .order_by(*self.ordering)
        )

    def get_list_queryset(self):
        reply_queryset = (
            self._get_base_queryset()
            .filter(replied_to__isnull=False)
            .select_related("sender", "replied_to", "replied_to__sender")
            .prefetch_related("reactions__user")
            .order_by("created_at", "id")
        )
        return (
            self._get_base_queryset()
            .filter(replied_to__isnull=True)
            .select_related("sender")
            .prefetch_related(
                "reactions__user",
                Prefetch("replies", queryset=reply_queryset, to_attr="prefetched_replies"),
            )
            .order_by(*self.ordering)
        )

    def get_object(self):
        queryset = self.filter_queryset(self.get_queryset())
        return get_object_or_404(
            queryset,
            pk=self.kwargs[self.lookup_url_kwarg],
        )

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["user"] = self.request.user
        return context

    def get_serializer_class(self):
        if self.action == "list":
            return ActionItemCommentThreadSerializer
        return ActionItemCommentSerializer

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_list_queryset())
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        action_item = self.get_action_item()
        payload = request.data.copy()
        payload["action_item"] = action_item.id

        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        _notify_new_comment(serializer.instance, request.user)

        headers = self.get_success_headers(serializer.data)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
            headers=headers,
        )

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop("partial", False)
        instance = self.get_object()
        self._assert_author(instance)

        payload = request.data.copy()
        payload["action_item"] = instance.action_item_id

        serializer = self.get_serializer(
            instance,
            data=payload,
            partial=partial,
        )
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        self._assert_author(instance)

        if instance.is_deleted:
            return Response(status=status.HTTP_204_NO_CONTENT)

        instance.is_deleted = True
        instance.deleted_at = timezone.now()
        instance.deleted_by = request.user
        instance.content = ""
        instance.save(
            update_fields=[
                "is_deleted",
                "deleted_at",
                "deleted_by",
                "content",
                "updated_at",
            ]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"], url_path="mention-candidates")
    def mention_candidates(self, request, action_item_id=None):
        action_item = self.get_action_item()
        search = request.query_params.get("search", "")
        raw_limit = request.query_params.get("limit", 20)

        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 20
        limit = min(max(limit, 1), 50)

        return Response(
            {
                "results": get_action_item_mention_candidates(
                    action_item=action_item,
                    search=search,
                    limit=limit,
                )
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=False, methods=["post"], url_path="attachments")
    def upload_attachments(self, request, action_item_id=None):
        self.get_action_item()
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

        max_attachment_files = int(getattr(settings, "CHAT_ATTACHMENT_MAX_FILES", 5) or 5)
        if len(files) > max_attachment_files:
            return Response(
                {
                    "error": (
                        f"You can upload at most {max_attachment_files} files per comment."
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
                    {"error": f"File type '{extension}' is not allowed."},
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
                f"action-item-comments/attachments/{self.kwargs['action_item_id']}/{uuid4().hex}{extension}",
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

    @action(detail=True, methods=["post"], url_path="reactions/toggle")
    def toggle_reaction(self, request, comment_id=None, action_item_id=None):
        comment = self.get_object()
        emoji = (request.data or {}).get("emoji")
        if not emoji:
            return Response({"error": "emoji is required"}, status=status.HTTP_400_BAD_REQUEST)

        if comment.is_deleted:
            return Response(
                {"error": "Cannot react to deleted comment"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        key = str(emoji)
        if len(key) > 255:
            return Response({"error": "emoji too long"}, status=status.HTTP_400_BAD_REQUEST)

        existing = ActionItemCommentReaction.objects.filter(
            comment=comment,
            user=request.user,
            key=key,
        ).first()
        if existing:
            existing.delete()
        else:
            ActionItemCommentReaction.objects.create(
                comment=comment,
                user=request.user,
                key=key,
            )

        refreshed_comment = self.get_queryset().get(pk=comment.pk)
        serializer = self.get_serializer(
            refreshed_comment,
            context={"request": request, "user": request.user},
        )
        return Response(serializer.data, status=status.HTTP_200_OK)

    def _assert_author(self, instance):
        if instance.sender_id != self.request.user.id:
            raise PermissionDenied("Only the comment author can modify this comment.")
