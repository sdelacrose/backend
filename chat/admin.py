from django.contrib import admin
from .models import (
    ChatRoom,
    MatrixReactionMap,
    MatrixMessageMap,
    MatrixRoomMap,
    MatrixUserMap,
    Message,
    MessageReaction,
    MessageRead,
)
# Register your models here.
admin.site.register(ChatRoom)
admin.site.register(Message)
admin.site.register(MessageRead)
admin.site.register(MatrixUserMap)
admin.site.register(MatrixRoomMap)
admin.site.register(MatrixMessageMap)
admin.site.register(MessageReaction)
admin.site.register(MatrixReactionMap)
