import os

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from chat.object_links import (
    extract_linked_objects_from_text,
    filter_accessible_linked_objects,
    merge_linked_objects,
    normalize_linked_object_refs,
    normalize_linked_objects,
    resolve_linked_objects,
)
from project_management.action_item_comment_mentions import (
    get_action_item_mention_user_snapshots,
)
from project_management.models import ActionItemComment


class ActionItemCommentSerializer(serializers.ModelSerializer):
    content = serializers.CharField(required=False, allow_blank=True)
    sender = serializers.IntegerField(source="sender_id", read_only=True)
    sender_username = serializers.SerializerMethodField()
    sender_image = serializers.SerializerMethodField()
    reply_to = serializers.SerializerMethodField()
    reactions = serializers.SerializerMethodField()
    linked_object_refs = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        write_only=True,
    )

    class Meta:
        model = ActionItemComment
        fields = [
            "id",
            "action_item",
            "sender",
            "sender_username",
            "sender_image",
            "content",
            "attachments",
            "linked_objects",
            "mentions",
            "linked_object_refs",
            "replied_to",
            "reply_to",
            "is_deleted",
            "is_edited",
            "edited_at",
            "created_at",
            "updated_at",
            "reactions",
        ]
        read_only_fields = [
            "sender",
            "sender_username",
            "sender_image",
            "reply_to",
            "is_deleted",
            "is_edited",
            "edited_at",
            "created_at",
            "updated_at",
            "reactions",
        ]

    def _get_request_user(self):
        request = self.context.get("request")
        if request is not None:
            return getattr(request, "user", None)
        return self.context.get("user")

    def get_sender_username(self, obj):
        sender = getattr(obj, "sender", None)
        return sender.full_name if sender else ""

    def get_sender_image(self, obj):
        sender = getattr(obj, "sender", None)
        return sender.user_image if sender else ""

    def get_reply_to(self, obj):
        target = getattr(obj, "replied_to", None)
        if not target:
            return None

        sender = getattr(target, "sender", None)
        content = "" if target.is_deleted else target.content
        return {
            "id": target.id,
            "sender": target.sender_id,
            "sender_username": sender.full_name if sender else "",
            "content": content,
            "is_deleted": target.is_deleted,
            "created_at": target.created_at.isoformat() if target.created_at else None,
        }

    def get_reactions(self, obj):
        if getattr(obj, "is_deleted", False):
            return []

        viewer = self._get_request_user()
        viewer_id = getattr(viewer, "id", None)

        grouped = {}
        for reaction in obj.reactions.all():
            bucket = grouped.get(reaction.key)
            if not bucket:
                bucket = {
                    "emoji": reaction.key,
                    "count": 0,
                    "users": [],
                    "reacted_by_me": False,
                }
                grouped[reaction.key] = bucket

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

    def validate_attachments(self, value):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Attachments must be a list.")
        return value

    def validate_mentions(self, value):
        if value in (None, ""):
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("Mentions must be a list.")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)

        request_user = self._get_request_user()
        instance = self.instance

        if instance and instance.is_deleted:
            raise serializers.ValidationError("Deleted comments cannot be updated.")

        action_item = attrs.get("action_item") or getattr(instance, "action_item", None)
        replied_to = attrs.get("replied_to")
        content = str(attrs.get("content", getattr(instance, "content", "")) or "").strip()
        attachments_raw = attrs.get("attachments", getattr(instance, "attachments", [])) or []
        linked_objects_raw = attrs.get(
            "linked_objects",
            getattr(instance, "linked_objects", []),
        ) or []
        mentions_raw = attrs.get(
            "mentions",
            getattr(instance, "mentions", []) if instance else [],
        ) or []
        linked_object_refs_raw = attrs.get("linked_object_refs") or []

        if instance:
            if "action_item" in attrs and attrs["action_item"].id != instance.action_item_id:
                raise serializers.ValidationError(
                    {"action_item": "Action item cannot be changed."}
                )
            if "replied_to" in attrs and replied_to and instance.replied_to_id != replied_to.id:
                raise serializers.ValidationError(
                    {"replied_to": "Reply target cannot be changed after creation."}
                )
            if "attachments" in attrs and attrs["attachments"] != instance.attachments:
                raise serializers.ValidationError(
                    {"attachments": "Attachments cannot be changed after send."}
                )

        if replied_to is not None and action_item is not None:
            if replied_to.action_item_id != action_item.id:
                raise serializers.ValidationError(
                    {"replied_to": "Reply target must belong to the same action item."}
                )
            if replied_to.replied_to_id is not None:
                raise serializers.ValidationError(
                    {"replied_to": "Replies can only be added to top-level comments."}
                )

        try:
            normalized_linked_objects = normalize_linked_objects(linked_objects_raw)
        except ValueError as exc:
            raise serializers.ValidationError({"linked_objects": str(exc)})

        normalized_linked_object_refs = normalize_linked_object_refs(linked_object_refs_raw)
        normalized_linked_object_refs_from_text = extract_linked_objects_from_text(content)
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
                        f"Maximum {max_linked_objects} linked objects are allowed per comment."
                    )
                }
            )

        normalized_attachments = self._normalize_attachments(attachments_raw)
        if "mentions" in attrs or not instance:
            attrs["mentions"] = self._normalize_mentions(mentions_raw, action_item)

        if not content and not normalized_attachments and not accessible_linked_objects:
            raise serializers.ValidationError(
                {"content": "Comment content cannot be empty."}
            )

        attrs["content"] = content
        attrs["attachments"] = normalized_attachments
        attrs["linked_objects"] = accessible_linked_objects
        attrs.pop("linked_object_refs", None)
        return attrs

    def _normalize_attachments(self, attachments_raw):
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
                        f"Maximum {max_attachment_files} attachments are allowed per comment."
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

        return normalized_attachments

    def _normalize_mentions(self, mentions_raw, action_item):
        if not mentions_raw:
            return []

        if action_item is None:
            raise serializers.ValidationError(
                {"mentions": "Action item is required for mentions."}
            )

        mentioned_user_ids = []
        seen_user_ids = set()
        for index, mention in enumerate(mentions_raw, start=1):
            if not isinstance(mention, dict):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} must be an object."}
                )

            raw_user_id = mention.get("user_id", mention.get("id"))
            if raw_user_id in (None, ""):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} user_id is required."}
                )

            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                raise serializers.ValidationError(
                    {"mentions": f"Mention #{index} user_id must be an integer."}
                )

            if user_id in seen_user_ids:
                continue

            seen_user_ids.add(user_id)
            mentioned_user_ids.append(user_id)

        mention_snapshots = get_action_item_mention_user_snapshots(
            action_item,
            mentioned_user_ids,
        )
        invalid_user_ids = [
            user_id
            for user_id in mentioned_user_ids
            if user_id not in mention_snapshots
        ]
        if invalid_user_ids:
            raise serializers.ValidationError(
                {
                    "mentions": (
                        "Invalid or out-of-scope mention user IDs: "
                        f"{', '.join(str(user_id) for user_id in invalid_user_ids)}."
                    )
                }
            )

        return [mention_snapshots[user_id] for user_id in mentioned_user_ids]

    def create(self, validated_data):
        request_user = self._get_request_user()
        if request_user is not None and getattr(request_user, "is_authenticated", False):
            validated_data["sender"] = request_user
        return super().create(validated_data)

    def update(self, instance, validated_data):
        for field in ("action_item", "attachments", "replied_to"):
            validated_data.pop(field, None)

        content_changed = (
            "content" in validated_data and validated_data["content"] != instance.content
        )
        linked_objects_changed = (
            "linked_objects" in validated_data
            and validated_data["linked_objects"] != (instance.linked_objects or [])
        )
        mentions_changed = (
            "mentions" in validated_data
            and validated_data["mentions"] != (instance.mentions or [])
        )

        if content_changed or linked_objects_changed or mentions_changed:
            validated_data["is_edited"] = True
            validated_data["edited_at"] = timezone.now()

        return super().update(instance, validated_data)

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        if instance.is_deleted:
            representation["content"] = ""
            representation["attachments"] = []
            representation["linked_objects"] = []
            representation["mentions"] = []
            return representation

        request_user = self._get_request_user()
        try:
            representation["linked_objects"] = resolve_linked_objects(
                getattr(instance, "linked_objects", None) or [],
                request_user,
            )
        except Exception:
            representation["linked_objects"] = []

        return representation


class ActionItemCommentThreadSerializer(ActionItemCommentSerializer):
    replies = serializers.SerializerMethodField()

    class Meta(ActionItemCommentSerializer.Meta):
        fields = ActionItemCommentSerializer.Meta.fields + ["replies"]
        read_only_fields = (
            list(ActionItemCommentSerializer.Meta.read_only_fields) + ["replies"]
        )

    def get_replies(self, obj):
        replies = getattr(obj, "prefetched_replies", None)
        if replies is None:
            replies = obj.replies.all()

        serializer = ActionItemCommentSerializer(
            replies,
            many=True,
            context=self.context,
        )
        return serializer.data
