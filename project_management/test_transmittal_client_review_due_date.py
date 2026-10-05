"""L3H-3: Client Review due date -- targeted tests only. No full Client Review/comments/
outcomes, no INTERNAL comment integration, no Supplier comment-return workflow, no
notifications (those remain future lots), deliberately not covered here.
"""

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from project_management.models import ECPMPConfig
from project_management.services.document_type_review_duration import (
    upsert_review_duration,
)
from project_management.services.transmittal_eligibility import validate_item_eligibility
from project_management.services.transmittal_finalize import finalize_transmittal
from project_management.services.transmittal_items import add_transmittal_item
from project_management.test_transmittal import TransmittalTestBase

DOCUMENT_TYPE = "Drawing"


class ClientReviewDueDateBase(TransmittalTestBase):
    def configure_document_types(self, document_types=(f"{DOCUMENT_TYPE}:DWG",)):
        ECPMPConfig.objects.update_or_create(
            project=self.project, defaults={"document_types": list(document_types)},
        )

    def set_document_type(self, revision, document_type=DOCUMENT_TYPE):
        document = revision.document
        document.document_type = document_type
        document.save(update_fields=["document_type"])
        return revision


# A. A DRAFT OUT item has no Client review snapshot.
class DraftItemTests(ClientReviewDueDateBase):
    def test_draft_item_has_no_review_snapshot(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertIsNone(item.review_duration_days)
        self.assertIsNone(item.review_due_date)


# B/C/D/E/F. Snapshot correctness on successful OUT FOR_REVIEW finalization.
class SnapshotOnFinalizationTests(ClientReviewDueDateBase):
    def test_issuing_for_review_snapshots_configured_duration(self):
        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 10},
        )
        transmittal = self.create_transmittal()
        revision = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual(item.review_duration_days, 10)
        self.assertEqual(
            item.review_due_date, finalized.issued_at.date() + timedelta(days=10)
        )

    def test_no_override_snapshots_default_15(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual(item.review_duration_days, 15)
        self.assertEqual(
            item.review_due_date, finalized.issued_at.date() + timedelta(days=15)
        )

    def test_due_date_uses_transmittal_issued_at(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual(item.review_due_date, finalized.issued_at.date() + timedelta(days=15))

    def test_due_date_does_not_use_revision_creation_date(self):
        from project_management.models import DocumentRevision

        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        backdated = timezone.now() - timedelta(days=30)
        # DocumentRevisionQuerySet.update() is blocked at the application level (write-once
        # immutability) -- raw SQL is the established way this test suite already bypasses
        # such application-level guards for test setup (test_company_root_repair.py's own
        # connection.cursor() use), never a production code path.
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {DocumentRevision._meta.db_table} SET created_at = %s WHERE id = %s",
                [backdated, revision.pk],
            )
        revision.refresh_from_db()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        # Due date is anchored to issuance (today), never to the (deliberately backdated)
        # revision creation date 30 days ago.
        self.assertEqual(item.review_due_date, finalized.issued_at.date() + timedelta(days=15))
        self.assertNotEqual(item.review_due_date, backdated.date() + timedelta(days=15))

    def test_calendar_day_arithmetic(self):
        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 7},
        )
        transmittal = self.create_transmittal()
        revision = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual((item.review_due_date - finalized.issued_at.date()).days, 7)


# G/H/I. Configuration-change isolation + independent historical snapshots.
class ConfigurationIsolationTests(ClientReviewDueDateBase):
    def test_configuration_change_after_issuance_does_not_alter_existing_item(self):
        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 10},
        )
        transmittal = self.create_transmittal()
        revision = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        original_duration, original_due_date = item.review_duration_days, item.review_due_date

        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 20},
        )
        item.refresh_from_db()
        self.assertEqual(item.review_duration_days, original_duration)
        self.assertEqual(item.review_due_date, original_due_date)

    def test_later_qualifying_issuance_uses_new_configuration(self):
        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 10},
        )
        transmittal_a = self.create_transmittal()
        revision_a = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal_a, revision_a, "FOR_REVIEW", self.owner)
        finalized_a = finalize_transmittal(transmittal_a, self.owner)
        item_a = finalized_a.items.get()

        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 20},
        )
        transmittal_b = self.create_transmittal()
        revision_b = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal_b, revision_b, "FOR_REVIEW", self.owner)
        finalized_b = finalize_transmittal(transmittal_b, self.owner)
        item_b = finalized_b.items.get()

        self.assertEqual(item_a.review_duration_days, 10)
        self.assertEqual(item_b.review_duration_days, 20)

    def test_same_exact_revision_in_separate_out_issuances_preserves_each_items_own_snapshot(self):
        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 10},
        )
        revision = self.set_document_type(self.make_approved_internal_revision())
        transmittal_1 = self.create_transmittal()
        add_transmittal_item(transmittal_1, revision, "FOR_REVIEW", self.owner)
        finalized_1 = finalize_transmittal(transmittal_1, self.owner)
        item_1 = finalized_1.items.get()

        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 20},
        )
        # The SAME exact revision, legitimately re-transmitted in a brand new OUT Transmittal
        # (the unique constraint is per-Transmittal, not globally per-revision).
        transmittal_2 = self.create_transmittal()
        add_transmittal_item(transmittal_2, revision, "FOR_REVIEW", self.owner)
        finalized_2 = finalize_transmittal(transmittal_2, self.owner)
        item_2 = finalized_2.items.get()

        item_1.refresh_from_db()
        self.assertEqual(item_1.review_duration_days, 10)
        self.assertEqual(item_2.review_duration_days, 20)
        self.assertNotEqual(item_1.review_due_date, item_2.review_due_date)


# J/S. FOR_INFORMATION gets no review duration/due date, never overdue.
class ForInformationTests(ClientReviewDueDateBase):
    def test_for_information_gets_no_review_duration_or_due_date(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertIsNone(item.review_duration_days)
        self.assertIsNone(item.review_due_date)

    def test_for_information_is_never_overdue(self):
        from project_management.serializers.transmittal import TransmittalItemSerializer

        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_INFORMATION", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertFalse(TransmittalItemSerializer(item).data["is_overdue"])


# K. CLIENT-origin documents remain blocked by the existing (unchanged) rule.
class ClientOriginGateTests(ClientReviewDueDateBase):
    def test_client_origin_document_remains_ineligible_for_out(self):
        transmittal = self.create_transmittal()
        revision = self.make_client_revision()
        with self.assertRaises(ValidationError):
            validate_item_eligibility(transmittal, revision)


# L/M/N. Supplier/Internal gates unchanged -- Client timing only follows a successful issuance.
class EligibilityUnchangedTests(ClientReviewDueDateBase):
    def test_supplier_reviewed_document_gets_client_timing_when_issued_for_review(self):
        revision = self.make_supplier_revision(
            category="FOR_REVIEW", client_category="For review",
        )
        self.make_surveillance(revision, "CLOSED", result="REVIEWED")
        transmittal = self.create_transmittal()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual(item.review_duration_days, 15)
        self.assertIsNotNone(item.review_due_date)

    def test_supplier_pending_surveillance_remains_ineligible(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        self.make_surveillance(revision, "UNDER_REVIEW")
        transmittal = self.create_transmittal()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_supplier_commented_surveillance_remains_ineligible(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        self.make_surveillance(revision, "CLOSED", result="COMMENTED")
        transmittal = self.create_transmittal()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_supplier_rejected_surveillance_remains_ineligible(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        self.make_surveillance(revision, "CLOSED", result="REJECTED")
        transmittal = self.create_transmittal()
        with self.assertRaises(ValidationError):
            add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)

    def test_internal_approved_document_gets_client_timing_when_issued_for_review(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        self.assertEqual(item.review_duration_days, 15)
        self.assertIsNotNone(item.review_due_date)


# O/P/Q/R. is_overdue derivation (via the serializer, the chosen exposure point).
class IsOverdueTests(ClientReviewDueDateBase):
    def _issued_item(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        return finalized.items.get()

    def _is_overdue(self, item):
        from project_management.serializers.transmittal import TransmittalItemSerializer

        return TransmittalItemSerializer(item).data["is_overdue"]

    def test_null_due_date_is_never_overdue(self):
        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        item = add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        self.assertIsNone(item.review_due_date)
        self.assertFalse(self._is_overdue(item))

    def test_past_due_date_is_overdue(self):
        item = self._issued_item()
        item.review_due_date = timezone.localdate() - timedelta(days=1)
        item.save(update_fields=["review_due_date"])
        self.assertTrue(self._is_overdue(item))

    def test_due_today_is_not_overdue(self):
        item = self._issued_item()
        item.review_due_date = timezone.localdate()
        item.save(update_fields=["review_due_date"])
        self.assertFalse(self._is_overdue(item))

    def test_future_due_date_is_not_overdue(self):
        item = self._issued_item()
        item.review_due_date = timezone.localdate() + timedelta(days=1)
        item.save(update_fields=["review_due_date"])
        self.assertFalse(self._is_overdue(item))


# T. The serializer/API exposes review_duration_days, review_due_date, is_overdue.
class ApiExposureTests(ClientReviewDueDateBase):
    def test_serializer_exposes_duration_due_date_and_is_overdue(self):
        from project_management.serializers.transmittal import TransmittalItemSerializer

        self.configure_document_types()
        upsert_review_duration(
            project=self.project, document_type=DOCUMENT_TYPE,
            values={"client_review_duration_days": 10},
        )
        transmittal = self.create_transmittal()
        revision = self.set_document_type(self.make_approved_internal_revision())
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        item = finalized.items.get()
        data = TransmittalItemSerializer(item).data
        self.assertEqual(data["review_duration_days"], 10)
        # DRF's DateField.to_representation renders an ISO string, not a raw date object.
        self.assertEqual(
            data["review_due_date"], str(finalized.issued_at.date() + timedelta(days=10)),
        )
        self.assertIn("is_overdue", data)
        self.assertFalse(data["is_overdue"])

    def test_header_transmittal_serializer_nests_the_same_fields(self):
        from project_management.serializers.transmittal import TransmittalSerializer

        transmittal = self.create_transmittal()
        revision = self.make_approved_internal_revision()
        add_transmittal_item(transmittal, revision, "FOR_REVIEW", self.owner)
        finalized = finalize_transmittal(transmittal, self.owner)
        data = TransmittalSerializer(finalized).data
        item_data = data["items"][0]
        self.assertIn("review_duration_days", item_data)
        self.assertIn("review_due_date", item_data)
        self.assertIn("is_overdue", item_data)
