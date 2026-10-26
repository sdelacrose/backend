"""L3H-2: Supplier Surveillance due date -- targeted tests only. No Client Review timing, no
Transmittal timing, no notifications, no Supplier comment-return workflow (those remain
L3H-3/L3I/future lots), deliberately not covered here.
"""

from datetime import timedelta

from django.utils import timezone

from project_management.models import SupplierSurveillance
from project_management.services.document_type_review_duration import (
    upsert_review_duration,
)
from project_management.services.supplier_surveillance import (
    compute_is_overdue,
    create_supplier_surveillance,
)
from project_management.test_supplier_surveillance import SupplierSurveillanceBase
from project_management.views.supplier_surveillance import serialize_supplier_surveillance

Status = SupplierSurveillance.Status


class DueDateSnapshotTests(SupplierSurveillanceBase):
    # A. Configured Supplier duration is snapshotted.
    def test_for_review_surveillance_snapshots_configured_duration(self):
        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 10},
        )
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        self.assertEqual(surveillance.review_duration_days, 10)
        self.assertEqual(
            surveillance.review_due_date,
            revision.created_at.date() + timedelta(days=10),
        )

    # B. No override -> system default 15.
    def test_no_override_snapshots_default_15(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        self.assertEqual(surveillance.review_duration_days, 15)
        self.assertEqual(
            surveillance.review_due_date,
            revision.created_at.date() + timedelta(days=15),
        )

    # C. Due date uses DocumentRevision.created_at, NOT Surveillance.created_at.
    def test_due_date_uses_revision_created_at_not_surveillance_created_at(self):
        from django.db import connection

        revision = self.make_supplier_revision(category="FOR_REVIEW")
        backdated = timezone.now() - timedelta(days=30)
        # DocumentRevisionQuerySet.update() is blocked at the application level (write-once
        # immutability) -- raw SQL is the established way this test suite already exercises/
        # bypasses such application-level guards for test setup (see
        # test_company_root_repair.py's own connection.cursor() use), never a production code
        # path. Simulates a revision actually received 30 days ago, with the Surveillance only
        # created today -- the exact trap section 3 warns against.
        from project_management.models import DocumentRevision

        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {DocumentRevision._meta.db_table} SET created_at = %s WHERE id = %s",
                [backdated, revision.pk],
            )
        revision.refresh_from_db()
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        self.assertEqual(surveillance.review_due_date, backdated.date() + timedelta(days=15))
        self.assertNotEqual(surveillance.review_due_date, surveillance.created_at.date() + timedelta(days=15))

    # D. Due date uses plain calendar days (no working-day logic).
    def test_due_date_uses_calendar_days(self):
        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 7},
        )
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        self.assertEqual(
            (surveillance.review_due_date - revision.created_at.date()).days, 7
        )

    # E. Configuration change after creation never alters an existing snapshot.
    def test_configuration_change_does_not_alter_existing_snapshot(self):
        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 10},
        )
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        original_duration = surveillance.review_duration_days
        original_due_date = surveillance.review_due_date

        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 20},
        )
        surveillance.refresh_from_db()
        self.assertEqual(surveillance.review_duration_days, original_duration)
        self.assertEqual(surveillance.review_due_date, original_due_date)

    # F/G. A new Surveillance after a configuration change uses the new duration; exact
    # revisions A and B retain independent deadlines.
    def test_new_surveillance_after_configuration_change_uses_new_duration(self):
        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 10},
        )
        revision_a = self.make_supplier_revision(category="FOR_REVIEW", external_revision="A")
        template = self.make_template()
        surveillance_a = create_supplier_surveillance(
            document_revision=revision_a, actor=self.creator, template=template,
        )

        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 20},
        )
        revision_b = self.make_supplier_revision(category="FOR_REVIEW", external_revision="B")
        surveillance_b = create_supplier_surveillance(
            document_revision=revision_b, actor=self.creator, template=template,
        )

        self.assertEqual(surveillance_a.review_duration_days, 10)
        self.assertEqual(surveillance_b.review_duration_days, 20)
        self.assertNotEqual(surveillance_a.review_due_date, surveillance_b.review_due_date)
        self.assertEqual(
            surveillance_a.review_due_date, revision_a.created_at.date() + timedelta(days=10)
        )
        self.assertEqual(
            surveillance_b.review_due_date, revision_b.created_at.date() + timedelta(days=20)
        )

    # H. FOR_INFORMATION exceptional/manual Surveillance gets no mandatory deadline.
    def test_for_information_surveillance_gets_no_mandatory_deadline(self):
        revision = self.make_supplier_revision(category="FOR_INFORMATION")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        self.assertIsNone(surveillance.review_duration_days)
        self.assertIsNone(surveillance.review_due_date)


class IsOverdueTests(SupplierSurveillanceBase):
    def _surveillance(self, *, status, review_due_date):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        surveillance.status = status
        surveillance.review_due_date = review_due_date
        surveillance.save(update_fields=["status", "review_due_date"])
        return surveillance

    # I. Null due date -> never overdue.
    def test_null_due_date_is_never_overdue(self):
        surveillance = self._surveillance(status=Status.DRAFT, review_due_date=None)
        self.assertFalse(compute_is_overdue(surveillance))

    # J/K/L. Yesterday + DRAFT/UNDER_REVIEW/READY_FOR_CLOSURE -> overdue.
    def test_yesterday_draft_is_overdue(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        surveillance = self._surveillance(status=Status.DRAFT, review_due_date=yesterday)
        self.assertTrue(compute_is_overdue(surveillance))

    def test_yesterday_under_review_is_overdue(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        surveillance = self._surveillance(status=Status.UNDER_REVIEW, review_due_date=yesterday)
        self.assertTrue(compute_is_overdue(surveillance))

    def test_yesterday_ready_for_closure_is_overdue(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        surveillance = self._surveillance(
            status=Status.READY_FOR_CLOSURE, review_due_date=yesterday,
        )
        self.assertTrue(compute_is_overdue(surveillance))

    # M. Yesterday + CLOSED -> never overdue.
    def test_yesterday_closed_is_never_overdue(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        surveillance = self._surveillance(status=Status.CLOSED, review_due_date=yesterday)
        self.assertFalse(compute_is_overdue(surveillance))

    # N. Due today -> not yet overdue.
    def test_due_today_is_not_overdue(self):
        today = timezone.localdate()
        surveillance = self._surveillance(status=Status.DRAFT, review_due_date=today)
        self.assertFalse(compute_is_overdue(surveillance))

    # O. Future due date -> not overdue.
    def test_future_due_date_is_not_overdue(self):
        tomorrow = timezone.localdate() + timedelta(days=1)
        surveillance = self._surveillance(status=Status.DRAFT, review_due_date=tomorrow)
        self.assertFalse(compute_is_overdue(surveillance))


# P. The existing API exposes review_duration_days, review_due_date, is_overdue.
class ApiExposureTests(SupplierSurveillanceBase):
    def test_serializer_exposes_review_duration_days_due_date_and_is_overdue(self):
        upsert_review_duration(
            project=self.project, document_type="Drawing",
            values={"supplier_review_duration_days": 10},
        )
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        data = serialize_supplier_surveillance(surveillance)
        self.assertEqual(data["review_duration_days"], 10)
        self.assertEqual(
            data["review_due_date"], revision.created_at.date() + timedelta(days=10)
        )
        self.assertIn("is_overdue", data)
        self.assertFalse(data["is_overdue"])

    def test_serializer_reports_is_overdue_true_when_applicable(self):
        revision = self.make_supplier_revision(category="FOR_REVIEW")
        template = self.make_template()
        surveillance = create_supplier_surveillance(
            document_revision=revision, actor=self.creator, template=template,
        )
        surveillance.review_due_date = timezone.localdate() - timedelta(days=1)
        surveillance.save(update_fields=["review_due_date"])
        data = serialize_supplier_surveillance(surveillance)
        self.assertTrue(data["is_overdue"])
