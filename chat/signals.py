from django.db.models.signals import post_save
from django.dispatch import receiver

from .matrix_service import matrix_service
from .metrics import emit_chat_metric
from .models import ChatRoom


@receiver(post_save, sender=ChatRoom)
def ensure_matrix_room_on_chatroom_create(sender, instance: ChatRoom, created: bool, **kwargs):
    if not created:
        return

    matrix_room_map = matrix_service.ensure_room(instance)
    if matrix_room_map:
        emit_chat_metric(
            "matrix_room_created_on_chatroom_create",
            room_id=instance.id,
            room_type=matrix_room_map.room_type,
        )
