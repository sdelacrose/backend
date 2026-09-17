from django.db import transaction

from .models import Message, MessageRead


READ_BATCH_SIZE = 1000


def get_unread_messages(user, room_ids):
    read_message_ids = MessageRead.objects.filter(
        user=user,
        message__room_id__in=room_ids,
    ).values_list("message_id", flat=True)
    return (
        Message.objects.filter(room_id__in=room_ids)
        .exclude(sender=user)
        .exclude(id__in=read_message_ids)
    )


def count_unread_messages(user, room_ids):
    return get_unread_messages(user, room_ids).count()


@transaction.atomic
def mark_rooms_as_read(user, room_ids):
    normalized_room_ids = list(dict.fromkeys(room_ids))
    if not normalized_room_ids:
        return 0

    unread_queryset = get_unread_messages(user, normalized_room_ids)
    unread_message_ids = list(unread_queryset.values_list("id", flat=True))
    if not unread_message_ids:
        return 0

    MessageRead.objects.bulk_create(
        [
            MessageRead(user=user, message_id=message_id)
            for message_id in unread_message_ids
        ],
        ignore_conflicts=True,
        batch_size=READ_BATCH_SIZE,
    )

    remaining_count = count_unread_messages(user, normalized_room_ids)
    return max(0, len(unread_message_ids) - remaining_count)
