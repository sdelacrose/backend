from django.urls import path, include
from .views import (
    ChatPresenceViewSet,
    ChatPushTokenViewSet,
    ChatRoomViewSet,
    MessageViewSet,
)
from project_management.slash_router import OptionalSlashRouter

router = OptionalSlashRouter()

router.register(r'chatrooms', ChatRoomViewSet, basename='chatrooms')
router.register(r'messages', MessageViewSet, basename='messages')
router.register(r'push-tokens', ChatPushTokenViewSet, basename='chat-push-tokens')
router.register(r'presence', ChatPresenceViewSet, basename='chat-presence')

urlpatterns = [
    path('', include(router.urls)),
]
