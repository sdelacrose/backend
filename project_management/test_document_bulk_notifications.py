from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from project_management.services.document_notifications import (
    send_bulk_document_update_summary,
)
from project_management.signals import notify_on_document_save
from project_management.tasks import send_bulk_document_update_notification


class DocumentBulkNotificationSignalTests(SimpleTestCase):
    @patch("project_management.signals.get_document_recipients")
    def test_bulk_update_suppresses_ordinary_document_notification(
        self,
        get_recipients,
    ):
        document = SimpleNamespace(
            id=11,
            tracker=SimpleNamespace(has_changed=lambda _field: False),
            _suppress_document_notifications=True,
        )

        notify_on_document_save(
            sender=object(),
            instance=document,
            created=False,
        )

        get_recipients.assert_not_called()


class DocumentBulkNotificationSummaryTests(SimpleTestCase):
    @patch(
        "project_management.services.document_notifications."
        "create_notification_with_recipients"
    )
    @patch(
        "project_management.services.document_notifications."
        "ContentType.objects.get_for_model",
        return_value="content-type",
    )
    @patch(
        "project_management.services.document_notifications."
        "should_notify_user_comprehensive",
        return_value=True,
    )
    @patch(
        "project_management.services.document_notifications."
        "get_validate_recipients"
    )
    @patch(
        "project_management.services.document_notifications."
        "_document_recipients"
    )
    @patch(
        "project_management.services.document_notifications."
        "User.objects.filter"
    )
    @patch(
        "project_management.services.document_notifications."
        "_document_queryset"
    )
    def test_summary_notifies_eligible_recipients_once(
        self,
        document_queryset,
        user_filter,
        document_recipients,
        validate_recipients,
        should_notify,
        get_content_type,
        create_notification,
    ):
        actor = SimpleNamespace(id=7, full_name="Bulk Editor")
        recipient = MagicMock()
        recipient.id = 8
        project = SimpleNamespace(pk=21)
        folder = SimpleNamespace(
            project=project,
            project_id=project.pk,
            company_profile=None,
            company_profile_id=None,
            freelancer=None,
            freelancer_id=None,
        )
        document = SimpleNamespace(id=31, folder=folder)
        queryset = MagicMock()
        queryset.filter.return_value.order_by.return_value = [document]
        document_queryset.return_value = queryset
        actor_queryset = MagicMock()
        actor_queryset.first.return_value = actor
        recipient_queryset = MagicMock()
        user_filter.side_effect = [actor_queryset, recipient_queryset]
        document_recipients.return_value = {recipient}
        validate_recipients.return_value = {recipient}

        send_bulk_document_update_summary(
            document_ids=[document.id],
            actor_id=actor.id,
            changed_fields=["status"],
        )

        should_notify.assert_called_once_with(
            recipient,
            "documents",
            document,
            action_type="update",
            project=project,
        )
        get_content_type.assert_called_once_with(project)
        create_notification.assert_called_once_with(
            message="1 Documents were updated by Bulk Editor (fields: Status).",
            content_type="content-type",
            object_id=project.pk,
            content_object=project,
            notification_type="documents",
            action_type="update",
            recipients=recipient_queryset,
            project=project,
            bypass_preference_check=True,
        )

    @patch(
        "project_management.services.document_notifications."
        "_document_queryset"
    )
    @patch(
        "project_management.services.document_notifications."
        "create_notification_with_recipients"
    )
    def test_missing_documents_do_not_create_summary(
        self,
        create_notification,
        document_queryset,
    ):
        queryset = MagicMock()
        queryset.filter.return_value.order_by.return_value = []
        document_queryset.return_value = queryset

        send_bulk_document_update_summary(
            document_ids=[404],
            actor_id=7,
            changed_fields=["status"],
        )

        create_notification.assert_not_called()

    @patch(
        "project_management.services.document_notifications."
        "send_bulk_document_update_summary"
    )
    def test_celery_task_delegates_to_notification_service(self, send_summary):
        send_bulk_document_update_notification(
            [1, 2],
            7,
            ["status"],
        )

        send_summary.assert_called_once_with(
            document_ids=[1, 2],
            actor_id=7,
            changed_fields=["status"],
        )
