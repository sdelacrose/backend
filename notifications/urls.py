from rest_framework.routers import DefaultRouter
from django.urls import path, include

from notifications.views import NotificationViewSet, PreferenceViewSet

router = DefaultRouter(trailing_slash=False)
router.register('notification', NotificationViewSet)
router.register('preference', PreferenceViewSet)

urlpatterns = [
    path('', include(router.urls)),
]