from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework import serializers, status
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.permissions import ObjectAssessmentPermission
from project_management.services.object_assessment_bulk_update import (
    BULK_OBJECT_ASSESSMENT_UPDATE_FIELDS,
    MAX_BULK_OBJECT_ASSESSMENTS,
    BulkObjectAssessmentUpdateRequestSerializer,
    ObjectAssessmentBulkUpdateService,
)
from project_management.views.object_assessment import ObjectAssessmentViewSet


class BulkObjectAssessmentUpdateRequestSerializerTests(SimpleTestCase):
    def test_accepts_supported_changes_and_deduplicates_ids(self):
        changes = {
            "author": 17,
            "document_type": "Drawing",
            "status": "Draft",
        }
        serializer = BulkObjectAssessmentUpdateRequestSerializer(
            data={"ids": [3, 1, 3], "changes": changes}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["ids"], [3, 1])
        self.assertEqual(serializer.validated_data["changes"], changes)
        self.assertEqual(set(changes), BULK_OBJECT_ASSESSMENT_UPDATE_FIELDS)

    def test_rejects_unsupported_or_empty_changes(self):
        unsupported = BulkObjectAssessmentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"category": "Design"}}
        )
        empty = BulkObjectAssessmentUpdateRequestSerializer(
            data={"ids": [1], "changes": {}}
        )

        self.assertFalse(unsupported.is_valid())
        self.assertIn("category", str(unsupported.errors["changes"]))
        self.assertFalse(empty.is_valid())
        self.assertIn("changes", empty.errors)

    def test_rejects_more_than_maximum_ids(self):
        serializer = BulkObjectAssessmentUpdateRequestSerializer(
            data={
                "ids": list(range(1, MAX_BULK_OBJECT_ASSESSMENTS + 2)),
                "changes": {"status": "Draft"},
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("ids", serializer.errors)


class ObjectAssessmentBulkUpdateServiceTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=71)
        self.request = SimpleNamespace(user=self.user)

    def _object_assessment(self, object_assessment_id, project_id=5):
        return SimpleNamespace(
            id=object_assessment_id,
            project_id=project_id,
            project=SimpleNamespace(id=project_id),
        )

    def _service(self):
        return ObjectAssessmentBulkUpdateService(
            request=self.request,
            serializer_context={"request": self.request},
        )

    @patch(
        "project_management.services.object_assessment_bulk_update."
        "transaction.on_commit"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "transaction.atomic"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessmentSerializer"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessmentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessment.objects.select_related"
    )
    def test_updates_same_project_and_schedules_one_summary(
        self,
        select_related,
        can_update_object,
        serializer_class,
        atomic,
        on_commit,
    ):
        first = self._object_assessment(1)
        second = self._object_assessment(2)
        select_related.return_value.in_bulk.return_value = {
            1: first,
            2: second,
        }
        atomic.side_effect = lambda: nullcontext()
        on_commit.side_effect = lambda callback: callback()
        service = self._service()
        service._enqueue_summary_notification = MagicMock()

        result = service.execute(
            object_assessment_ids=[1, 2],
            changes={"status": "Draft"},
        )

        self.assertEqual(result, {"updated_ids": [1, 2], "failures": []})
        self.assertTrue(first._suppress_object_assessment_notifications)
        self.assertTrue(second._suppress_object_assessment_notifications)
        self.assertEqual(serializer_class.call_count, 2)
        can_update_object.assert_has_calls(
            [call(self.user, first), call(self.user, second)]
        )
        service._enqueue_summary_notification.assert_called_once()

    @patch(
        "project_management.services.object_assessment_bulk_update."
        "transaction.on_commit"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "transaction.atomic"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessmentSerializer"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessmentPermission.can_update_object"
    )
    @patch(
        "project_management.services.object_assessment_bulk_update."
        "ObjectAssessment.objects.select_related"
    )
    def test_reports_permission_project_validation_and_missing_failures(
        self,
        select_related,
        can_update_object,
        serializer_class,
        atomic,
        on_commit,
    ):
        allowed = self._object_assessment(1)
        denied = self._object_assessment(2)
        other_project = self._object_assessment(3, project_id=9)
        invalid = self._object_assessment(4)
        select_related.return_value.in_bulk.return_value = {
            1: allowed,
            2: denied,
            3: other_project,
            4: invalid,
        }
        can_update_object.side_effect = [True, False, True, True]
        atomic.side_effect = lambda: nullcontext()
        serializer_class.return_value.is_valid.side_effect = [
            None,
            serializers.ValidationError({"status": ["Invalid status."]}),
        ]

        result = self._service().execute(
            object_assessment_ids=[1, 2, 3, 4, 5],
            changes={"status": "Unknown"},
        )

        self.assertEqual(result["updated_ids"], [1])
        self.assertEqual(
            result["failures"],
            [
                {
                    "id": 2,
                    "error": (
                        "You do not have permission to update this "
                        "Object Assessment."
                    ),
                },
                {
                    "id": 3,
                    "error": (
                        "All Object Assessments must belong to the same project."
                    ),
                },
                {"id": 4, "error": "status: Invalid status."},
                {"id": 5, "error": "Object Assessment not found."},
            ],
        )
        on_commit.assert_called_once()


class ObjectAssessmentBulkUpdateEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(id=91, is_authenticated=True)
        self.view = ObjectAssessmentViewSet.as_view({"patch": "bulk_update"})

    def test_bulk_update_url_resolves_to_object_assessment_view(self):
        match = resolve(
            "/api/project_management/object_assessment/bulk-update"
        )

        self.assertEqual(match.url_name, "objectassessment-bulk-update")

    @patch.object(ObjectAssessmentViewSet, "get_permissions", return_value=[])
    @patch(
        "project_management.views.object_assessment."
        "ObjectAssessmentBulkUpdateService"
    )
    def test_bulk_update_endpoint_returns_service_result(
        self,
        service_class,
        _get_permissions,
    ):
        service_class.return_value.execute.return_value = {
            "updated_ids": [1],
            "failures": [],
        }
        request = self.factory.patch(
            "/api/project_management/object_assessment/bulk-update",
            {"ids": [1], "changes": {"status": "Draft"}},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = self.view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated_ids"], [1])
        service_class.return_value.execute.assert_called_once_with(
            object_assessment_ids=[1],
            changes={"status": "Draft"},
        )


class ObjectAssessmentPermissionRoutingTests(SimpleTestCase):
    def test_bulk_helper_uses_project_update_permission(self):
        user = SimpleNamespace(id=1)
        object_assessment = self._object_assessment()
        permission = ObjectAssessmentPermission(action="update", scope="project")

        with patch.object(
            ObjectAssessmentPermission,
            "_check_update_permission_project",
            return_value=True,
        ) as check_update:
            allowed = permission.can_update_object(user, object_assessment)

        self.assertTrue(allowed)
        check_update.assert_called_once()

    @staticmethod
    def _object_assessment():
        project = SimpleNamespace(id=8)
        return SimpleNamespace(project=project)
