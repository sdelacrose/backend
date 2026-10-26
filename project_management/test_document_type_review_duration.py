"""L3H-1: Document Review Durations configuration -- targeted tests only. CONFIGURATION ONLY:
no due date, no snapshot, no overdue computation, no Client Review/Supplier comment-return
workflow (those remain L3H-2/L3H-3/future lots), deliberately not covered here.
"""

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from rest_framework.test import APIRequestFactory, force_authenticate

from project_management.models import DocumentTypeReviewDuration
from project_management.models.document_type_review_duration import (
    DEFAULT_REVIEW_DURATION_DAYS,
    DURATION_FIELD_NAMES,
)
from project_management.services.document_type_review_duration import (
    MAX_BULK_REVIEW_DURATION_ROWS,
    assert_document_type_known,
    bulk_upsert_review_durations,
    list_effective_review_durations,
    resolve_review_durations,
    upsert_review_duration,
)
from project_management.test_document_control_reference import ProjectFixtureMixin
from project_management.test_document_workflow import WorkflowTestBase
from project_management.views.document_type_review_duration import (
    DocumentReviewDurationViewSet,
)

PERMISSION_TARGET = "project_management.views.document_type_review_duration.ECPMPConfigPermission"


class ReviewDurationBase(ProjectFixtureMixin, WorkflowTestBase):
    pass


# A/B/C. Project + Document Type configuration create / per-project independence / uniqueness.
class CreateAndUniquenessTests(ReviewDurationBase):
    def test_project_document_type_configuration_can_be_created(self):
        project, _folder = self.make_project("Project A")
        row = DocumentTypeReviewDuration.objects.create(
            project=project, document_type="Drawing", supplier_review_duration_days=10,
        )
        self.assertIsNotNone(row.pk)

    def test_same_document_type_has_independent_values_in_two_projects(self):
        project_a, _ = self.make_project("Project A2")
        project_b, _ = self.make_project("Project B2")
        DocumentTypeReviewDuration.objects.create(
            project=project_a, document_type="Drawing", supplier_review_duration_days=10,
        )
        DocumentTypeReviewDuration.objects.create(
            project=project_b, document_type="Drawing", supplier_review_duration_days=20,
        )
        effective_a = resolve_review_durations(project=project_a, document_type="Drawing")
        effective_b = resolve_review_durations(project=project_b, document_type="Drawing")
        self.assertEqual(effective_a["supplier_review_duration_effective_days"], 10)
        self.assertEqual(effective_b["supplier_review_duration_effective_days"], 20)

    def test_duplicate_project_document_type_rejected(self):
        project, _folder = self.make_project("Project C")
        DocumentTypeReviewDuration.objects.create(project=project, document_type="Drawing")
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                DocumentTypeReviewDuration.objects.create(
                    project=project, document_type="Drawing"
                )


# D. Each of the four values configurable independently.
class FourIndependentValuesTests(ReviewDurationBase):
    def test_each_of_four_values_configured_independently(self):
        project, _folder = self.make_project("Project D")
        row = upsert_review_duration(
            project=project, document_type="Drawing",
            values={
                "supplier_review_duration_days": 10,
                "client_review_duration_days": 20,
                "internal_client_comment_integration_days": 7,
                "supplier_client_comment_return_days": 15,
            },
        )
        self.assertEqual(row.supplier_review_duration_days, 10)
        self.assertEqual(row.client_review_duration_days, 20)
        self.assertEqual(row.internal_client_comment_integration_days, 7)
        self.assertEqual(row.supplier_client_comment_return_days, 15)

    def test_editing_one_duration_does_not_change_the_other_three(self):
        project, _folder = self.make_project("Project D2")
        upsert_review_duration(
            project=project, document_type="Drawing",
            values={
                "supplier_review_duration_days": 10,
                "client_review_duration_days": 20,
                "internal_client_comment_integration_days": 7,
                "supplier_client_comment_return_days": 15,
            },
        )
        row = upsert_review_duration(
            project=project, document_type="Drawing",
            values={"supplier_review_duration_days": 11},
        )
        self.assertEqual(row.supplier_review_duration_days, 11)
        self.assertEqual(row.client_review_duration_days, 20)
        self.assertEqual(row.internal_client_comment_integration_days, 7)
        self.assertEqual(row.supplier_client_comment_return_days, 15)


# E/F/G/H. Null resolution for each of the four fields -> system default.
class DefaultResolutionTests(ReviewDurationBase):
    def test_null_supplier_review_resolves_to_default(self):
        project, _folder = self.make_project("Project E")
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(
            effective["supplier_review_duration_effective_days"], DEFAULT_REVIEW_DURATION_DAYS
        )

    def test_null_client_review_resolves_to_default(self):
        project, _folder = self.make_project("Project F")
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(
            effective["client_review_duration_effective_days"], DEFAULT_REVIEW_DURATION_DAYS
        )

    def test_null_internal_client_comment_integration_resolves_to_default(self):
        project, _folder = self.make_project("Project G")
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(
            effective["internal_client_comment_integration_effective_days"],
            DEFAULT_REVIEW_DURATION_DAYS,
        )

    def test_null_supplier_client_comment_return_resolves_to_default(self):
        project, _folder = self.make_project("Project H")
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(
            effective["supplier_client_comment_return_effective_days"],
            DEFAULT_REVIEW_DURATION_DAYS,
        )

    def test_default_resolution_matches_system_default_15(self):
        self.assertEqual(DEFAULT_REVIEW_DURATION_DAYS, 15)


# I/J. Configured override resolves correctly; mixed null/override resolves independently.
class OverrideResolutionTests(ReviewDurationBase):
    def test_configured_override_resolves_correctly(self):
        project, _folder = self.make_project("Project I")
        upsert_review_duration(
            project=project, document_type="Drawing",
            values={"supplier_review_duration_days": 10},
        )
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(effective["supplier_review_duration_days"], 10)
        self.assertEqual(effective["supplier_review_duration_effective_days"], 10)

    def test_mixed_null_and_override_row_resolves_each_independently(self):
        project, _folder = self.make_project("Project J")
        DocumentTypeReviewDuration.objects.create(
            project=project, document_type="Drawing",
            supplier_review_duration_days=10,
            client_review_duration_days=None,
            internal_client_comment_integration_days=7,
            supplier_client_comment_return_days=None,
        )
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(effective["supplier_review_duration_effective_days"], 10)
        self.assertEqual(effective["client_review_duration_effective_days"], 15)
        self.assertEqual(effective["internal_client_comment_integration_effective_days"], 7)
        self.assertEqual(effective["supplier_client_comment_return_effective_days"], 15)


# K/L. Invalid values rejected (0, negative).
class ValidationTests(ReviewDurationBase):
    def test_zero_rejected(self):
        project, _folder = self.make_project("Project K")
        with self.assertRaises(ValidationError):
            upsert_review_duration(
                project=project, document_type="Drawing",
                values={"supplier_review_duration_days": 0},
            )

    def test_negative_rejected(self):
        project, _folder = self.make_project("Project L")
        with self.assertRaises(ValidationError):
            upsert_review_duration(
                project=project, document_type="Drawing",
                values={"supplier_review_duration_days": -5},
            )

    def test_non_integer_rejected(self):
        project, _folder = self.make_project("Project L2")
        with self.assertRaises(ValidationError):
            upsert_review_duration(
                project=project, document_type="Drawing",
                values={"supplier_review_duration_days": "ten"},
            )
        with self.assertRaises(ValidationError):
            upsert_review_duration(
                project=project, document_type="Drawing",
                values={"supplier_review_duration_days": 2.5},
            )


# M. Invalid/non-project Document Type rejected.
class DocumentTypeValidationTests(ReviewDurationBase):
    def test_unknown_document_type_rejected(self):
        project, _folder = self.make_project("Project M")
        with self.assertRaises(ValidationError):
            upsert_review_duration(
                project=project, document_type="NotAConfiguredType",
                values={"supplier_review_duration_days": 10},
            )

    def test_label_code_pair_in_document_types_is_handled(self):
        # ProjectFixtureMixin.make_project configures document_types as "Label:CODE" pairs
        # (["Drawing:DWG", "Report:REP"]) -- the configured document_type here is the plain
        # Label ("Drawing"), matching the same choice.split(":")[0] convention
        # DocumentSerializer.validate_document_type already uses (L3H-1 section 11).
        project, _folder = self.make_project("Project M2")
        assert_document_type_known(project, "Drawing")
        assert_document_type_known(project, "Report")
        with self.assertRaises(ValidationError):
            assert_document_type_known(project, "Drawing:DWG")


# N/O/P. Permission enforcement + project isolation via the API view.
class ApiPermissionTests(ReviewDurationBase):
    def _list_request(self, project, as_user=None):
        factory = APIRequestFactory()
        request = factory.get("/x/", {"project": project.id})
        force_authenticate(request, user=as_user or self.creator)
        view = DocumentReviewDurationViewSet.as_view({"get": "list"}, permission_classes=[])
        return view(request)

    def _upsert_request(self, project, data, as_user=None):
        factory = APIRequestFactory()
        request = factory.post("/x/", data, format="json")
        force_authenticate(request, user=as_user or self.creator)
        view = DocumentReviewDurationViewSet.as_view({"post": "upsert"}, permission_classes=[])
        return view(request)

    def test_read_permission_enforced(self):
        project, _folder = self.make_project("Project N")
        with patch(f"{PERMISSION_TARGET}._check_view_permission", return_value=False):
            response = self._list_request(project)
        self.assertEqual(response.status_code, 403)

    def test_read_allowed_with_permission(self):
        project, _folder = self.make_project("Project N2")
        with patch(f"{PERMISSION_TARGET}._check_view_permission", return_value=True):
            response = self._list_request(project)
        self.assertEqual(response.status_code, 200)

    def test_update_permission_enforced(self):
        project, _folder = self.make_project("Project O")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=False):
            response = self._upsert_request(
                project, {"project": project.id, "document_type": "Drawing",
                          "supplier_review_duration_days": 10},
            )
        self.assertEqual(response.status_code, 403)

    def test_update_allowed_with_permission(self):
        project, _folder = self.make_project("Project O2")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=True):
            response = self._upsert_request(
                project, {"project": project.id, "document_type": "Drawing",
                          "supplier_review_duration_days": 10},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["supplier_review_duration_days"], 10)

    def test_project_a_configuration_cannot_be_modified_via_project_b_context(self):
        project_a, _ = self.make_project("Project P-A")
        project_b, _ = self.make_project("Project P-B")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=True):
            self._upsert_request(
                project_a, {"project": project_a.id, "document_type": "Drawing",
                            "supplier_review_duration_days": 10},
            )
            self._upsert_request(
                project_b, {"project": project_b.id, "document_type": "Drawing",
                            "supplier_review_duration_days": 99},
            )
        effective_a = resolve_review_durations(project=project_a, document_type="Drawing")
        effective_b = resolve_review_durations(project=project_b, document_type="Drawing")
        self.assertEqual(effective_a["supplier_review_duration_days"], 10)
        self.assertEqual(effective_b["supplier_review_duration_days"], 99)


# Q. No configuration row is created simply by resolving defaults.
class NoSideEffectTests(ReviewDurationBase):
    def test_resolve_review_durations_creates_no_row(self):
        project, _folder = self.make_project("Project Q")
        self.assertEqual(DocumentTypeReviewDuration.objects.count(), 0)
        resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(DocumentTypeReviewDuration.objects.count(), 0)

    def test_list_effective_review_durations_creates_no_row(self):
        project, _folder = self.make_project("Project Q2")
        self.assertEqual(DocumentTypeReviewDuration.objects.count(), 0)
        results = list_effective_review_durations(project)
        self.assertEqual(len(results), 2)  # Drawing + Report, from ProjectFixtureMixin's TYPES
        self.assertEqual(DocumentTypeReviewDuration.objects.count(), 0)

    def test_api_list_creates_no_row(self):
        project, _folder = self.make_project("Project Q3")
        factory = APIRequestFactory()
        request = factory.get("/x/", {"project": project.id})
        force_authenticate(request, user=self.creator)
        view = DocumentReviewDurationViewSet.as_view({"get": "list"}, permission_classes=[])
        with patch(f"{PERMISSION_TARGET}._check_view_permission", return_value=True):
            response = view(request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(DocumentTypeReviewDuration.objects.count(), 0)


# FINISH-B: one Save action persists every dirty row in one request.
class BulkUpsertServiceTests(ReviewDurationBase):
    def test_multiple_rows_are_persisted_in_one_call(self):
        project, _folder = self.make_project("Project BU1")
        result = bulk_upsert_review_durations(
            project=project,
            rows=[
                {"document_type": "Drawing", "supplier_review_duration_days": 10},
                {"document_type": "Report", "client_review_duration_days": 20},
            ],
        )
        self.assertEqual(result["updated"], ["Drawing", "Report"])
        self.assertEqual(result["failures"], [])
        self.assertEqual(
            resolve_review_durations(project=project, document_type="Drawing")[
                "supplier_review_duration_days"
            ],
            10,
        )
        self.assertEqual(
            resolve_review_durations(project=project, document_type="Report")[
                "client_review_duration_days"
            ],
            20,
        )

    def test_unchanged_fields_on_a_row_are_not_touched(self):
        project, _folder = self.make_project("Project BU2")
        upsert_review_duration(
            project=project, document_type="Drawing",
            values={"client_review_duration_days": 20},
        )
        bulk_upsert_review_durations(
            project=project,
            rows=[{"document_type": "Drawing", "supplier_review_duration_days": 10}],
        )
        effective = resolve_review_durations(project=project, document_type="Drawing")
        self.assertEqual(effective["supplier_review_duration_days"], 10)
        self.assertEqual(effective["client_review_duration_days"], 20)

    def test_one_invalid_row_fails_independently_without_rolling_back_the_others(self):
        project, _folder = self.make_project("Project BU3")
        result = bulk_upsert_review_durations(
            project=project,
            rows=[
                {"document_type": "Drawing", "supplier_review_duration_days": 10},
                {"document_type": "Report", "supplier_review_duration_days": 0},
            ],
        )
        self.assertEqual(result["updated"], ["Drawing"])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(result["failures"][0]["document_type"], "Report")
        # The valid row's write survives even though a later row in the same request failed.
        self.assertEqual(
            resolve_review_durations(project=project, document_type="Drawing")[
                "supplier_review_duration_days"
            ],
            10,
        )
        self.assertEqual(
            resolve_review_durations(project=project, document_type="Report")[
                "supplier_review_duration_days"
            ],
            None,
        )

    def test_unknown_document_type_in_one_row_is_reported_as_a_failure(self):
        project, _folder = self.make_project("Project BU4")
        result = bulk_upsert_review_durations(
            project=project,
            rows=[{"document_type": "NotConfigured", "supplier_review_duration_days": 10}],
        )
        self.assertEqual(result["updated"], [])
        self.assertEqual(result["failures"][0]["document_type"], "NotConfigured")

    def test_more_than_the_row_cap_is_rejected(self):
        project, _folder = self.make_project("Project BU5")
        rows = [
            {"document_type": "Drawing", "supplier_review_duration_days": 10},
        ] * (MAX_BULK_REVIEW_DURATION_ROWS + 1)
        with self.assertRaises(ValidationError):
            bulk_upsert_review_durations(project=project, rows=rows)


# FINISH-B: the bulk-upsert API action reuses the exact same write permission as single upsert.
class BulkUpsertApiTests(ReviewDurationBase):
    def _bulk_request(self, project, rows, as_user=None):
        factory = APIRequestFactory()
        request = factory.post(
            "/x/", {"project": project.id, "rows": rows}, format="json",
        )
        force_authenticate(request, user=as_user or self.creator)
        view = DocumentReviewDurationViewSet.as_view(
            {"post": "bulk_upsert"}, permission_classes=[],
        )
        return view(request)

    def test_bulk_upsert_permission_enforced(self):
        project, _folder = self.make_project("Project BU6")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=False):
            response = self._bulk_request(
                project, [{"document_type": "Drawing", "supplier_review_duration_days": 10}],
            )
        self.assertEqual(response.status_code, 403)

    def test_bulk_upsert_allowed_with_permission_persists_all_rows(self):
        project, _folder = self.make_project("Project BU7")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=True):
            response = self._bulk_request(
                project,
                [
                    {"document_type": "Drawing", "supplier_review_duration_days": 10},
                    {"document_type": "Report", "client_review_duration_days": 20},
                ],
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], ["Drawing", "Report"])
        self.assertEqual(response.data["failures"], [])

    def test_bulk_upsert_rejects_an_empty_rows_list(self):
        project, _folder = self.make_project("Project BU8")
        with patch(f"{PERMISSION_TARGET}._check_update_permission", return_value=True):
            response = self._bulk_request(project, [])
        self.assertEqual(response.status_code, 400)


# Structural: the four field names are the ones this whole lot is built around.
class FieldNamesTests(ReviewDurationBase):
    def test_duration_field_names_match_the_four_independent_concepts(self):
        self.assertEqual(
            DURATION_FIELD_NAMES,
            (
                "supplier_review_duration_days",
                "client_review_duration_days",
                "internal_client_comment_integration_days",
                "supplier_client_comment_return_days",
            ),
        )
