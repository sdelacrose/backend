from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.http import Http404
from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework import serializers, status
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import Document, DocumentControl
from project_management.permissions import DocumentPermission
from project_management.serializers.document_register import (
    DocumentControlWriteSerializer,
    DocumentRegisterRowSerializer,
)
from project_management.services import document_register as register_service
from project_management.views.document_register import DocumentRegisterViewSet


class DocumentControlModelTests(SimpleTestCase):
    def test_is_one_to_one_with_document_and_does_not_touch_document_fields(self):
        field = DocumentControl._meta.get_field("document")

        self.assertTrue(field.one_to_one)
        self.assertEqual(field.remote_field.related_name, "control")
        self.assertIs(field.related_model, Document)
        self.assertNotIn(
            "control", {f.name for f in Document._meta.get_fields() if f.concrete}
        )

    def test_defaults_and_lot1_choices(self):
        self.assertEqual(DocumentControl.DEFAULT_REVISION, "A")
        self.assertEqual(
            DocumentControl._meta.get_field("revision").default, "A"
        )
        self.assertEqual(
            {value for value, _ in DocumentControl.Status.choices},
            {"DRAFT", "ACTIVE", "ARCHIVED"},
        )
        self.assertIn("Technical Note", DocumentControl.DocumentType.values)
        self.assertIn("Instrumentation", DocumentControl.Discipline.values)
        self.assertEqual(len(DocumentControl.DocumentType.values), 13)
        self.assertEqual(len(DocumentControl.Discipline.values), 18)

    def test_control_metadata_is_optional_except_revision_and_status(self):
        for name in ("reference", "document_type", "discipline", "owner"):
            self.assertTrue(DocumentControl._meta.get_field(name).null, name)


class RegisterUrlTests(SimpleTestCase):
    def test_register_urls_resolve(self):
        for path, name in (
            ("/api/project_management/document-register", "document-register"),
            (
                "/api/project_management/document-register/filter-options",
                "document-register-filter-options",
            ),
            (
                "/api/project_management/document-register/5",
                "document-register-detail",
            ),
            (
                "/api/project_management/document-control/9",
                "document-control-detail",
            ),
        ):
            self.assertEqual(resolve(path).url_name, name)


class RegisterScopeTests(SimpleTestCase):
    def test_register_queryset_starts_from_accessible_documents(self):
        request = SimpleNamespace(user=SimpleNamespace(id=1))
        accessible = MagicMock(name="accessible_qs")
        accessible.select_related.return_value.annotate.return_value = accessible

        with patch.object(
            register_service,
            "get_accessible_document_queryset",
            return_value=accessible,
        ) as get_accessible:
            register_service.get_register_queryset(request, project_id=7)

        get_accessible.assert_called_once_with(request, project_id=7)
        accessible.select_related.assert_called_once()
        accessible.filter.assert_called_once_with(folder__project_id=7)

    def test_direct_access_to_inaccessible_document_is_404(self):
        request = SimpleNamespace(user=SimpleNamespace(id=1))
        empty = MagicMock()
        empty.filter.return_value.first.return_value = None

        with patch.object(
            register_service.Document, "objects"
        ) as document_manager, patch.object(
            register_service, "get_register_queryset", return_value=empty
        ), patch.object(
            register_service.DocumentControl, "objects"
        ) as control_manager:
            document_manager.filter.return_value.values_list.return_value.first.return_value = (
                None
            )
            with self.assertRaises(Http404):
                register_service.get_register_document_or_404(
                    request, control_id=3
                )
            control_manager.get.assert_not_called()
            control_manager.filter.assert_not_called()

        empty.filter.assert_called_once_with(control__pk=3)


class OwnerCompanyIsolationTests(SimpleTestCase):
    def test_owner_queryset_is_limited_to_current_company(self):
        user = SimpleNamespace(
            id=1, pk=1, is_freelancer=False, company_id=42
        )

        with patch.object(register_service.User, "objects") as manager:
            register_service.get_allowed_owner_queryset(user)

        kwargs = manager.filter.call_args.kwargs
        self.assertEqual(kwargs["company_id"], 42)
        self.assertTrue(kwargs["is_active"])
        self.assertFalse(kwargs["is_deleted"])

    def test_freelancer_can_only_select_themselves(self):
        user = SimpleNamespace(id=5, pk=5, is_freelancer=True, company_id=None)

        with patch.object(register_service.User, "objects") as manager:
            register_service.get_allowed_owner_queryset(user)

        manager.filter.assert_called_once_with(pk=5)


class WriteSerializerTests(SimpleTestCase):
    def _serializer(self, data, document, owner_qs=None):
        return DocumentControlWriteSerializer(
            data=data,
            partial=True,
            context={"document": document, "user": SimpleNamespace(id=1)},
        )

    def _document(self, control=None, number=None):
        document = SimpleNamespace(
            id=10,
            full_document_number=number,
            folder=SimpleNamespace(project_id=3, company_profile_id=None, freelancer_id=None),
        )
        if control is not None:
            document.control = control
        return document

    def _no_duplicates(self):
        duplicates = MagicMock()
        duplicates.exclude.return_value.exists.return_value = False
        return patch(
            "project_management.serializers.document_register."
            "DocumentControl.objects.filter",
            return_value=duplicates,
        )

    @patch("project_management.serializers.document_register.get_allowed_owner_queryset")
    def test_owner_from_another_company_is_rejected_by_backend(self, owners):
        owners.return_value.filter.return_value.first.return_value = None
        serializer = self._serializer({"owner": 999}, self._document())

        with self._no_duplicates():
            self.assertFalse(serializer.is_valid())

        self.assertIn("owner", serializer.errors)

    @patch("project_management.serializers.document_register.get_allowed_owner_queryset")
    def test_owner_from_current_company_is_accepted(self, owners):
        owner = SimpleNamespace(id=7)
        owners.return_value.filter.return_value.first.return_value = owner
        serializer = self._serializer({"owner": 7}, self._document())

        with self._no_duplicates():
            self.assertTrue(serializer.is_valid(), serializer.errors)

        self.assertIs(serializer.validated_data["owner"], owner)

    def test_first_save_reuses_existing_document_number_as_reference(self):
        serializer = self._serializer(
            {"status": "ACTIVE"}, self._document(number="PRJ-DOC-00001-00")
        )

        with self._no_duplicates():
            self.assertTrue(serializer.is_valid(), serializer.errors)

        self.assertEqual(
            serializer.validated_data["reference"], "PRJ-DOC-00001-00"
        )

    def test_invalid_status_and_blank_revision_are_rejected(self):
        serializer = self._serializer(
            {"status": "DELETED", "revision": ""}, self._document()
        )

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)
        self.assertIn("revision", serializer.errors)

    def test_duplicate_reference_and_revision_in_library_is_rejected(self):
        duplicates = MagicMock()
        duplicates.exclude.return_value.exists.return_value = True
        serializer = self._serializer(
            {"reference": "DOC-1", "revision": "A"}, self._document()
        )

        with patch(
            "project_management.serializers.document_register."
            "DocumentControl.objects.filter",
            return_value=duplicates,
        ) as filter_mock:
            self.assertFalse(serializer.is_valid())

        self.assertIn("reference", serializer.errors)
        self.assertEqual(
            filter_mock.call_args.kwargs["document__folder__project_id"], 3
        )


class SaveDocumentControlTests(SimpleTestCase):
    def test_creates_on_first_save_and_never_touches_document_or_file(self):
        document = MagicMock(name="document")
        control = SimpleNamespace(
            reference=None,
            document_type=None,
            discipline=None,
            revision="A",
            status="DRAFT",
            owner=None,
            save=MagicMock(),
        )

        with patch.object(
            register_service.DocumentControl.objects,
            "get_or_create",
            return_value=(control, True),
        ) as get_or_create:
            register_service.save_document_control(
                document, {"status": "ARCHIVED", "reference": "REF-1"}
            )

        get_or_create.assert_called_once_with(document=document)
        self.assertEqual(control.status, "ARCHIVED")
        self.assertEqual(control.reference, "REF-1")
        self.assertEqual(control.revision, "A")
        control.save.assert_called_once()
        document.save.assert_not_called()
        document.delete.assert_not_called()
        document.file.save.assert_not_called()
        document.file.delete.assert_not_called()


class RegisterRowTests(SimpleTestCase):
    def _document(self, control=None):
        document = SimpleNamespace(
            id=4,
            file_name="plan.pdf",
            file=None,
            folder_id=2,
            folder=SimpleNamespace(project=None),
            updated_at="2026-01-01T00:00:00Z",
            register_title="plan.pdf",
            register_reference="DOC-00001",
            register_updated_at="2026-01-02T00:00:00Z",
        )
        if control is not None:
            document.control = control
        return document

    def test_document_without_control_is_listed_with_empty_metadata(self):
        row = DocumentRegisterRowSerializer().to_representation(self._document())

        self.assertEqual(row["id"], 4)
        self.assertIsNone(row["control_id"])
        self.assertEqual(row["title"], "plan.pdf")
        self.assertEqual(row["reference"], "DOC-00001")
        for key in ("document_type", "discipline", "revision", "status", "owner"):
            self.assertIsNone(row[key], key)
        self.assertIsNone(row["control"])


class RegisterEndpointPermissionTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = SimpleNamespace(id=91, is_authenticated=True)
        self.view = DocumentRegisterViewSet.as_view({"patch": "partial_update"})
        self.get_view = DocumentRegisterViewSet.as_view({"get": "retrieve"})

    def _patch(self, data=None):
        request = self.factory.patch(
            "/api/project_management/document-register/1",
            data or {"status": "ARCHIVED"},
            format="json",
        )
        force_authenticate(request, user=self.user)
        return request

    @patch.object(DocumentRegisterViewSet, "get_permissions", return_value=[])
    @patch("project_management.views.document_register.save_document_control")
    @patch(
        "project_management.views.document_register.get_register_document_or_404",
        side_effect=Http404,
    )
    def test_patch_on_inaccessible_document_is_404(self, _get, save, _perms):
        response = self.view(self._patch(), document_id=1)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        save.assert_not_called()

    @patch.object(DocumentRegisterViewSet, "get_permissions", return_value=[])
    @patch(
        "project_management.views.document_register.get_register_document_or_404",
        side_effect=Http404,
    )
    def test_get_on_inaccessible_document_is_404(self, _get, _perms):
        request = self.factory.get("/api/project_management/document-control/9")
        force_authenticate(request, user=self.user)

        response = self.get_view(request, control_id=9)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @patch.object(DocumentRegisterViewSet, "get_permissions", return_value=[])
    @patch("project_management.views.document_register.save_document_control")
    @patch(
        "project_management.views.document_register.get_register_document_or_404"
    )
    def test_read_only_user_cannot_patch(self, get_document, save, _perms):
        get_document.return_value = SimpleNamespace(
            folder=SimpleNamespace(company_profile=object())
        )

        with patch.object(
            DocumentPermission, "_check_update_permission", return_value=False
        ):
            response = self.view(self._patch(), document_id=1)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        save.assert_not_called()

    @patch.object(DocumentRegisterViewSet, "get_permissions", return_value=[])
    @patch("project_management.views.document_register.save_document_control")
    @patch(
        "project_management.views.document_register.get_register_document_or_404"
    )
    def test_user_allowed_to_update_document_can_patch(
        self, get_document, save, _perms
    ):
        document = SimpleNamespace(
            id=1,
            full_document_number=None,
            file=None,
            file_name="a.pdf",
            folder_id=2,
            updated_at="2026-01-01T00:00:00Z",
            folder=SimpleNamespace(
                company_profile=object(),
                company_profile_id=5,
                project=None,
                project_id=None,
                freelancer_id=None,
            ),
        )
        get_document.return_value = document
        duplicates = MagicMock()
        duplicates.exclude.return_value.exists.return_value = False

        with patch.object(
            DocumentPermission, "_check_update_permission", return_value=True
        ), patch(
            "project_management.serializers.document_register."
            "DocumentControl.objects.filter",
            return_value=duplicates,
        ):
            response = self.view(self._patch(), document_id=1)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[1]["status"], "ARCHIVED")
