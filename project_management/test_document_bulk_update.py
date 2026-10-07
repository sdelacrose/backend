from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework import serializers, status
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.permissions import DocumentPermission
from project_management.services.document_bulk_update import (
    BULK_DOCUMENT_UPDATE_FIELDS,
    MAX_BULK_DOCUMENTS,
    BulkDocumentUpdateRequestSerializer,
    DocumentBulkUpdateService,
)
from project_management.views.document import DocumentViewSet


class BulkDocumentUpdateRequestSerializerTests(SimpleTestCase):
    def test_accepts_supported_changes(self):
        changes = {
            "author": 17,
            "category": "Design",
            "discipline": "Architecture",
            "discipline_group_id": "engineering",
            "discipline_option_id": "architecture",
            "document_type": "Drawing",
            "external_review_status": "Pending",
            "internal_review_status": "Approved",
            "status": "Submitted",
            "transmittal_category": "For approval",
            "verifier": 21,
            "approver": 22,
        }
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1, 2], "changes": changes}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["changes"], changes)
        self.assertEqual(set(changes), BULK_DOCUMENT_UPDATE_FIELDS)

    def test_deduplicates_ids_in_original_order(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [3, 1, 3, 2, 1], "changes": {"status": "Draft"}}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["ids"], [3, 1, 2])

    def test_rejects_more_than_maximum_ids(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={
                "ids": list(range(1, MAX_BULK_DOCUMENTS + 2)),
                "changes": {"status": "Draft"},
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("ids", serializer.errors)

    def test_rejects_empty_ids(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [], "changes": {"status": "Draft"}}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("ids", serializer.errors)

    def test_rejects_empty_changes(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {}}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("changes", serializer.errors)

    def test_rejects_unsupported_fields(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"revision": 2, "file_name": "x.pdf"}}
        )

        self.assertFalse(serializer.is_valid())
        error = str(serializer.errors["changes"])
        self.assertIn("file_name", error)
        self.assertIn("revision", error)

    def test_requires_complete_discipline_change(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={
                "ids": [1],
                "changes": {
                    "discipline": "Architecture",
                    "discipline_option_id": "architecture",
                },
            }
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("discipline_group_id", str(serializer.errors["changes"]))

    def test_defers_submitted_review_validation_to_each_document(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"status": "Submitted"}}
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_accepts_submitted_with_external_review_status(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={
                "ids": [1],
                "changes": {
                    "status": "Submitted",
                    "external_review_status": "Pending",
                },
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_non_object_changes(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": ["status", "Draft"]}
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("changes", serializer.errors)

    def test_dry_run_defaults_to_false_and_accepts_true(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"status": "Draft"}}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.validated_data["dry_run"])

        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"status": "Draft"}, "dry_run": True}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertTrue(serializer.validated_data["dry_run"])

    def test_verifier_and_approver_accept_null_to_clear_but_reject_absence_of_other_rules(self):
        serializer = BulkDocumentUpdateRequestSerializer(
            data={"ids": [1], "changes": {"verifier": None, "approver": 9}}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertIsNone(serializer.validated_data["changes"]["verifier"])
        self.assertEqual(serializer.validated_data["changes"]["approver"], 9)


class DocumentBulkUpdateServiceTests(SimpleTestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=71)
        self.request = SimpleNamespace(user=self.user)

    def _document(self, document_id, *, project_id=5, company_id=None):
        return SimpleNamespace(
            id=document_id,
            folder=SimpleNamespace(
                project_id=project_id,
                company_profile_id=company_id,
                freelancer_id=None,
            ),
        )

    def _service(self):
        return DocumentBulkUpdateService(
            request=self.request,
            serializer_context={"request": self.request},
        )

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_updates_same_scope_documents_and_schedules_one_summary(
        self,
        select_related,
        can_update_object,
        serializer_class,
        atomic,
        on_commit,
    ):
        first = self._document(1)
        second = self._document(2)
        select_related.return_value.in_bulk.return_value = {
            1: first,
            2: second,
        }
        atomic.side_effect = lambda: nullcontext()
        on_commit.side_effect = lambda callback: callback()
        service = self._service()
        service._enqueue_summary_notification = MagicMock()

        result = service.execute(
            document_ids=[1, 2],
            changes={"status": "Draft"},
        )

        self.assertEqual(
            result, {"updated_ids": [1, 2], "failures": [], "dry_run": False},
        )
        self.assertTrue(first._suppress_document_notifications)
        self.assertTrue(second._suppress_document_notifications)
        self.assertEqual(serializer_class.call_count, 2)
        serializer_class.assert_any_call(
            first,
            data={"status": "Draft"},
            partial=True,
            context={"request": self.request, "user": self.user},
        )
        serializer_class.assert_any_call(
            second,
            data={"status": "Draft"},
            partial=True,
            context={"request": self.request, "user": self.user},
        )
        service._enqueue_summary_notification.assert_called_once()
        can_update_object.assert_has_calls(
            [call(self.user, first), call(self.user, second)]
        )

    # --- FINISH-B: verifier/approver (DocumentControl) + dry_run -----------------------------

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.save_document_control")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_verifier_and_approver_route_through_save_document_control(
        self,
        select_related,
        can_update_object,
        serializer_class,
        save_document_control,
        atomic,
        on_commit,
    ):
        # verifier/approver-only change: no plain Document field present, so the
        # DocumentSerializer is never even instantiated for this document.
        document = self._document(1)
        select_related.return_value.in_bulk.return_value = {1: document}
        atomic.side_effect = lambda: nullcontext()
        resolved_verifier = SimpleNamespace(id=10)
        resolved_approver = SimpleNamespace(id=20)
        serializer_class._resolve_user.side_effect = (
            lambda value: {10: resolved_verifier, 20: resolved_approver}[value]
        )

        result = self._service().execute(
            document_ids=[1],
            changes={"verifier": 10, "approver": 20},
        )

        self.assertEqual(result["updated_ids"], [1])
        self.assertEqual(result["failures"], [])
        serializer_class.assert_not_called()
        save_document_control.assert_called_once_with(
            document, {"verifier": resolved_verifier, "approver": resolved_approver},
        )

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.save_document_control")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_mixed_plain_and_control_changes_both_applied_to_the_same_document(
        self,
        select_related,
        can_update_object,
        serializer_class,
        save_document_control,
        atomic,
        on_commit,
    ):
        document = self._document(1)
        select_related.return_value.in_bulk.return_value = {1: document}
        atomic.side_effect = lambda: nullcontext()
        on_commit.side_effect = lambda callback: callback()
        resolved_approver = SimpleNamespace(id=20)
        serializer_class._resolve_user.return_value = resolved_approver
        service = self._service()
        service._enqueue_summary_notification = MagicMock()

        result = service.execute(
            document_ids=[1],
            changes={"status": "Draft", "approver": 20},
        )

        self.assertEqual(result["updated_ids"], [1])
        serializer_class.assert_called_once_with(
            document,
            data={"status": "Draft"},
            partial=True,
            context={"request": self.request, "user": self.user},
        )
        serializer_class.return_value.save.assert_called_once()
        save_document_control.assert_called_once_with(
            document, {"approver": resolved_approver},
        )

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.save_document_control")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_dry_run_reports_eligibility_without_writing_anything(
        self,
        select_related,
        can_update_object,
        serializer_class,
        save_document_control,
        atomic,
        on_commit,
    ):
        document = self._document(1)
        select_related.return_value.in_bulk.return_value = {1: document}
        atomic.side_effect = lambda: nullcontext()
        service = self._service()
        service._enqueue_summary_notification = MagicMock()

        result = service.execute(
            document_ids=[1],
            changes={"status": "Draft", "approver": 20},
            dry_run=True,
        )

        # Still reports the document as (would-be) eligible/updated -- permission + validation
        # both still ran -- but nothing was actually written or scheduled.
        self.assertEqual(
            result, {"updated_ids": [1], "failures": [], "dry_run": True},
        )
        serializer_class.return_value.is_valid.assert_called_once_with(
            raise_exception=True,
        )
        serializer_class.return_value.save.assert_not_called()
        save_document_control.assert_not_called()
        service._enqueue_summary_notification.assert_not_called()

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object"
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_returns_permission_missing_and_validation_failures(
        self,
        select_related,
        can_update_object,
        serializer_class,
        atomic,
        on_commit,
    ):
        denied = self._document(1)
        invalid = self._document(2)
        select_related.return_value.in_bulk.return_value = {
            1: denied,
            2: invalid,
        }
        can_update_object.side_effect = [False, True]
        atomic.side_effect = lambda: nullcontext()
        serializer_class.return_value.is_valid.side_effect = (
            serializers.ValidationError({"status": ["Invalid status."]})
        )

        result = self._service().execute(
            document_ids=[1, 2, 3],
            changes={"status": "Unknown"},
        )

        self.assertEqual(result["updated_ids"], [])
        self.assertEqual(
            result["failures"],
            [
                {
                    "id": 1,
                    "error": (
                        "You do not have permission to update this Document."
                    ),
                },
                {"id": 2, "error": "status: Invalid status."},
                {"id": 3, "error": "Document not found."},
            ],
        )
        on_commit.assert_not_called()

    @patch("project_management.services.document_bulk_update.transaction.on_commit")
    @patch("project_management.services.document_bulk_update.transaction.atomic")
    @patch("project_management.services.document_bulk_update.DocumentSerializer")
    @patch(
        "project_management.services.document_bulk_update."
        "DocumentPermission.can_update_object",
        return_value=True,
    )
    @patch(
        "project_management.services.document_bulk_update."
        "Document.objects.select_related"
    )
    def test_rejects_documents_after_scope_changes(
        self,
        select_related,
        _can_update_object,
        serializer_class,
        atomic,
        on_commit,
    ):
        project_document = self._document(1)
        company_document = self._document(2, project_id=None, company_id=9)
        select_related.return_value.in_bulk.return_value = {
            1: project_document,
            2: company_document,
        }
        atomic.side_effect = lambda: nullcontext()

        result = self._service().execute(
            document_ids=[1, 2],
            changes={"status": "Draft"},
        )

        self.assertEqual(result["updated_ids"], [1])
        self.assertEqual(
            result["failures"],
            [
                {
                    "id": 2,
                    "error": "All Documents must belong to the same scope.",
                }
            ],
        )
        self.assertEqual(serializer_class.call_count, 1)
        on_commit.assert_called_once()


class DocumentBulkUpdateEndpointTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(id=91, is_authenticated=True)
        self.view = DocumentViewSet.as_view({"patch": "bulk_update"})

    def test_bulk_update_url_resolves_to_document_view(self):
        match = resolve("/api/project_management/document/bulk-update")

        self.assertEqual(match.url_name, "document-bulk-update")

    @patch.object(DocumentViewSet, "get_permissions", return_value=[])
    @patch("project_management.views.document.DocumentBulkUpdateService")
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
            "/api/project_management/document/bulk-update",
            {"ids": [1], "changes": {"status": "Draft"}},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = self.view(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated_ids"], [1])
        service_class.return_value.execute.assert_called_once_with(
            document_ids=[1],
            changes={"status": "Draft"},
            dry_run=False,
        )

    @patch.object(DocumentViewSet, "get_permissions", return_value=[])
    @patch("project_management.views.document.DocumentBulkUpdateService")
    def test_invalid_request_does_not_call_service(
        self,
        service_class,
        _get_permissions,
    ):
        request = self.factory.patch(
            "/api/project_management/document/bulk-update",
            {"ids": [], "changes": {}},
            format="json",
        )
        force_authenticate(request, user=self.user)

        response = self.view(request)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        service_class.assert_not_called()


class DocumentPermissionRoutingTests(SimpleTestCase):
    def test_company_document_uses_company_update_check(self):
        user = SimpleNamespace(id=1)
        document = SimpleNamespace(
            folder=SimpleNamespace(company_profile=object()),
        )
        permission = DocumentPermission(action="update", scope="mixed")

        with patch.object(
            DocumentPermission,
            "_check_update_permission",
            return_value=True,
        ) as check_update:
            allowed = permission.can_update_object(user, document)

        self.assertTrue(allowed)
        check_update.assert_called_once_with(user, document)

    def test_project_document_uses_project_update_check(self):
        user = SimpleNamespace(id=1)
        project = SimpleNamespace(id=8)
        document = SimpleNamespace(
            folder=SimpleNamespace(company_profile=None, project=project),
        )
        permission = DocumentPermission(action="update", scope="mixed")

        with patch.object(
            DocumentPermission,
            "_check_update_permission_project",
            return_value=True,
        ) as check_update:
            allowed = permission.can_update_object(user, document)

        self.assertTrue(allowed)
        check_update.assert_called_once_with(
            user,
            document,
            {"project": project},
        )
