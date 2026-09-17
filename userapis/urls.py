from django.urls import path, include

from .views import (
    ActionCountersView,
    CalendarEventViewSet,
    DisplayConfigurationView,
    FeedbackNotificationView,
    FeedbackViewSet,
    MicrosoftAuthViewSet,
    UserDashboardConfigurationViewSet,
    UserInfoViewSet,
    UserRoleViewSet,
    UserSearchView,
)
from project_management.slash_router import OptionalSlashRouter


router = OptionalSlashRouter()
router.register('user_info', UserInfoViewSet)
router.register('user-dashboad-config', UserDashboardConfigurationViewSet)
router.register("events", CalendarEventViewSet)
router.register('auth/outlook', MicrosoftAuthViewSet, basename="auth-outlook")
router.register('feedback', FeedbackViewSet, basename="feedback")
router.register('user-role', UserRoleViewSet, basename="user-role")

urlpatterns = [
    path('', include(router.urls)),
    path("user-search", UserSearchView.as_view(), name="user_search"),
    path("action-counters", ActionCountersView.as_view(), name="action_counters"),
    path(
        "display-configuration",
        DisplayConfigurationView.as_view(),
        name="display_configuration",
    ),
    path("notify_user_on_feedback_update", FeedbackNotificationView.as_view(), name="feedback_notification"),
]
