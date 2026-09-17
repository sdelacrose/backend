import logging
from datetime import timedelta
from urllib.parse import urlencode
from urllib.parse import urlsplit

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from ecpmpapp.utils import get_frontend_url_for_solution, get_user_active_solution
from models.models import Preference, User

from .models import ChatPushDevice, Message
from .push_service import send_chat_push_notifications
from .utils import can_access_group_chat


logger = logging.getLogger(__name__)


def _normalize_base_url(url_value):
    base_url = (url_value or "").strip()
    if not base_url:
        return ""
    if base_url.startswith("http://") or base_url.startswith("https://"):
        return base_url.rstrip("/")
    return f"https://{base_url}".rstrip("/")


def _chat_scope_for_room(room):
    if room.project_id:
        return "project"
    if room.company_id and not room.is_group and not room.is_direct:
        return "company"
    return "room"


def _chat_path_for_room(room):
    return "/project-chat" if room.project_id else "/chat"


def _chat_context_for_room(room):
    if room.project_id:
        project_name = (getattr(room.project, "project_name", "") or "").strip()
        return project_name or (room.name or "").strip() or "Project"

    if room.company_id and not room.is_group and not room.is_direct:
        company_name = (getattr(room.company, "company_name", "") or "").strip()
        return company_name or (room.name or "").strip() or "Company"

    if room.is_group:
        return (room.name or "").strip() or "Group"

    return ""


def _shared_object_phrase(message):
    linked_objects = getattr(message, "linked_objects", None) or []
    if not isinstance(linked_objects, list) or not linked_objects:
        return ""

    if len(linked_objects) > 1:
        return "Shared items"

    first_object = linked_objects[0]
    if not isinstance(first_object, dict):
        return "Shared object"

    object_type = str(first_object.get("object_type") or "").strip().lower()
    object_type_labels = {
        "action_item": "Shared Action Item",
        "phase": "Shared Phase",
        "document": "Shared Document",
        "project": "Shared Project",
    }
    return object_type_labels.get(object_type, "Shared Object")


def _append_preview_suffix(base_text, suffix_text, max_length=140):
    if not suffix_text:
        return base_text

    if len(base_text) + len(suffix_text) <= max_length:
        return f"{base_text}{suffix_text}"

    if len(suffix_text) >= max_length:
        return f"{suffix_text[: max_length - 3]}..."

    allowed_base_length = max_length - len(suffix_text)
    if len(base_text) > allowed_base_length:
        if allowed_base_length <= 3:
            trimmed_base = ""
        else:
            trimmed_base = f"{base_text[: allowed_base_length - 3]}..."
    else:
        trimmed_base = base_text

    return f"{trimmed_base}{suffix_text}"


def _message_preview(message):
    shared_object_phrase = _shared_object_phrase(message)

    if message.message_type == message.TYPE_VOICE:
        return _append_preview_suffix("Sent a voice note", f" - {shared_object_phrase}" if shared_object_phrase else "")

    if message.attachments and not (message.content or "").strip():
        return _append_preview_suffix("Sent an attachment", f" - {shared_object_phrase}" if shared_object_phrase else "")

    if getattr(message, "linked_objects", None) and not (message.content or "").strip():
        return shared_object_phrase or "Shared an object"

    content = (message.content or "").strip()
    if not content:
        return _append_preview_suffix("Sent a message", f" - {shared_object_phrase}" if shared_object_phrase else "")

    if shared_object_phrase:
        return _append_preview_suffix(content, f" - {shared_object_phrase}")

    if len(content) > 140:
        return f"{content[:137]}..."
    return content


def _mentioned_user_id_set(message):
    mentions = getattr(message, "mentions", None) or []
    if not isinstance(mentions, list):
        return set()

    mentioned_user_ids = set()
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        raw_user_id = mention.get("user_id")
        try:
            mentioned_user_ids.add(int(raw_user_id))
        except (TypeError, ValueError):
            continue

    return mentioned_user_ids


def _mention_preview(message):
    preview = _message_preview(message)
    sender_name = (getattr(message.sender, "full_name", "") or "").strip() or "Someone"
    return _append_preview_suffix(f"{sender_name} mentioned you", f": {preview}")


def _user_allows_chat_push(user):
    preference = Preference.objects.filter(user=user).only(
        "notify_item_created_by_me",
        "notify_item_i_own",
        "notify_item_am_part",
        "specific_notify_item_i_own",
        "specific_notify_item_am_part_of",
        "created_at",
        "updated_at",
    ).first()

    if not preference:
        return True

    has_enabled_preferences = any(
        [
            bool(preference.notify_item_created_by_me),
            bool(preference.notify_item_i_own),
            bool(preference.notify_item_am_part),
            bool(preference.specific_notify_item_i_own),
            bool(preference.specific_notify_item_am_part_of),
        ]
    )

    if has_enabled_preferences:
        return True

    if preference.updated_at and preference.created_at:
        untouched_default = preference.updated_at <= preference.created_at + timedelta(seconds=1)
        return untouched_default

    return True


def _resolve_recipients(message):
    room = message.room

    if room.is_direct or room.is_group:
        candidate_users = list(room.participants.all())
    elif room.project_id:
        candidate_users = list(
            User.objects.filter(project_teams__project_id=room.project_id).distinct()
        )
    elif room.company_id:
        candidate_users = list(User.objects.filter(company_id=room.company_id).distinct())
    elif room.freelancer_id:
        candidate_users = list(User.objects.filter(id=room.freelancer_id))
    else:
        candidate_users = []

    recipients = []
    for user in candidate_users:
        if user.id == message.sender_id:
            continue
        if not user.is_active or user.is_deleted or user.is_permanently_deleted:
            continue
        if room.is_group and not can_access_group_chat(user, room):
            continue
        if not _user_allows_chat_push(user):
            continue
        recipients.append(user)

    return recipients


def _build_chat_link_for_user(user, room, message_id):
    solution_name = get_user_active_solution(user.id)
    frontend_base = _normalize_base_url(get_frontend_url_for_solution(solution_name))
    if not frontend_base:
        frontend_base = _normalize_base_url(getattr(settings, "FRONTEND_URL", ""))

    if not frontend_base:
        return ""

    params = urlencode(
        {
            "message_id": message_id,
            "chat_room_id": room.id,
            "chat_scope": _chat_scope_for_room(room),
        }
    )
    return f"{frontend_base}{_chat_path_for_room(room)}?{params}"


def _resolve_reaction_recipients(message, reactor_id):
    room = message.room

    if room.is_direct or room.is_group:
        candidate_users = list(room.participants.all())
    elif room.project_id:
        candidate_users = list(
            User.objects.filter(project_teams__project_id=room.project_id).distinct()
        )
    elif room.company_id:
        candidate_users = list(User.objects.filter(company_id=room.company_id).distinct())
    elif room.freelancer_id:
        candidate_users = list(User.objects.filter(id=room.freelancer_id))
    else:
        candidate_users = []

    recipients = []
    for user in candidate_users:
        if user.id == reactor_id:
            continue
        if not user.is_active or user.is_deleted or user.is_permanently_deleted:
            continue
        if room.is_group and not can_access_group_chat(user, room):
            continue
        if not _user_allows_chat_push(user):
            continue
        recipients.append(user)

    return recipients


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def send_chat_message_push_notifications(self, message_id):
    if not getattr(settings, "CHAT_PUSH_NOTIFICATIONS_ENABLED", False):
        logger.info("Chat push skipped because feature is disabled")
        return

    try:
        message = Message.objects.select_related(
            "room",
            "room__project",
            "room__company",
            "sender",
        ).get(id=message_id)
    except Message.DoesNotExist:
        logger.info("Chat push skipped because message was not found", extra={"message_id": message_id})
        return

    if message.is_deleted:
        logger.info("Chat push skipped because message is deleted", extra={"message_id": message_id})
        return

    recipients = _resolve_recipients(message)
    if not recipients:
        logger.info(
            "Chat push skipped because no recipients matched",
            extra={"message_id": message.id, "room_id": message.room_id},
        )
        return

    recipient_ids = [user.id for user in recipients]
    token_rows = list(
        ChatPushDevice.objects.filter(user_id__in=recipient_ids, is_active=True).values(
            "user_id", "token"
        )
    )
    if not token_rows:
        logger.info(
            "Chat push skipped because no active device tokens were found",
            extra={"message_id": message.id, "room_id": message.room_id, "recipient_count": len(recipients)},
        )
        return

    tokens_by_user_id = {}
    for row in token_rows:
        tokens_by_user_id.setdefault(row["user_id"], []).append(row["token"])

    payload_template = {
        "type": "chat_message",
        "message_id": str(message.id),
        "chat_room_id": str(message.room_id),
        "chat_scope": _chat_scope_for_room(message.room),
        "sender_id": str(message.sender_id),
    }

    sender_name = message.sender.full_name or "New message"
    chat_context = _chat_context_for_room(message.room)
    if message.room.is_direct or not chat_context:
        title = sender_name
    else:
        title = f"{sender_name} in {chat_context}"

    body = _message_preview(message)

    payload_template["title"] = title
    payload_template["body"] = body
    payload_template["chat_context"] = chat_context
    mentioned_user_ids = _mentioned_user_id_set(message)

    invalid_tokens = []
    for recipient in recipients:
        recipient_tokens = tokens_by_user_id.get(recipient.id) or []
        if not recipient_tokens:
            continue

        deep_link = _build_chat_link_for_user(recipient, message.room, message.id)
        icon_base = ""
        if deep_link:
            parsed_link = urlsplit(deep_link)
            if parsed_link.scheme and parsed_link.netloc:
                icon_base = f"{parsed_link.scheme}://{parsed_link.netloc}"

        icon_url = f"{icon_base}/favicon.ico" if icon_base else None
        badge_url = f"{icon_base}/favicon.ico" if icon_base else None

        is_mentioned_recipient = recipient.id in mentioned_user_ids
        recipient_body = _mention_preview(message) if is_mentioned_recipient else body
        payload = {
            **payload_template,
            "body": recipient_body,
            "link": deep_link,
            "is_mention": "true" if is_mentioned_recipient else "false",
        }
        invalid_tokens.extend(
            send_chat_push_notifications(
                tokens=recipient_tokens,
                title=title,
                body=recipient_body,
                data_payload=payload,
                link=deep_link,
                icon=icon_url,
                badge=badge_url,
            )
        )

    if invalid_tokens:
        now = timezone.now()
        ChatPushDevice.objects.filter(token__in=set(invalid_tokens)).update(
            is_active=False,
            last_seen_at=now,
            updated_at=now,
        )

    logger.info(
        "Chat push dispatched",
        extra={
            "message_id": message.id,
            "room_id": message.room_id,
            "recipient_count": len(recipients),
        },
    )


@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def send_chat_reaction_push_notifications(self, message_id, reactor_id, emoji):
    if not getattr(settings, "CHAT_PUSH_NOTIFICATIONS_ENABLED", False):
        logger.info("Chat reaction push skipped because feature is disabled")
        return

    try:
        message = Message.objects.select_related(
            "room",
            "room__project",
            "room__company",
            "sender",
        ).get(id=message_id)
    except Message.DoesNotExist:
        logger.info(
            "Chat reaction push skipped because message was not found",
            extra={"message_id": message_id, "reactor_id": reactor_id},
        )
        return

    if message.is_deleted:
        logger.info(
            "Chat reaction push skipped because message is deleted",
            extra={"message_id": message.id, "reactor_id": reactor_id},
        )
        return

    try:
        reactor = User.objects.only(
            "id",
            "first_name",
            "last_name",
            "username",
            "email",
        ).get(id=reactor_id)
    except User.DoesNotExist:
        logger.info(
            "Chat reaction push skipped because reactor was not found",
            extra={"message_id": message.id, "reactor_id": reactor_id},
        )
        return

    recipients = _resolve_reaction_recipients(message, reactor_id)
    if not recipients:
        logger.info(
            "Chat reaction push skipped because no recipients matched",
            extra={"message_id": message.id, "room_id": message.room_id, "reactor_id": reactor_id},
        )
        return

    recipient_ids = [user.id for user in recipients]
    token_rows = list(
        ChatPushDevice.objects.filter(user_id__in=recipient_ids, is_active=True).values(
            "user_id", "token"
        )
    )
    if not token_rows:
        logger.info(
            "Chat reaction push skipped because no active device tokens were found",
            extra={"message_id": message.id, "room_id": message.room_id, "recipient_count": len(recipients)},
        )
        return

    tokens_by_user_id = {}
    for row in token_rows:
        tokens_by_user_id.setdefault(row["user_id"], []).append(row["token"])

    reactor_full_name = str(getattr(reactor, "full_name", "") or "").strip()
    if not reactor_full_name:
        first_name = str(getattr(reactor, "first_name", "") or "").strip()
        last_name = str(getattr(reactor, "last_name", "") or "").strip()
        reactor_full_name = f"{first_name} {last_name}".strip()

    reactor_name = (
        reactor_full_name
        or str(getattr(reactor, "username", "") or "").strip()
        or str(getattr(reactor, "email", "") or "").split("@")[0].strip()
        or "Someone"
    )
    chat_context = _chat_context_for_room(message.room)
    if message.room.is_direct or not chat_context:
        title = reactor_name
    else:
        title = f"{reactor_name} in {chat_context}"

    safe_emoji = str(emoji or "").strip()[:32]
    if safe_emoji:
        body = f"Reacted {safe_emoji} to your message"
    else:
        body = "Reacted to your message"

    payload_template = {
        "type": "chat_reaction",
        "message_id": str(message.id),
        "chat_room_id": str(message.room_id),
        "chat_scope": _chat_scope_for_room(message.room),
        "sender_id": str(message.sender_id),
        "reactor_id": str(reactor.id),
        "emoji": safe_emoji,
        "title": title,
        "body": body,
        "chat_context": chat_context,
    }

    invalid_tokens = []
    for recipient in recipients:
        recipient_tokens = tokens_by_user_id.get(recipient.id) or []
        if not recipient_tokens:
            continue

        deep_link = _build_chat_link_for_user(recipient, message.room, message.id)
        icon_base = ""
        if deep_link:
            parsed_link = urlsplit(deep_link)
            if parsed_link.scheme and parsed_link.netloc:
                icon_base = f"{parsed_link.scheme}://{parsed_link.netloc}"

        icon_url = f"{icon_base}/favicon.ico" if icon_base else None
        badge_url = f"{icon_base}/favicon.ico" if icon_base else None

        payload = {
            **payload_template,
            "link": deep_link,
        }
        invalid_tokens.extend(
            send_chat_push_notifications(
                tokens=recipient_tokens,
                title=title,
                body=body,
                data_payload=payload,
                link=deep_link,
                icon=icon_url,
                badge=badge_url,
            )
        )

    if invalid_tokens:
        now = timezone.now()
        ChatPushDevice.objects.filter(token__in=set(invalid_tokens)).update(
            is_active=False,
            last_seen_at=now,
            updated_at=now,
        )

    logger.info(
        "Chat reaction push dispatched",
        extra={
            "message_id": message.id,
            "room_id": message.room_id,
            "reactor_id": reactor_id,
            "recipient_count": len(recipients),
        },
    )
