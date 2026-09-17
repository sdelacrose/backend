from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from project_management.signals import notify_on_object_assessment_save
from project_management.tasks import (
    send_bulk_object_assessment_update_notification,
)


class ObjectAssessmentBulkNotificationTests(SimpleTestCase):
    @patch("project_management.signals.get_project_recipients")
    def test_bulk_update_suppresses_per_record_notification(
        self,
        get_project_recipients,
    ):
        object_assessment = SimpleNamespace(
            _suppress_object_assessment_notifications=True,
        )

        notify_on_object_assessment_save(
            sender=object(),
            instance=object_assessment,
            created=False,
        )

        get_project_recipients.assert_not_called()

    @patch(
        "project_management.services.object_assessment_notifications."
        "send_bulk_object_assessment_update_summary"
    )
    def test_celery_task_delegates_to_notification_service(self, send_summary):
        send_bulk_object_assessment_update_notification(
            [1, 2],
            7,
            ["status"],
        )

        send_summary.assert_called_once_with(
            object_assessment_ids=[1, 2],
            actor_id=7,
            changed_fields=["status"],
        )
