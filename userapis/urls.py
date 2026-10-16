from django.urls import path, include

from .views import (
    AccessibleMailboxesView,
    ActionCountersView,
    CalendarEventViewSet,
    DisplayConfigurationView,
    FeedbackNotificationView,
    FeedbackViewSet,
    MailboxConnectionViewSet,
    MailboxMessageViewSet,
    MailboxSearchView,
    MailboxImportView,
    MailboxPreviewView,
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
router.register('mailbox-connections', MailboxConnectionViewSet, basename="mailbox-connection")
router.register('mailbox-messages', MailboxMessageViewSet, basename="mailbox-message")

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
    path("mailbox-search", MailboxSearchView.as_view(), name="mailbox_search"),
    path("mailbox-search/", MailboxSearchView.as_view()),
    path("mailbox-import", MailboxImportView.as_view(), name="mailbox_import"),
    path("mailbox-import/", MailboxImportView.as_view()),
    path("mailbox-preview", MailboxPreviewView.as_view(), name="mailbox_preview"),
    path("mailbox-preview/", MailboxPreviewView.as_view()),
    path(
        "mailboxes/accessible",
        AccessibleMailboxesView.as_view(),
        name="accessible_mailboxes",
    ),
]
